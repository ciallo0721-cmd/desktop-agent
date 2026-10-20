# -*- coding: utf-8 -*-
"""Agent 核心：对话存储、动作解析、工具执行、多步循环。

设计要点：
- AI 通过 ```action 代码块里的 JSON 调用工具，不依赖 function calling，
  所以任何 OpenAI 兼容接口都能用。
- 流式输出时动作块不能显示给用户，strip_actions / strip_streaming 负责剥掉。
- 每个动作执行完把结果塞回对话，再让 AI 继续，直到 done 或达到 max_steps。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config, llm, memory, prompts, tools, websearch
from . import images as images_mod

# ```action\n{...}\n```
ACTION_FENCE = re.compile(r"```[ \t]*action[ \t]*\r?\n?(.*?)```", re.S | re.I)
# 流式输出时可能还没闭合的围栏
FENCE_TAIL = re.compile(r"```[ \t]*action[ \t]*.*$", re.S | re.I)

# --------------------------------------------------------------------------
# DeepSeek DSML 兜底
#
# deepseek 系模型有时不走我们规定的 ```action JSON，而是直接吐它自己的
# 工具调用标记（训练里见过的那套），实测原文：
#   <｜｜DSML｜｜ calls>
#   <｜｜DSML｜｜ invoke name="bash">
#   <｜｜DSML｜｜ parameter name="cmd" string="true">ls -la</｜｜DSML｜｜ parameter>
#   </｜｜DSML｜｜ invoke>
#   </｜｜DSML｜｜ calls>
# 竖线是全角 U+FF5C，也可能被换成 ASCII 的 | 或三连。
# 这里不认死具体分隔符，只要求中间出现 DSML 三个字母。
# --------------------------------------------------------------------------
_DSML_TAG = r"<[^<>\n]{0,24}?DSML[^<>\n]{0,24}?"
DSML_INVOKE = re.compile(
    _DSML_TAG + r"\s*invoke\s+name=\"([^\"]+)\"[^>]*>(.*?)"
    r"</[^<>\n]{0,24}?DSML[^<>\n]{0,24}?\s*invoke\s*>",
    re.S | re.I,
)
DSML_PARAM = re.compile(
    _DSML_TAG + r"\s*parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)"
    r"</[^<>\n]{0,24}?DSML[^<>\n]{0,24}?\s*parameter\s*>",
    re.S | re.I,
)
# 整个 calls 块（外层有就整块吃掉）
DSML_BLOCK = re.compile(
    _DSML_TAG + r"\s*(?:tool_)?(?:calls|function_calls)\s*>.*?"
    r"(?:</[^<>\n]{0,24}?DSML[^<>\n]{0,24}?\s*(?:tool_)?(?:calls|function_calls)\s*>|$)",
    re.S | re.I,
)
# 流式时还没吐完的标签（含 <｜｜DSM 这种半截）
DSML_TAIL = re.compile(r"<[｜|][^\n]*$", re.S)
DSML_LINE = re.compile(
    r"(?m)^[ \t]*" + _DSML_TAG + r"[^\n]*>[ \t]*$"
)

TOOL_RESULT_PREFIX = "[工具 {name} 的执行结果]"

# 这些参数要转成数字，否则 click 之类会拿到字符串
_DSML_NUM_KEYS = {"x", "y", "amount", "seconds", "width", "height", "count"}
_DSML_UNESCAPE = (
    ("&quot;", '"'), ("&apos;", "'"),
    ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
)


# ==========================================================================
# 动作解析
# ==========================================================================

def _dsml_value(key: str, raw: str):
    """把 DSML 的参数值转成合适的类型。"""
    v = raw.strip()
    for bad, good in _DSML_UNESCAPE:
        v = v.replace(bad, good)
    if key == "region":
        nums = [p for p in re.split(r"[,\s]+", v.strip("[]() ")) if p]
        if len(nums) == 4:
            try:
                return [int(float(p)) for p in nums]
            except ValueError:
                pass
        return v
    if key in _DSML_NUM_KEYS:
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


def parse_dsml_actions(text: str) -> list[dict]:
    """从 DSML 风格的回复里抽动作。不是 DSML 就返回空表。"""
    if not text or "DSML" not in text.upper():
        return []
    out: list[dict] = []
    for m in DSML_INVOKE.finditer(text):
        name = (m.group(1) or "").strip()
        if not name:
            continue
        act: dict = {"tool": name}
        for p in DSML_PARAM.finditer(m.group(2) or ""):
            key = (p.group(1) or "").strip()
            if not key:
                continue
            act[key] = _dsml_value(key, p.group(2) or "")
        out.append(act)
    return out


def _strip_dsml(text: str) -> str:
    """把 DSML 标记从展示文本里剥掉。"""
    if not text or "DSML" not in text.upper():
        return text
    cleaned = DSML_BLOCK.sub("", text)
    cleaned = DSML_INVOKE.sub("", cleaned)
    cleaned = DSML_PARAM.sub("", cleaned)
    # 残留的纯标签行
    cleaned = DSML_LINE.sub("", cleaned)
    return cleaned


def parse_actions(text: str) -> list[dict]:
    """从回复里抽出所有动作块。解析失败的块返回 __bad_json__。

    先按约定的 ```action JSON 找，找不到再兜底 DSML 格式。
    """
    out: list[dict] = []
    for m in ACTION_FENCE.finditer(text or ""):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            out.append({"tool": "__bad_json__", "raw": raw})
            continue
        if isinstance(obj, dict):
            if "tool" in obj:
                out.append(obj)
            elif "name" in obj:
                obj["tool"] = obj.pop("name")
                out.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and "tool" in item:
                    out.append(item)

    if not out:
        out = parse_dsml_actions(text)
    return out


def strip_actions(text: str) -> str:
    """去掉动作块，只留给人看的部分。"""
    if not text:
        return ""
    cleaned = ACTION_FENCE.sub("", text)
    cleaned = FENCE_TAIL.sub("", cleaned)
    cleaned = _strip_dsml(cleaned)
    return cleaned.strip()


def strip_streaming(text: str) -> str:
    """流式过程中的显示文本：完整块去掉，未闭合的块从围栏处截断。"""
    cleaned = ACTION_FENCE.sub("", text or "")
    cleaned = FENCE_TAIL.sub("", cleaned)
    cleaned = _strip_dsml(cleaned)
    # 正在吐的 DSML 标签，从残留的 <｜ 处整体截掉
    cleaned = DSML_TAIL.sub("", cleaned)
    return cleaned.strip()


def describe_action(act: dict) -> str:
    """给界面显示的一行摘要。"""
    tool = act.get("tool", "?")
    if tool == "bash":
        return str(act.get("cmd", ""))[:160]
    if tool == "click":
        return f"({act.get('x')}, {act.get('y')}) {act.get('button', 'left')}"
    if tool == "type":
        return str(act.get("text", ""))[:100]
    if tool == "key":
        return str(act.get("keys", ""))
    if tool == "move":
        return f"({act.get('x')}, {act.get('y')})"
    if tool == "scroll":
        return str(act.get("amount", 0))
    if tool == "wait":
        return f"{act.get('seconds', 1)}s"
    if tool == "screenshot":
        region = act.get("region")
        if isinstance(region, (list, tuple)) and len(region) == 4:
            return f"区域 {region[2]}x{region[3]} @ ({region[0]}, {region[1]})"
        return "全屏"
    if tool == "copy_region":
        return f"({act.get('x')}, {act.get('y')}) {act.get('w')}x{act.get('h')}"
    if tool == "clipboard":
        op = str(act.get("action", "read")).lower()
        if op in ("write", "set", "copy"):
            return f"写入 {len(str(act.get('text', '')))} 字"
        return "读取"
    if tool == "web_search":
        return str(act.get("query", ""))[:100]
    if tool == "web_fetch":
        return str(act.get("url", ""))[:100]
    if tool == "done":
        return str(act.get("summary", ""))[:120]
    return ""


# ==========================================================================
# 对话存储
# ==========================================================================

class Conversation:
    def __init__(self, cid: str | None = None, title: str = "新对话",
                 messages: list[dict] | None = None,
                 created: float | None = None, updated: float | None = None):
        self.id = cid or time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.title = title or "新对话"
        self.messages = messages or []
        self.created = created or time.time()
        self.updated = updated or time.time()

    # ---------------- 磁盘 ----------------
    @property
    def path(self) -> Path:
        return config.CONV_DIR / f"{self.id}.json"

    def save(self) -> None:
        config.ensure_dirs()
        self.updated = time.time()
        data = {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "messages": self.messages,
        }
        try:
            text = json.dumps(data, ensure_ascii=False, indent=1)
            # 覆盖前先留一份快照，任何一次写入都能回滚
            if self.path.exists():
                self._snapshot()
            # 原子写：先落临时文件再 replace，避免中途崩溃留下半截 json
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _snapshot(self) -> None:
        """每对话保留最近 8 份历史，按分钟归档。

        同一分钟内重复写入会覆盖当分钟那份（保留更完整的版本），
        跨分钟才新增，避免工具循环把快照刷爆。
        """
        folder = config.CONV_BACKUP_DIR / self.id
        try:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / (time.strftime("%Y%m%d_%H%M") + ".json")
            if dest.exists():
                try:
                    if dest.stat().st_size >= self.path.stat().st_size:
                        return
                except OSError:
                    return
            shutil.copy2(self.path, dest)
            olds = sorted(folder.glob("*.json"))
            for p in olds[:-8]:
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path) -> "Conversation":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        return cls(
            cid=data.get("id") or path.stem,
            title=data.get("title") or "新对话",
            messages=data.get("messages") or [],
            created=data.get("created"),
            updated=data.get("updated"),
        )

    def delete(self) -> None:
        """不真删，移到 data/trash 里，随时能捞回来。"""
        try:
            config.ensure_dirs()
            dest = config.CONV_TRASH_DIR / self.path.name
            if dest.exists():
                dest = config.CONV_TRASH_DIR / (
                    f"{self.id}_{int(time.time())}.json"
                )
            os.replace(self.path, dest)
        except OSError:
            pass
        # 图片跟着走，别在 data/images 里留一堆没人认领的文件
        images_mod.drop_conv(self.id)

    # ---------------- 消息 ----------------
    def add_user(self, text: str, images: list[str] | None = None) -> None:
        msg = {"role": "user", "content": text}
        if images:
            msg["images"] = list(images)
        self.messages.append(msg)
        if self.title in ("新对话", "") and text.strip():
            self.title = " ".join(text.split())[:24]
        elif self.title in ("新对话", "") and images:
            self.title = f"[图片] {Path(images[0]).name}"[:24]
        self.save()

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
        self.save()

    def add_tool(self, name: str, result: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool": name,
            "tool_result": True,
            "content": result,
        })
        self.save()

    def user_turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")

    def visible(self) -> list[dict]:
        """需要画到界面上的消息。纯动作的 AI 回复会被丢掉。"""
        out = []
        for m in self.messages:
            if m.get("tool_result"):
                out.append(m)
                continue
            if m.get("role") == "assistant":
                if not strip_actions(m.get("content", "")).strip():
                    continue
                out.append(m)
                continue
            # 只发图不打字的消息也要显示，否则界面上凭空少一条
            if (m.get("content") or "").strip() or m.get("images"):
                out.append(m)
        return out

    def to_api_messages(self, limit: int = 24) -> list[dict]:
        """转成 OpenAI 格式。工具结果伪装成 user 消息，兼容性最好。

        图片只在「最近一条带图的消息」里发原图，更早的降级成一句占位：
        否则历史里堆五六张图，每次请求体都是十几 MB。
        """
        window = self.messages[-max(2, limit):]

        last_img = -1
        for i, m in enumerate(window):
            if m.get("role") == "user" and m.get("images"):
                last_img = i

        rows: list[dict] = []
        for i, m in enumerate(window):
            role = m.get("role")
            content = m.get("content") or ""
            if m.get("tool_result"):
                name = m.get("tool", "tool")
                rows.append({
                    "role": "user",
                    "content": TOOL_RESULT_PREFIX.format(name=name) + "\n" + content,
                })
            elif role in ("user", "assistant"):
                imgs = m.get("images") or []
                if imgs and role == "user":
                    if i != last_img:
                        rows.append({
                            "role": "user",
                            "content": content + "\n（这里原本有图片，已经省略）",
                        })
                        continue
                    parts: list[dict] = []
                    if content.strip():
                        parts.append({"type": "text", "text": content})
                    n = 0
                    for rel in imgs:
                        got = images_mod.api_parts(rel)
                        if got:
                            parts += got
                            n += 1
                    if n:
                        rows.append({"role": "user", "content": parts})
                    else:
                        rows.append({"role": "user",
                                     "content": content + "\n（图片读取失败，没有发出去）"})
                    continue

                if content.strip():
                    rows.append({"role": role, "content": content})
                elif imgs:
                    rows.append({"role": role, "content": "（图片）"})
        return rows


def daily_backup() -> None:
    """每天首次启动时把 conversations 全量复制到 data/backup/daily/YYYYMMDD。

    对话文件本身被误删时，从这里捞。
    """
    config.ensure_dirs()
    daily = config.CONV_BACKUP_DIR / "daily"
    dest = daily / time.strftime("%Y%m%d")
    try:
        srcs = [p for p in config.CONV_DIR.glob("*.json") if p.is_file()]
        if not srcs:
            return
        # 目录存在但空（上次备份中途失败）也要重做
        if dest.exists() and any(dest.glob("*.json")):
            return
        dest.mkdir(parents=True, exist_ok=True)
        for p in srcs:
            shutil.copy2(p, dest / p.name)
        for old in sorted(daily.iterdir())[:-7]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


def list_conversations() -> list[Conversation]:
    config.ensure_dirs()
    items: list[Conversation] = []
    for p in config.CONV_DIR.glob("*.json"):
        conv = Conversation.load(p)
        if conv.messages:
            items.append(conv)
    items.sort(key=lambda c: c.updated, reverse=True)
    return items


# ==========================================================================
# 工具执行
# ==========================================================================

BAD_JSON_MSG = (
    "【解析失败】动作块的 JSON 不合法，没法执行。原文：\n{raw}\n"
    "请重新输出，确保是合法 JSON（字符串里的引号和反斜杠要转义）。"
)


def execute_action(cfg: dict, act: dict, confirm=None, cancel=None) -> str:
    """执行一个动作。confirm 只在命中高危命令时被调用。"""
    tool = (act.get("tool") or "").strip()

    if tool == "__bad_json__":
        return BAD_JSON_MSG.format(raw=str(act.get("raw", ""))[:400])

    mode = cfg.get("agent_mode", "both")
    screen_control = bool(cfg.get("screen_control", True))
    screen_ok = bool((cfg.get("vision_api") or {}).get("api_key"))

    if tool == "bash":
        if mode not in ("bash", "both"):
            return "【不允许】当前是纯截屏模式，没有 bash 权限。请改用屏幕工具，或让用户把交互方式改成「两者都用」。"
        return tools.run_bash(cfg, str(act.get("cmd", "")), confirm=confirm, cancel=cancel)

    if tool == "screenshot":
        passthrough = bool(cfg.get("vision_passthrough", True))
        if not passthrough and not screen_ok:
            return (
                "【不可用】没有配置识图 API，图片直传也是关的，截了看不见。"
                "让用户去设置里打开「截图直接给模型看」，或者填一个识图模型。"
            )
        region = act.get("region")
        if region:
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                return "【参数错误】region 要写成 [x, y, w, h] 四个整数。"
            return tools.screenshot_region(cfg, *region)
        return tools.screenshot(cfg)

    if tool == "copy_region":
        return tools.copy_region(
            act.get("x", 0), act.get("y", 0),
            act.get("w", 0), act.get("h", 0),
        )

    if tool == "clipboard":
        op = str(act.get("action", "read")).strip().lower()
        if op in ("write", "set", "copy"):
            return tools.clipboard_write(str(act.get("text", "")))
        return tools.clipboard_read()

    if tool == "click":
        if not screen_control:
            return "【不允许】屏幕控制已关闭。"
        return tools.click(act.get("x", 0), act.get("y", 0), act.get("button", "left"))

    if tool == "move":
        if not screen_control:
            return "【不允许】屏幕控制已关闭。"
        return tools.move(act.get("x", 0), act.get("y", 0))

    if tool == "type":
        if not screen_control:
            return "【不允许】屏幕控制已关闭。"
        return tools.type_text(str(act.get("text", "")))

    if tool == "key":
        if not screen_control:
            return "【不允许】屏幕控制已关闭。"
        return tools.press_key(str(act.get("keys", "")))

    if tool == "scroll":
        if not screen_control:
            return "【不允许】屏幕控制已关闭。"
        return tools.scroll(act.get("amount", 0))

    if tool == "web_search":
        if not cfg.get("web_search", True):
            return "【已关闭】联网搜索在设置里被关掉了。让用户打开它，或者改用 bash 里的 curl。"
        query = str(act.get("query") or act.get("q") or "").strip()
        if not query:
            return "【参数错误】web_search 需要 query 字段。"
        try:
            count = int(act.get("count") or cfg.get("search_results", 8))
        except (TypeError, ValueError):
            count = 8
        try:
            hits, source = websearch.search(cfg, query, count)
        except Exception as e:
            return f"【搜索失败】{type(e).__name__}: {e}"
        return websearch.format_results(query, hits, source)

    if tool == "web_fetch":
        url = str(act.get("url") or act.get("link") or "").strip()
        if not url:
            return "【参数错误】web_fetch 需要 url 字段。"
        try:
            limit = int(act.get("max_chars") or 12000)
        except (TypeError, ValueError):
            limit = 12000
        try:
            return websearch.fetch(url, max(500, min(limit, 40000)))
        except Exception as e:
            return f"【抓取失败】{type(e).__name__}: {e}"

    if tool == "wait":
        return tools.wait(act.get("seconds", 1))

    if tool == "ask":
        return str(act.get("question", "")).strip() or "（AI 想问你点什么，但没写问题）"

    if tool == "done":
        return str(act.get("summary", "")).strip() or "完成。"

    return (
        f"【未知工具】没有叫 {tool!r} 的工具。"
        "只能用系统提示里列出来的那些，检查一下拼写。"
    )


# ==========================================================================
# 主循环
# ==========================================================================

class AgentRunner:
    """一次用户输入 → 若干轮 LLM + 工具 → 结束。在后台线程里跑。"""

    def __init__(self, cfg: dict, conv: Conversation):
        self.cfg = cfg
        self.conv = conv

    def run(self, user_text: str, emit, cancel: threading.Event, confirm=None,
            images: list[str] | None = None) -> None:
        cfg = self.cfg
        conv = self.conv
        images = [p for p in (images or []) if p]

        try:
            is_new = conv.user_turns() == 0
            screen_ok = bool((cfg.get("vision_api") or {}).get("api_key"))

            system = prompts.build_system_prompt(
                cfg, memory.read_memory(cfg), is_new, screen_ok,
                tools.shell_description(cfg),
            )

            conv.add_user(user_text, images)
            messages = [{"role": "system", "content": system}]
            history = conv.to_api_messages(int(cfg.get("history_limit", 24)))
            # 最后一条就是本次输入，下面要单独带图 append，别在这儿重复一遍
            if history and conv.messages and history[-1].get("role") == "user":
                history = history[:-1]
            messages += history

            head_text = prompts.build_user_message(user_text)
            if images:
                parts: list[dict] = [{"type": "text", "text": head_text}]
                attached = 0
                for rel in images:
                    got = images_mod.api_parts(rel)
                    if got:
                        parts += got
                        attached += 1
                if attached:
                    messages.append({"role": "user", "content": parts})
                else:
                    messages.append({
                        "role": "user",
                        "content": head_text + "\n（附带的图片读取失败，没能传给模型）",
                    })
            else:
                messages.append({"role": "user", "content": head_text})

            api = config.api_of(cfg, "main_api")
            if not api.get("api_key"):
                emit({"type": "error", "msg": "没有配置主 API Key，先去设置里填。"})
                return

            max_steps = max(1, int(cfg.get("max_steps", 12)))
            temperature = float(cfg.get("temperature", 0.7))
            step = 0

            while step < max_steps:
                if cancel.is_set():
                    emit({"type": "cancel"})
                    return
                step += 1
                emit({"type": "step", "index": step})

                chunks: list[str] = []
                think_sent = [False]

                def _on_think(_t, _sent=think_sent):
                    if not _sent[0]:
                        _sent[0] = True
                        emit({"type": "thinking"})

                try:
                    for piece in llm.stream_chat(api, messages,
                                                 temperature=temperature,
                                                 cancel=cancel,
                                                 on_reasoning=_on_think):
                        chunks.append(piece)
                        emit({"type": "delta", "text": piece})
                except Exception as e:
                    emit({"type": "error", "msg": f"{type(e).__name__}: {e}"})
                    return

                if cancel.is_set():
                    text = "".join(chunks).strip()
                    if text:
                        conv.add_assistant(text)
                    emit({"type": "cancel"})
                    return

                full = "".join(chunks).strip()
                emit({"type": "assistant_end", "text": full})
                if not full:
                    emit({"type": "error", "msg": "模型返回了空内容。"})
                    return

                conv.add_assistant(full)
                messages.append({"role": "assistant", "content": full})

                actions = parse_actions(full)
                if not actions:
                    emit({"type": "done"})
                    return

                # 协议是一轮一个动作。模型偶尔会一口气甩好几个块，
                # 以前只取第 0 个、其余静默丢掉，模型还当自己都做完了。
                # 现在把「只执行了一个」写回上下文，让它下一轮重新发。
                if len(actions) > 1:
                    names = "、".join(str(a.get("tool") or "?") for a in actions)
                    first = str(actions[0].get("tool") or "?").strip()
                    note = (
                        f"你这一轮给了 {len(actions)} 个 action 块（{names}），"
                        f"但一次只能执行一个。这次只执行了「{first}」，"
                        f"其余 {len(actions) - 1} 个没有执行。"
                        f"如果还需要它们，下一轮一个一个重新发。"
                    )
                    conv.add_tool("notice", note)
                    messages.append({"role": "user", "content": "（系统提示）" + note})
                    emit({"type": "tool_start", "name": "提示",
                          "detail": f"收到 {len(actions)} 个动作，只执行第 1 个"})
                    emit({"type": "tool_result", "name": "提示", "text": note})

                act = actions[0]
                tool = (act.get("tool") or "").strip()

                if tool == "done":
                    summary = str(act.get("summary", "")).strip()
                    conv.add_tool("done", summary or "完成")
                    emit({"type": "tool_start", "name": "done", "detail": summary})
                    emit({"type": "tool_result", "name": "done", "text": summary or "完成"})
                    emit({"type": "done"})
                    return

                if tool == "ask":
                    question = str(act.get("question", "")).strip()
                    conv.add_tool("ask", question)
                    emit({"type": "ask", "text": question})
                    emit({"type": "done"})
                    return

                emit({
                    "type": "tool_start",
                    "name": tool or "?",
                    "detail": describe_action(act),
                })

                try:
                    result = execute_action(cfg, act, confirm=confirm, cancel=cancel)
                except Exception as e:
                    result = f"【工具异常】{type(e).__name__}: {e}"

                # 截图这类工具会返回 {"text":..., "image":...}，图片要单独塞进对话
                image_path = ""
                if isinstance(result, dict):
                    image_path = str(result.get("image") or "")
                    result = str(result.get("text") or "")
                elif not isinstance(result, str):
                    result = str(result)

                conv.add_tool(tool or "?", result)
                emit({"type": "tool_result", "name": tool or "?", "text": result})

                if cancel.is_set():
                    emit({"type": "cancel"})
                    return

                head = TOOL_RESULT_PREFIX.format(name=tool or "tool") + "\n" + result
                if image_path:
                    # 只留最新一张截图。旧图换成文字占位，否则请求体几轮就爆了。
                    # 注意必须只动「工具结果」消息：用户自己发的图片不能删。
                    for i, m in enumerate(messages):
                        parts = m.get("content")
                        if not isinstance(parts, list):
                            continue
                        if not any(p.get("type") == "image_url" for p in parts):
                            continue
                        txt = next((p.get("text", "") for p in parts
                                    if p.get("type") == "text"), "")
                        if not txt.startswith("[工具 "):
                            continue
                        messages[i] = {
                            "role": m.get("role", "user"),
                            "content": txt + "\n（旧截图已省略，需要时重新截一张）",
                        }
                    try:
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "text", "text": head},
                                {"type": "image_url",
                                 "image_url": {"url": llm.data_uri(image_path)}},
                            ],
                        })
                    except Exception as e:
                        messages.append({
                            "role": "user",
                            "content": head + f"\n（图片读取失败：{e}）",
                        })
                else:
                    messages.append({"role": "user", "content": head})

            emit({
                "type": "error",
                "msg": f"已经连续执行 {max_steps} 步还没结束，先停一下。"
                       "可以让用户确认方向，或者把设置里的最大步数调大。",
            })
        except Exception as e:
            emit({"type": "error", "msg": f"内部错误 {type(e).__name__}: {e}"})
