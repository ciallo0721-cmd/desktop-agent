# -*- coding: utf-8 -*-
"""Skill（技能插件）加载器。

目录结构，和 prompt/ 平级：

    skills/
      <技能id>/
        SKILL.md          必需，开头是 --- 包起来的元信息
        scripts/*.py      可选，skill_run 用
        references/*.md   可选，skill 的附件，按需读

SKILL.md 长这样：

    ---
    name: PDF 处理
    description: 拆分、合并、抽文字、转图片。用户提到 pdf 时就该用。
    when: 需要处理 .pdf 文件时
    enabled: true
    ---
    正文，也就是技能说明书。写清楚步骤、命令、注意事项。

设计取舍（和 WorkBuddy 的 skill 一个套路）：
  1. 只把「id + 名字 + 一句描述」塞进系统提示词，正文不进 —— 否则十个技能
     就把上下文吃光了，而且模型会乱套用。
  2. 模型判断需要时调 {"tool": "skill", "name": "<id>"} 才把正文读进来。
  3. 附件（references/）再用 {"tool": "skill", "name": "<id>", "file": "..."} 读。
  4. scripts/ 里的东西用 {"tool": "skill_run", ...} 在技能目录里跑。
"""
from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

MAX_BODY = 12000        # 正文给模型的上限，再长就截断
MAX_ATTACH = 20000      # 单个附件上限
MAX_ATTACH_LIST = 40    # 附件最多列这么多个


# ==========================================================================
# frontmatter
# ==========================================================================

def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 --- 包起来的元信息，返回 (meta, 正文)。

    不引 yaml 依赖：只认 `key: value`，值支持引号、支持下一行缩进续写
    （多行描述在 YAML 里常写成  description: > 加缩进，这里也能吃）。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        return {}, text.strip()

    lines = text.split("\n")
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end < 0:
        return {}, text.strip()

    meta: dict = {}
    key = ""
    buf: list[str] = []

    def flush() -> None:
        if key:
            meta[key] = " ".join(x for x in buf if x).strip()

    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if raw[:1] in (" ", "\t") and key:
            buf.append(raw.strip())
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", raw.strip())
        if not m:
            continue
        flush()
        key = m.group(1).lower()
        val = m.group(2).strip()
        if val in (">", "|", ">-", "|-"):
            val = ""
        buf = [val] if val else []
    flush()

    body = "\n".join(lines[end + 1:]).strip()
    return meta, body


def _truthy(v: str, default: bool = True) -> bool:
    if not v:
        return default
    return str(v).strip().lower() not in ("false", "0", "no", "off", "n", "否")


# ==========================================================================
# 数据
# ==========================================================================

@dataclass
class Skill:
    sid: str                       # 目录名，也是给模型用的 id
    name: str
    description: str
    when: str
    body: str
    folder: Path
    enabled: bool = True
    error: str = ""
    deps: str = ""                 # 运行依赖，比如「需要 OPENAI_API_KEY」
    meta: dict = field(default_factory=dict)

    @property
    def skill_file(self) -> Path:
        return self.folder / "SKILL.md"

    def attachments(self) -> list[str]:
        """文件夹里除 SKILL.md 之外的文本文档，相对路径。"""
        out: list[str] = []
        try:
            for p in sorted(self.folder.rglob("*")):
                if not p.is_file() or p.name == "SKILL.md":
                    continue
                if p.suffix.lower() in (".pyc", ".log", ".tmp"):
                    continue
                if any(part.startswith(".") or part == "__pycache__"
                       for part in p.relative_to(self.folder).parts):
                    continue
                out.append(p.relative_to(self.folder).as_posix())
                if len(out) >= MAX_ATTACH_LIST:
                    break
        except OSError:
            pass
        return out

    def one_line(self) -> str:
        d = " ".join((self.description or "").split())
        if len(d) > 160:
            d = d[:160] + "…"
        line = f"- {self.sid} · {self.name}：{d}"
        if self.deps:
            line += f"（⚠依赖：{self.deps}）"
        return line


# ==========================================================================
# 扫描
# ==========================================================================

_cache: dict = {"at": 0.0, "items": []}
_CACHE_TTL = 1.5


def skill_dir() -> Path:
    return config.SKILL_DIR


def _read_one(folder: Path) -> Skill | None:
    path = folder / "SKILL.md"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    meta, body = split_frontmatter(raw)
    sid = folder.name
    name = _strip_quotes(meta.get("name", "")) or sid
    desc = _strip_quotes(meta.get("description", ""))
    when = _strip_quotes(meta.get("when", ""))

    return Skill(
        sid=sid,
        name=name,
        description=desc,
        when=when,
        body=body,
        folder=folder,
        enabled=_truthy(str(meta.get("enabled", "")), True),
        deps=_strip_quotes(meta.get("deps", "")),
        meta=meta,
    )


