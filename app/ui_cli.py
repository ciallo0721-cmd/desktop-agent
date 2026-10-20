# -*- coding: utf-8 -*-
"""CLI 模式：在命令行里跟 agent 对话。

    python agent.py cli
    python agent.py --mode cli

- 流式打字机效果
- Ctrl+C 打断当前这一轮（不会退出程序），再按一次才退出
- 高危命令会停下来问你 y/N
"""
from __future__ import annotations

import os
import sys
import time

from . import agent_core, config, images, skills
from .session import Session

# ---------------------------------------------------------------- 终端控制

_C = {
    "rst": "\033[0m", "dim": "\033[2m", "b": "\033[1m",
    "red": "\033[31m", "grn": "\033[32m", "yel": "\033[33m",
    "blu": "\033[34m", "cya": "\033[36m", "gry": "\033[90m",
}


def _color(key: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{_C.get(key, '')}{text}{_C['rst']}"


def _setup_terminal() -> None:
    """UTF-8 + 打开 ANSI 转义。Windows 老控制台默认不开 VT。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


# ---------------------------------------------------------------- 渲染

class _Printer:
    def __init__(self):
        self.buf = ""
        self.shown = ""
        self.open_line = False

    def begin(self) -> None:
        self.buf = ""
        self.shown = ""
        self.open_line = False

    def feed(self, piece: str) -> None:
        self.buf += piece
        shown = agent_core.strip_streaming(self.buf)
        if shown.startswith(self.shown):
            add = shown[len(self.shown):]
            if add:
                sys.stdout.write(add)
                sys.stdout.flush()
                self.open_line = not add.endswith("\n")
        self.shown = shown

    def end(self, full: str) -> None:
        text = agent_core.strip_actions(full)
        if text.startswith(self.shown):
            rest = text[len(self.shown):]
            if rest:
                sys.stdout.write(rest)
        elif text:
            if self.open_line:
                sys.stdout.write("\n")
            sys.stdout.write(text)
        if self.open_line or text:
            sys.stdout.write("\n")
        sys.stdout.flush()
        self.open_line = False

    def newline(self) -> None:
        if self.open_line:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.open_line = False


def _clip(text: str, limit: int = 8) -> str:
    lines = (text or "").strip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit]) + f"\n...（还有 {len(lines) - limit} 行）"


# ---------------------------------------------------------------- 命令

HELP = """\
{title}

/help            看这份帮助
/new             开一个新对话
/list            列出历史对话
/use N           切到第 N 个对话
/del N           删掉第 N 个对话（移到 data/trash）
/stop            停掉正在跑的这一轮（等于 Ctrl+C）
/mode [模式]     查看或切换 bash | screen | both
/skill           列出技能；/skill on <id> 开，/skill off <id> 关，/skill cat <id> 看正文
/img [路径]      附加一张图；不带路径 = 从剪贴板取；/img - 清空
/clear           清屏
/exit            退出

直接输入内容就发给 agent。Ctrl+C 打断当前回复，连按两次退出。
""".format(title=_color("b", "命令"))


def _cmd_mode(s: Session, arg: str) -> None:
    cur = s.cfg.get("agent_mode", "both")
    if not arg:
        print(f"当前交互方式：{_color('cya', cur)}（可选 bash / screen / both）")
        return
    arg = arg.strip().lower()
    if arg not in ("bash", "screen", "both"):
        print(_color("red", "只能是 bash / screen / both"))
        return
    s.cfg["agent_mode"] = arg
    cfg_all = config.load_config()
    cfg_all["agent_mode"] = arg
    config.save_config(cfg_all)
    print(f"已切到 {_color('cya', arg)}")


def _cmd_skill(s: Session, arg: str) -> None:
    skills.ensure_scaffold()
    parts = arg.split(None, 1)
    sub = parts[0].lower() if parts else ""
    target = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("on", "off"):
        if not target:
            print(_color("red", "用法：/skill on <id>"))
            return
        sk = skills.get_skill(target, s.cfg)
        if not sk:
            print(_color("red", f"没有这个技能：{target}"))
            return
        skills.set_enabled(s.cfg, sk.sid, sub == "on")
        print(_color("gry", f"{sk.sid} 已{'启用' if sub == 'on' else '关闭'}"))
        return

    if sub in ("cat", "show", "read"):
        if not target:
            print(_color("red", "用法：/skill cat <id>"))
            return
        ok, text = skills.load_text(s.cfg, target)
        print(text if ok else _color("red", text))
        return

    items = skills.list_skills(s.cfg, fresh=True)
    if not items:
        print(_color("gry", f"还没有技能。目录：{config.SKILL_DIR}"))
        return
    master = "开" if s.cfg.get("skills_enabled", True) else "关"
    print(_color("b", f"技能（总开关 {master}）"))
    for sk in items:
        flag = _color("grn", "开") if sk.enabled else _color("gry", "关")
        desc = " ".join((sk.description or "").split())[:52]
        print(f"  [{flag}] {_color('cya', sk.sid):<22} {desc}")
    print(_color("gry", f"  目录：{config.SKILL_DIR}  ·  /skill on|off|cat <id>"))


def _cmd_list(s: Session) -> None:
    convs = s.conversations()
    if not convs:
        print(_color("gry", "还没有对话"))
        return
    print(_color("b", "历史对话"))
    for i, c in enumerate(convs, 1):
        mark = "*" if (s.conv and c.id == s.conv.id) else " "
        ts = time.strftime("%m-%d %H:%M", time.localtime(c.updated))
        print(f" {mark} {i:>2}. {_color('grn', c.title[:30]):<32} {c.user_turns():>3} 轮  {ts}")


def _cmd_img(s: Session, arg: str, pending: list) -> None:
    if arg in ("-", "clear", "清空"):
        n = len(pending)
        pending.clear()
        print(_color("gry", f"清空 {n} 张待发图片"))
        return

    if not arg:
        if pending:
            for i, r in enumerate(pending, 1):
                print(f" {i}. {r}")
            print(_color("gry", "/img - 清空；直接发消息会一起带上"))
            return
        try:
            rel = images.from_clipboard(s.conv.id)
        except images.ImageError as e:
            print(_color("red", str(e)))
            return
        if rel:
            pending.append(rel)
            print(_color("grn", f"从剪贴板收了 1 张（共 {len(pending)} 张）"))
        else:
            print(_color("gry", r"剪贴板里没有图。用法：/img C:\path\to\pic.png"))
        return

    try:
        rel = images.save(arg, s.conv.id)
    except images.ImageError as e:
        print(_color("red", str(e)))
        return
    pending.append(rel)
    print(_color("grn", f"已附加（共 {len(pending)} 张）：{rel}"))


def _handle(s: Session, line: str, printer: _Printer, pending: list) -> bool:
    """返回 False 表示要退出。"""
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit", "/q"):
        return False
    if cmd == "/help":
        print(HELP)
    elif cmd == "/new":
        s.new()
        if pending:
            pending.clear()
        print(_color("gry", "新对话已开"))
    elif cmd == "/list":
        _cmd_list(s)
    elif cmd in ("/use", "/switch"):
        try:
            idx = int(arg) - 1
        except ValueError:
            print(_color("red", "用法：/use 2"))
            return True
        if s.switch_by_index(idx):
            if pending:
                pending.clear()
            print(_color("gry", f"已切到：{s.conv.title}"))
            for m in s.conv.visible():
                role = "你" if m.get("role") == "user" else "AI"
                text = m.get("content", "")
                if role == "AI":
                    text = agent_core.strip_actions(text)
                n_img = len(m.get("images") or [])
                tag = f"[图片×{n_img}] " if n_img else ""
                print(_color("gry", f"{role} › {tag}{text[:80]}"))
        else:
            print(_color("red", "没有这个序号"))
    elif cmd == "/del":
        try:
            idx = int(arg) - 1
        except ValueError:
            print(_color("red", "用法：/del 2"))
            return True
        convs = s.conversations()
        if 0 <= idx < len(convs):
            s.delete(convs[idx])
            print(_color("gry", f"删了：{convs[idx].title}"))
        else:
            print(_color("red", "没有这个序号"))
    elif cmd == "/stop":
        s.stop()
        print(_color("gry", "已请求停止"))
    elif cmd == "/mode":
        _cmd_mode(s, arg)
    elif cmd == "/skill":
        _cmd_skill(s, arg)
    elif cmd == "/img":
        _cmd_img(s, arg, pending)
    elif cmd == "/clear":
        os.system("cls" if os.name == "nt" else "clear")
    else:
        print(_color("red", f"不认识的命令：{cmd}，/help 看看"))
    return True


# ---------------------------------------------------------------- 主循环

def _chat(s: Session, text: str, printer: _Printer, imgs: list) -> None:
    ok, err = s.send(text, imgs)
    if not ok:
        print(_color("red", err))
        return

    if imgs:
        print(_color("gry", f"（附带 {len(imgs)} 张图片）"))

    printer.begin()
    print(_color("blu", "AI › "), end="", flush=True)
    idle = False
    while not idle:
        evts = s.poll()
        if not evts:
            time.sleep(0.02)
            continue
        for e in evts:
            kind = e.get("type")

            if kind == "_idle":
                idle = True
                continue

            if kind == "delta":
                printer.feed(e.get("text", ""))

            elif kind == "assistant_end":
                printer.end(e.get("text", ""))

            elif kind == "tool_start":
                printer.newline()
                print(_color("yel", f"  ⟩ {e.get('name')}  {e.get('detail', '')}"))

            elif kind == "tool_result":
                body = _clip(e.get("text", ""), 8)
                if body:
                    for ln in body.splitlines():
                        print(_color("gry", f"    {ln}"))

            elif kind == "ask":
                q = (e.get("text") or "").strip()
                if q:
                    printer.newline()
                    print(_color("cya", f"AI 想问：{q}"))

            elif kind == "confirm":
                printer.newline()
                print(_color("red", "⚠ 高危命令"))
                print(f"  命令：{e.get('cmd')}")
                print(f"  原因：{e.get('reason')}")
                try:
                    ans = input(_color("yel", "  允许执行？[y/N] ")).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                s.answer_confirm(ans in ("y", "yes", "1", "是"))

            elif kind == "error":
                printer.newline()
                print(_color("red", f"错误：{e.get('msg')}"))

            elif kind == "cancel":
                printer.newline()
                print(_color("gry", "（已停止）"))

            elif kind == "mem_ok":
                print(_color("gry", "（记忆已更新）"))

    printer.newline()


def run(cfg: dict | None = None) -> int:
    _setup_terminal()
    s = Session(cfg)
    printer = _Printer()
    pending: list[str] = []

    print(_color("b", "Desktop Agent · CLI"))
    print(_color("gry", f"模型 {s.cfg.get('main_api', {}).get('model') or '未配置'}"
                        f"  ·  方式 {s.cfg.get('agent_mode', 'both')}"
                        f"  ·  对话 {s.conv.title}"))
    print(_color("gry", "输入 /help 看命令。Ctrl+C 打断回复。"))
    print()

    if not config.is_configured(s.cfg):
        print(_color("red", "！没有配置主 API Key，发消息会失败。先跑 first.bat。"))
        print()

    while True:
        tag = f"你[{len(pending)}图] › " if pending else "你 › "
        try:
            line = input(_color("grn", tag))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            try:
                if not _handle(s, line, printer, pending):
                    break
            except (EOFError, KeyboardInterrupt):
                break
            continue

        try:
            imgs = list(pending)
            pending.clear()
            _chat(s, line, printer, imgs)
        except KeyboardInterrupt:
            s.stop()
            print(_color("gry", "（正在停止…）"))
            # 把剩余事件抽完，别让队列憋着
            while s.busy:
                for e in s.poll():
                    if e.get("type") == "_idle":
                        s.busy = False
                time.sleep(0.05)

    print(_color("gry", "再见。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