def _scan() -> list[Skill]:
    """真正读盘的扫描。只解析文件，不判断 enabled（那个跟 config 有关）。"""
    root = skill_dir()
    items: list[Skill] = []

    try:
        root.mkdir(parents=True, exist_ok=True)
        for folder in sorted(root.iterdir()):
            if not folder.is_dir():
                continue
            if folder.name.startswith(".") or folder.name.startswith("_"):
                continue
            got = _read_one(folder)
            if got:
                items.append(got)
    except OSError:
        pass

    # 单文件写法：skills/foo.md
    try:
        for f in sorted(root.glob("*.md")):
            if f.name.lower() in ("readme.md", "index.md"):
                continue
            raw = f.read_text(encoding="utf-8", errors="replace")
            meta, body = split_frontmatter(raw)
            sid = f.stem
            items.append(Skill(
                sid=sid,
                name=_strip_quotes(meta.get("name", "")) or sid,
                description=_strip_quotes(meta.get("description", "")),
                when=_strip_quotes(meta.get("when", "")),
                body=body,
                folder=root,
                meta=meta,
            ))
    except OSError:
        pass

    return items


def list_skills(cfg: dict | None = None, fresh: bool = False) -> list[Skill]:
    """列出所有技能。

    - 文件解析结果缓存 1.5 秒（别每次渲染界面都读盘）
    - **enabled 每次都按当前 cfg 现算**，不缓存。否则 GUI 关了一个技能，
      后台线程拿着旧的 cfg 还能从缓存里摸出「它是开的」。
    - 返回的是副本，外面的改动不会污染缓存。
    """
    now = time.time()
    if fresh or not _cache["items"] or now - _cache["at"] >= _CACHE_TTL:
        _cache["items"] = _scan()
        _cache["at"] = now

    disabled = set((cfg or config.load_config()).get("skills_disabled") or [])
    out: list[Skill] = []
    for s in _cache["items"]:
        base = _truthy(str(s.meta.get("enabled", "")), True)
        s2 = dataclasses.replace(s, enabled=bool(base and s.sid not in disabled))
        out.append(s2)

    out.sort(key=lambda s: (not s.enabled, s.sid.lower()))
    return out


def get_skill(sid: str, cfg: dict | None = None) -> Skill | None:
    sid = (sid or "").strip()
    if not sid:
        return None
    low = sid.lower()
    for s in list_skills(cfg, fresh=True):
        if s.sid.lower() == low or s.name.lower() == low:
            return s
    return None


def active_skills(cfg: dict) -> list[Skill]:
    return [s for s in list_skills(cfg) if s.enabled]


# ==========================================================================
# 开关
# ==========================================================================

def set_enabled(cfg: dict, sid: str, on: bool) -> bool:
    """改 config 里的 skills_disabled 并落盘。cfg 是调用方持有的配置对象。"""
    off = [x for x in (cfg.get("skills_disabled") or []) if x != sid]
    if not on:
        off.append(sid)
    cfg["skills_disabled"] = off
    try:
        config.save_config(cfg)
    except OSError:
        pass
    _cache["at"] = 0.0
    return True


# ==========================================================================
# 给系统提示词用的清单
# ==========================================================================

def catalog(cfg: dict) -> str:
    """塞进系统提示词的那一段。

    三种模式（config 的 skills_catalog 字段，默认 off）：
      off   —— 提示词里不放清单，只有一句「有技能、自己去查」的提示。
               模型用 {"tool": "skill"}（不带 name）拿清单，再按 id 读正文。
      names —— 只放一串技能 id，不放描述。折中方案，几百字符。
      full  —— 旧版：每个技能一行 id+名字+描述。52 个技能约 9KB，慎用。

    没有启用的技能时，off 模式返回空串，names/full 返回空串。
    """
    if not cfg.get("skills_enabled", True):
        return ""
    items = active_skills(cfg)
    if not items:
        return ""

    mode = (cfg.get("skills_catalog") or "off").lower()

    if mode == "full":
        rows = [s.one_line() for s in items]
        return (
            "【可用技能 Skill】\n"
            "下面这些是本地预置好的技能包，每个都是一份「怎么做这件事」的说明书。\n"
            + "\n".join(rows) + "\n"
            "\n"
            "用法（重要，按这个顺序来）：\n"
            "1. 判断当前任务命中哪个技能，先用 {\"tool\": \"skill\", \"name\": \"技能id\"} "
            "把它的正文读进来，再照着做。**不要没读就凭标题瞎猜步骤。**\n"
            "2. 技能正文里列了附件（references/、scripts/）时，需要哪个再用 "
            "{\"tool\": \"skill\", \"name\": \"技能id\", \"file\": \"相对路径\"} 读；"
            "附件很大，别一次全读。\n"
            "3. 技能自带的脚本用 {\"tool\": \"skill_run\", \"name\": \"技能id\", "
            "\"cmd\": \"python scripts/xxx.py 参数\"} 在技能目录里跑。\n"
            "4. 技能只是参考流程，不是权限升级：它让你做的事仍然受底线和 confirmation 约束。\n"
            "5. 没有技能匹配当前任务就正常干活，别硬套。"
        )

    ids = "、".join(s.sid for s in items)

    if mode == "names":
        return (
            "【可用技能 Skill】\n"
            f"本机装了 {len(items)} 个技能包，id 如下：{ids}。\n"
            "任务可能命中某个技能时，先用 {\"tool\": \"skill\", \"name\": \"技能id\"} 读入"
            "它的说明书再照着做（说明书写明步骤，别凭 id 瞎猜）。"
            "命中不了就正常干活。"
        )

    # off（默认）：提示词里完全不列技能，靠模型自己来查
    return (
        "【技能 Skill】\n"
        f"本机 skills/ 目录装了 {len(items)} 个技能包（各领域的操作说明书，"
        "清单不在这里，别凭空猜 id）。\n"
        "当任务可能命中某个技能（处理 pdf / 发微信 / 语音合成 / 系统清理 / 写作润色"
        "这类有固定套路的活），先调 {\"tool\": \"skill\"}（不带 name）拿技能清单，"
        "每个技能带一句话描述和依赖说明；命中哪个，再用 {\"tool\": \"skill\", "
        "\"name\": \"技能id\"} 读入正文照着做。没命中就正常干活，不用查。"
    )


# ==========================================================================
# 读取
# ==========================================================================

def load_text(cfg: dict, sid: str) -> tuple[bool, str]:
    """把技能正文交出去。"""
    sk = get_skill(sid, cfg)
    if not sk:
        return False, (f"【没有这个技能】{sid!r}。"
                       "先用 {\"tool\": \"skill\"}（不带 name）查技能清单，别自己编 id。")

    head = [f"【技能：{sk.name}（id={sk.sid}）】",
            f"目录：{sk.folder}"]
    if sk.deps:
        head.append(f"运行依赖：{sk.deps}（缺依赖就如实告诉用户装不上，别硬跑。）")
    files = sk.attachments()
    if files:
        head.append("附件：" + "、".join(files))
    if sk.when:
        head.append(f"适用场景：{sk.when}")
    head.append("")

    body = sk.body or "（这个技能没有正文，只有元信息。）"
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n...[技能正文过长，已截断]...\n（需要细节就按需读附件）"

    tail = ("\n\n读完就照着做。真要用到脚本，用 skill_run 在技能目录里执行，"
            "别自己拼绝对路径。")
    return True, "\n".join(head) + body + tail


def read_attachment(cfg: dict, sid: str, rel: str) -> tuple[bool, str]:
    """读技能里的一个附件。越界一律拒绝。"""
    sk = get_skill(sid, cfg)
    if not sk:
        return False, f"【没有这个技能】{sid!r}。"

    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return False, "【参数错误】file 不能为空。"

    try:
        target = (sk.folder / rel).resolve()
        root = sk.folder.resolve()
    except OSError as e:
        return False, f"【路径错误】{e}"

    if target != root and root not in target.parents:
        return False, "【拒绝】附件路径跑到技能目录外面了，不给读。"
    if not target.is_file():
        return False, f"【没有这个文件】{rel}。技能目录里没有它。"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"【读取失败】{type(e).__name__}: {e}"

    if len(text) > MAX_ATTACH:
        text = text[:MAX_ATTACH] + f"\n\n...[已截断，原文 {len(text)} 字符]..."
    return True, f"【{sk.sid} / {rel}】\n{text}"


def ensure_scaffold() -> None:
    """skills/ 不存在就建一个，顺便放一份 README 讲怎么写。"""
    root = skill_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        readme = root / "README.md"
        if not readme.exists():
            readme.write_text(SCAFFOLD_README, encoding="utf-8")
    except OSError:
        pass


SCAFFOLD_README = """# skills/

每个子文件夹是一个技能包，`SKILL.md` 必须有：

```
skills/
  my-skill/
    SKILL.md          # 元信息 + 正文说明
    scripts/run.py    # 可选，skill_run 调用
    references/a.md   # 可选，按需读取
```

`SKILL.md` 开头是元信息：

```
---
name: 我的技能
description: 一句话说明这个技能干什么、什么时候该用（会进系统提示词）
when: 更具体的触发条件，可以留空
enabled: true
---
正文：分步骤写清楚怎么做，命令原样贴出来。
```

规矩：

- `description` 写清「干什么 + 什么时候用」，模型靠这句话决定要不要加载。别写成广告词。
- 正文别抄一堆通用常识，只写这个技能特有、模型自己想不到的东西。
- 需要跑脚本就放 `scripts/`，正文里写调用方式。
- 状态存在 `data/config.json` 的 `skills_disabled` 里，界面上能直接开关。
"""
