# -*- coding: utf-8 -*-
"""TUI 模式：在命令行里开一个「窗口」。

    python agent.py tui

布局是 ASCII 边框画的：左边会话列表，右边聊天，底下输入框。
方向键选、PgUp/PgDn 翻，全程只靠 ANSI 转义 + msvcrt，不依赖 curses
（Windows 上装 windows-curses 是额外的坑）。

按键：
    ↑ ↓          在会话列表里选
    ← →          切换焦点（列表 / 输入框）
    Enter        发送
    PgUp PgDn    聊天区翻页
    Ctrl+N       新对话
    Ctrl+C       复制最后一条 AI 回复
    Ctrl+V       粘贴（终端自己处理，粘进来就行）
    Ctrl+G       把剪贴板里的图片收进待发列表
    /img 路径    附加一张图片（/img 单独敲 = 从剪贴板取，/img - 清空）
    Ctrl+End     停止正在跑的这一轮（Ctrl+X 也行）
    Esc          清空输入
    Ctrl+Q       退出（连按两次，防止输入法卡 Ctrl 时误触）
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
import unicodedata
from pathlib import Path

from . import agent_core, config, images
from .session import Session

# ---------------------------------------------------------------- 终端底层

STD_INPUT = -10
STD_OUTPUT = -11

ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_QUICK_EDIT = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080

C = {
    "rst": "\033[0m", "b": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "grn": "\033[32m", "yel": "\033[33m",
    "blu": "\033[34m", "mag": "\033[35m", "cya": "\033[36m",
    "wht": "\033[37m", "gry": "\033[90m",
    "rev": "\033[7m",
}


class Term:
    # 控制台/管道被关掉时（WinError 233），写 stdout 会抛 OSError。
    # 置位后所有输出变成静默失败，主循环据此优雅退出，不再崩出 traceback。
    dead = False

    def __init__(self) -> None:
        self.k32 = ctypes.windll.kernel32 if os.name == "nt" else None
        self.saved_in = None
        self.saved_out = None

    # ---------------- 安全输出 ----------------
    @staticmethod
    def write(s: str) -> bool:
        if Term.dead:
            return False
        try:
            sys.stdout.write(s)
            return True
        except (OSError, ValueError):
            Term.dead = True
            return False

    @staticmethod
    def flush() -> bool:
        if Term.dead:
            return False
        try:
            sys.stdout.flush()
            return True
        except (OSError, ValueError):
            Term.dead = True
            return False

    # ---------------- 模式 ----------------
    def setup(self) -> None:
        if not self.k32:
            return
        try:
            # 输出必须是 UTF-8，否则中文和边框全乱
            self.k32.SetConsoleOutputCP(65001)
            # 输入不能动：中文输入法依赖系统 ANSI 代码页（936）做候选转换，
            # 强行设成 65001 会让 IME 在 raw 控制台里上屏时把 conhost 搞崩。
            # msvcrt.getwch() 走的是 ReadConsoleW，读的是宽字符，跟代码页无关。
            acp = self.k32.GetACP()
            if acp:
                self.k32.SetConsoleCP(acp)
        except Exception:
            pass

        try:
            h = self.k32.GetStdHandle(STD_INPUT)
            mode = ctypes.c_uint32()
            if self.k32.GetConsoleMode(h, ctypes.byref(mode)):
                self.saved_in = mode.value
                new = mode.value
                new &= ~ENABLE_PROCESSED_INPUT      # Ctrl+C 当普通键给我
                new &= ~ENABLE_LINE_INPUT           # 逐字符读
                new &= ~ENABLE_ECHO_INPUT           # 别自动回显
                new &= ~ENABLE_QUICK_EDIT           # 否则鼠标一点就卡住输出
                new |= ENABLE_EXTENDED_FLAGS
                self.k32.SetConsoleMode(h, new)
        except Exception:
            pass

        try:
            h = self.k32.GetStdHandle(STD_OUTPUT)
            mode = ctypes.c_uint32()
            if self.k32.GetConsoleMode(h, ctypes.byref(mode)):
                self.saved_out = mode.value
                self.k32.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass

    def restore(self) -> None:
        if not self.k32:
            return
        try:
            if self.saved_in is not None:
                self.k32.SetConsoleMode(
                    self.k32.GetStdHandle(STD_INPUT), self.saved_in)
            if self.saved_out is not None:
                self.k32.SetConsoleMode(
                    self.k32.GetStdHandle(STD_OUTPUT), self.saved_out)
        except Exception:
            pass

    # ---------------- 尺寸 ----------------
    @staticmethod
    def size() -> tuple[int, int]:
        try:
            s = shutil.get_terminal_size(fallback=(100, 30))
            return s.columns, s.lines
        except Exception:
            return 100, 30

    # ---------------- 光标 / 屏 ----------------
    @staticmethod
    def hide_cursor() -> None:
        Term.write("\033[?25l")

    @staticmethod
    def show_cursor() -> None:
        Term.write("\033[?25h")

    @staticmethod
    def goto(row: int, col: int = 1) -> None:
        Term.write(f"\033[{row};{col}H")

    @staticmethod
    def clear() -> None:
        Term.write("\033[2J\033[H")

    # ---------------- 键盘 ----------------
    @staticmethod
    def getkey():
        """返回 (kind, value)。kind: char / special / esc / None

        esc 是 ESC 开头的 CSI 序列。Windows Terminal 把 Ctrl+End 发成
        ESC[1;5F，不整个吞掉的话 [1;5F 会被当普通字符打进输入框。
        """
        import msvcrt

        try:
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                return ("special", msvcrt.getwch())
            if ch == "\x1b":
                seq = ""
                t0 = time.time()
                while time.time() - t0 < 0.03 and msvcrt.kbhit():
                    seq += msvcrt.getwch()
                if seq:
                    return ("esc", seq)
                return ("char", "\x1b")
            return ("char", ch)
        except (OSError, ValueError):
            # 输入句柄也没了（窗口被关 / conhost 崩），跟输出一样处理
            Term.dead = True
            return None


# ---------------------------------------------------------------- 宽度计算

def _cwidth(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def dwidth(text: str) -> int:
    return sum(_cwidth(c) for c in text)


def cut(text: str, width: int) -> str:
    """按显示宽度截断，中文算 2 格。"""
    if width <= 0:
        return ""
    out, cur = [], 0
    for ch in text:
        w = _cwidth(ch)
        if cur + w > width:
            break
        out.append(ch)
        cur += w
    return "".join(out)


def pad(text: str, width: int) -> str:
    """截断 + 右侧补空格，保证整行铺满。"""
    body = cut(text, width)
    return body + " " * max(0, width - dwidth(body))


def wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行。"""
    if width <= 2:
        return [""]
    out: list[str] = []
    for raw in (text or "").split("\n"):
        if not raw:
            out.append("")
            continue
        cur, cw = [], 0
        for ch in raw:
            w = _cwidth(ch)
            if cw + w > width:
                out.append("".join(cur))
                cur, cw = [ch], w
            else:
                cur.append(ch)
                cw += w
        out.append("".join(cur))
    return out or [""]


# ---------------------------------------------------------------- 主程序

LEFT_W = 22          # 左侧会话列表宽度
HINTS = (" ↑↓ 选会话   ←→ 切焦点   Enter 发送   PgUp/PgDn 翻页   Ctrl+G 收剪贴板图 ",
         "Ctrl+N 新对话   Ctrl+C 复制   Ctrl+End 停止   Ctrl+Q×2 退出 ")


class TuiApp:
    def __init__(self, cfg: dict | None = None):
        self.term = Term()
        self.sess = Session(cfg)
        self.input = ""
        self.pending: list[str] = []   # 待发图片（/img 或 Ctrl+G 加进来）
        self.focus = "input"          # input | list
        self.sel = 0                  # 左侧选中下标
        self.scroll = 0               # 聊天区滚动行
        self.follow = True            # 是否自动跟到底
        self.status = ""
        self.status_until = 0.0
        self.dirty = True
        self.running = True
        self._q_ts = 0.0              # 上次按 Ctrl+Q 的时刻（防误触）

        # 渲染缓存
        self.lines: list[tuple[str, str]] = []   # (颜色, 文本)
        self.stream_buf = ""
        self.cur_ai = None
        self.cur_tool = None
        self.last_ai = ""
        self._cache_key = None

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def say(self, text: str, color: str = "gry", secs: float = 2.5) -> None:
        self.status = text
        self.status_until = time.time() + secs
        self.dirty = True

    def convs(self):
        return self.sess.conversations()

    def push(self, color: str, text: str) -> None:
        self.lines.append((color, text))
        self.dirty = True

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def handle_events(self) -> None:
        for e in self.sess.poll():
            kind = e.get("type")

            if kind == "_idle":
                self.stream_buf = ""
                self.cur_ai = None
                self.cur_tool = None
                continue

            if kind == "thinking":
                if self.cur_ai is None:
                    self.push("gry", "AI › 正在思考…")
                    self.cur_ai = len(self.lines) - 1
                continue

            if kind == "delta":
                self.stream_buf += e.get("text", "")
                shown = agent_core.strip_streaming(self.stream_buf)
                if self.cur_ai is None:
                    self.push("wht", "AI › " + shown)
                    self.cur_ai = len(self.lines) - 1
                else:
                    self.lines[self.cur_ai] = ("wht", "AI › " + (shown or "…"))
                self.follow = True
                self.dirty = True
                continue

            if kind == "assistant_end":
                text = agent_core.strip_actions(e.get("text", ""))
                self.stream_buf = ""
                if self.cur_ai is not None:
                    if text:
                        self.lines[self.cur_ai] = ("wht", "AI › " + text)
                    else:
                        self.lines.pop(self.cur_ai)
                elif text:
                    self.push("wht", "AI › " + text)
                if text:
                    self.last_ai = text
                self.cur_ai = None
                continue

            if kind == "tool_start":
                if e.get("name") == "done":
                    continue
                self.push("yel", f" ⚙ {e.get('name')}  {e.get('detail', '')}")
                self.cur_tool = len(self.lines) - 1
                continue

            if kind == "tool_result":
                body = (e.get("text") or "").strip()
                rows = body.splitlines()[:6]
                if self.cur_tool is not None:
                    tag = self.lines[self.cur_tool][1].split("  ")[0]
                    extra = [("gry", "   " + ln[:200]) for ln in rows]
                    self.lines[self.cur_tool:self.cur_tool + 1] = [
                        ("yel", tag + "  ✓")] + extra
                elif rows:
                    self.lines.extend(("gry", "   " + ln[:200]) for ln in rows)
                self.cur_tool = None
                self.dirty = True
                continue

            if kind == "ask":
                q = (e.get("text") or "").strip()
                if q:
                    self.push("cya", "AI 想问 › " + q)
                continue

            if kind == "error":
                self.push("red", "✗ " + (e.get("msg") or "未知错误"))
                continue

            if kind == "cancel":
                self.push("gry", "（已停止）")
                continue

            if kind == "confirm":
                self._ask_confirm(e.get("cmd", ""), e.get("reason", ""))
                continue

            if kind == "mem_ok":
                self.say("记忆已更新")
                continue

            if kind == "mem_fail":
                self.say("记忆更新失败：" + str(e.get("msg"))[:40], "red")
                continue

    def _ask_confirm(self, cmd: str, reason: str) -> None:
        """高危命令：临时退出 TUI，用普通输入问一句再回来。"""
        self.term.show_cursor()
        self.term.restore()      # input() 要行输入模式，先还回去
        Term.goto(1, 1)
        Term.clear()
        print(C["red"] + C["b"] + "⚠ AI 想要执行一条高危命令" + C["rst"])
        print()
        print("  命令：" + cmd[:400])
        print("  原因：" + reason[:200])
        print()
        try:
            ans = input("允许执行？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        self.sess.answer_confirm(ans in ("y", "yes", "1", "是"))
        Term.clear()
        self.term.setup()
        self.dirty = True

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def do_send(self) -> None:
        text = self.input.strip()
        if not text:
            return
        if text.startswith("/img"):
            self._cmd_img(text[4:].strip())
            self.input = ""
            self.dirty = True
            return

        imgs = list(self.pending)
        ok, err = self.sess.send(text, imgs)
        if not ok:
            self.say(err, "red", 4)
            return
        self.pending.clear()
        self.input = ""
        tag = f"[{len(imgs)}图] " if imgs else ""
        self.push("cya", "你 › " + tag + text)
        self.follow = True
        self.dirty = True

    def _cmd_img(self, arg: str) -> None:
        """命令行里没法选文件，只能给路径或从剪贴板抓。"""
        if arg in ("-", "clear", "清空"):
            n = len(self.pending)
            self.pending.clear()
            self.say(f"清空 {n} 张待发图片")
            return

        if not arg:
            if self.pending:
                names = "、".join(Path(r).name for r in self.pending)
                self.say(f"待发 {len(self.pending)} 张：{names}")
                return
            try:
                rel = images.from_clipboard(self.sess.conv.id)
            except images.ImageError as e:
                self.say(str(e), "red")
                return
            if rel:
                self.pending.append(rel)
                self.say(f"从剪贴板收了 1 张（共 {len(self.pending)} 张）")
            else:
                self.say(r"剪贴板里没有图。用法：/img C:\path\pic.png  或先截图再 Ctrl+G",
                         "gry")
            return

        try:
            rel = images.save(arg, self.sess.conv.id)
        except images.ImageError as e:
            self.say(str(e), "red")
            return
        self.pending.append(rel)
        self.say(f"已附加（共 {len(self.pending)} 张）：{Path(rel).name}")

    def _grab_clip(self) -> None:
        """Ctrl+G：直接把剪贴板里的图收进待发列表。"""
        try:
            rel = images.from_clipboard(self.sess.conv.id)
        except images.ImageError as e:
            self.say(str(e), "red")
            return
        if not rel:
            self.say("剪贴板里没有图片（先 Win+Shift+S 截一张）", "gry")
            return
        self.pending.append(rel)
        self.say(f"收了一张图（共 {len(self.pending)} 张），Enter 一起发出去")

    def do_stop(self) -> None:
        if self.sess.busy:
            self.sess.stop()
            self.say("正在停止…")
        else:
            self.say("现在没有在跑的任务")

    def do_copy(self) -> None:
        text = self.last_ai
        if not text:
            self.say("还没有可以复制的 AI 回复")
            return
        try:
            import pyperclip

            pyperclip.copy(text)
            self.say("已复制最后一条 AI 回复")
        except Exception as e:
            self.say("复制失败：" + str(e)[:40], "red")

    def do_new(self) -> None:
        self.sess.new()
        self.lines = []
        self.scroll = 0
        self.last_ai = ""
        self.pending.clear()
        self.say("新对话已开")
        self.dirty = True

    def do_switch(self, idx: int) -> None:
        convs = self.convs()
        if not (0 <= idx < len(convs)):
            return
        conv = convs[idx]
        self.sel = idx
        self.sess.switch(conv)
        self.pending.clear()
        self.lines = []
        self.scroll = 0
        self.follow = False
        for m in conv.visible():
            if m.get("tool_result"):
                self.push("yel", f" ⚙ {m.get('tool', 'tool')}")
                for ln in (m.get("content") or "").splitlines()[:5]:
                    self.push("gry", "   " + ln[:200])
            else:
                role = m.get("role", "user")
                text = m.get("content", "")
                if role == "assistant":
                    text = agent_core.strip_actions(text)
                    self.last_ai = text
                n_img = len(m.get("images") or [])
                tag = f"[{n_img}图] " if n_img else ""
                self.push("cya" if role == "user" else "wht",
                          ("你 › " if role == "user" else "AI › ") + tag + text)
        self.dirty = True

    # ------------------------------------------------------------------
    # 按键
    # ------------------------------------------------------------------
    def on_key(self, kind: str, val: str) -> bool:
        """返回 False 表示退出。"""
        if kind == "esc":
            seq = val
            if seq.endswith("F") and ";5" in seq:      # Ctrl+End → 停止
                self.do_stop()
            elif seq.endswith("H") and ";5" in seq:    # Ctrl+Home
                self.scroll = 0
                self.follow = False
                self.dirty = True
            elif seq == "[A":
                self._move(-1)
            elif seq == "[B":
                self._move(1)
            elif seq == "[C":
                self.focus = "input"
                self.dirty = True
            elif seq == "[D":
                self.focus = "list"
                self.dirty = True
            elif seq == "[5~":
                self._scroll(-self._page())
            elif seq == "[6~":
                self._scroll(self._page())
            # 其余不认识的一律吞掉，别污染输入框
            return True

        if kind == "special":
            if val == "H":                       # ↑
                self._move(-1)
            elif val == "P":                     # ↓
                self._move(1)
            elif val == "K":                     # ←
                self.focus = "list"
                self.dirty = True
            elif val == "M":                     # →
                self.focus = "input"
                self.dirty = True
            elif val == "I":                     # PgUp
                self._scroll(-(self._page() or 5))
            elif val == "Q":                     # PgDn
                self._scroll(self._page() or 5)
            elif val == "G":                     # Home
                self.scroll = 0
                self.follow = False
                self.dirty = True
            elif val == "O":                     # End
                self.follow = True
                self.dirty = True
            elif val in ("\xa3", "S"):           # Ctrl+End 的兜底 / Delete
                if val == "\xa3":
                    self.do_stop()
            return True

        ch = val
        if ch == "\x03":                         # Ctrl+C → 复制
            self.do_copy()
        elif ch == "\x11":                       # Ctrl+Q
            # 输入法坑：Ctrl+Space 切输入法时，Ctrl 的抬起事件常被吞掉，
            # 紧接着打的第一个字母（比如 qian 的 q）就变成 Ctrl+Q。
            # 所以：输入框有内容时一律不认这个键。
            if self.input.strip():
                self.say("Ctrl+Q 已忽略（输入框有内容，可能输入法卡住了 Ctrl）")
                return True
            now = time.time()
            if now - self._q_ts < 1.5:
                return False
            self._q_ts = now
            self.say("再按一次 Ctrl+Q 退出")
        elif ch == "\x0e":                       # Ctrl+N
            if self.input.strip():
                self.say("Ctrl+N 已忽略（输入框有内容）")
            else:
                self.do_new()
        elif ch == "\x07":                       # Ctrl+G → 收剪贴板里的图
            self._grab_clip()
        elif ch == "\x18":                       # Ctrl+X → 停止
            self.do_stop()
        elif ch == "\x16":                       # Ctrl+V（有的终端会发过来）
            self.say("直接按 Ctrl+V 粘贴就行")
        elif ch in ("\r", "\n"):
            if self.focus == "list":
                convs = self.convs()
                if 0 <= self.sel < len(convs):
                    self.do_switch(self.sel)
            else:
                self.do_send()
        elif ch == "\x7f" or ch == "\b":         # 退格
            self.input = self.input[:-1]
            self.dirty = True
        elif ch == "\x1b":                       # Esc
            self.input = ""
            self.dirty = True
        elif ch == "\t":
            self.focus = "list" if self.focus == "input" else "input"
            self.dirty = True
        elif ch >= " ":
            self.input += ch
            self.dirty = True
        return True

    def _move(self, step: int) -> None:
        if self.focus == "list":
            n = len(self.convs())
            if n:
                self.sel = max(0, min(n - 1, self.sel + step))
                self.dirty = True
        else:
            self._scroll(step * -1)

    def _scroll(self, delta: int) -> None:
        self.scroll = max(0, self.scroll + delta)
        self.follow = False
        self.dirty = True

    def _page(self) -> int:
        _, h = Term.size()
        return max(3, h - 8)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def build_rows(self, w: int, h: int) -> list[tuple[str, str]]:
        """把整个界面算成 (颜色, 整行文本) 列表，长度正好 h。"""
        left_w = LEFT_W
        right_w = max(20, w - left_w - 3)
        body_h = max(3, h - 5)

        convs = self.convs()
        if self.sel >= len(convs):
            self.sel = max(0, len(convs) - 1)

        # ---- 聊天区折行 ----
        rows: list[tuple[str, str]] = []
        for color, text in self.lines:
            for piece in wrap(text, right_w - 1):
                rows.append((color, piece))

        if not rows:
            rows = [("gry", " 我是跑在这台电脑上的 agent。直接打字，Enter 发送。"),
                    ("gry", " Ctrl+N 开新对话，← 切到左边选历史。")]

        scroll = self.scroll
        if self.follow:
            scroll = max(0, len(rows) - body_h)
            self.scroll = scroll
        scroll = max(0, min(scroll, max(0, len(rows) - body_h)))
        self.scroll = scroll
        view = rows[scroll:scroll + body_h]
        while len(view) < body_h:
            view.append(("rst", ""))

        # ---- 标题 ----
        model = (self.sess.cfg.get("main_api") or {}).get("model") or "未配置"
        mode = self.sess.cfg.get("agent_mode", "both")
        title = f" Desktop Agent · {model} · {mode} "
        out: list[tuple[str, str]] = []
        ttl = cut(title, w - 4)
        out.append(("gry", "╭─" + ttl
                    + "─" * max(0, w - 4 - dwidth(ttl)) + "─╮"))

        # ---- 左边栏文字 ----
        left_head = ("gry", " 会话")
        conv_rows: list[tuple[str, str]] = []
        for i, c in enumerate(convs):
            mark = "▸" if i == self.sel else " "
            cur = "*" if (self.sess.conv and c.id == self.sess.conv.id) else " "
            txt = f"{mark}{cur}" + (c.title or "新对话")[:left_w - 5]
            color = "rev" if (i == self.sel and self.focus == "list") else (
                "grn" if cur == "*" else "wht")
            conv_rows.append((color, cut(txt, left_w - 2)))
        while len(conv_rows) < body_h:
            conv_rows.append(("rst", ""))

        # ---- 主体 ----
        for i in range(body_h):
            if i == 0:
                ltxt = " 会话"
                lcolor = "gry"
            else:
                lcolor, ltxt = conv_rows[i - 1] if i - 1 < len(conv_rows) else ("rst", "")
            rcolor, rtxt = view[i]
            line = (
                "│" + _paint(lcolor, pad(ltxt, left_w)) + "│"
                + _paint(rcolor, pad(" " + rtxt, right_w)) + "│"
            )
            out.append(("rst", line))

        # ---- 输入框 ----
        prompt = " › "
        avail = w - 4
        shown = prompt + self.input
        if dwidth(shown) > avail:            # 太长就显示尾部
            while dwidth(shown) > avail and shown:
                shown = shown[1:]
        caret = "▏" if self.focus == "input" else " "
        input_line = _paint("cya" if self.focus == "input" else "wht",
                            pad(shown + caret, avail))
        out.append(("gry", "├" + "─" * (w - 2) + "┤"))
        out.append(("rst", "│" + input_line + "│"))

        # ---- 底边 + 状态 ----
        busy = "运行中" if self.sess.busy else "空闲"
        status = ""
        if self.status and time.time() < self.status_until:
            status = self.status
        tail = f" {busy} · {status} " if status else f" {busy} "
        tail = cut(tail, w - 4)
        out.append(("gry", "╰─" + tail
                    + "─" * max(0, w - 4 - dwidth(tail)) + "─╯"))
        out.append(("gry", cut(HINTS, w)))
        return out

    def draw(self) -> None:
        w, h = Term.size()
        rows = self.build_rows(w, h)

        buf = ["\033[?25l"]
        for i, (color, text) in enumerate(rows, 1):
            buf.append(f"\033[{i};1H")
            if color == "rst":
                buf.append(text + "\033[K")
            else:
                buf.append(C.get(color, "") + text + C["rst"] + "\033[K")
        # 光标放到输入框
        if self.focus == "input":
            col = 2 + dwidth("› " + self.input)
            col = min(col, max(2, w - 1))
            buf.append(f"\033[{h - 2};{col}H\033[?25h")
        else:
            buf.append("\033[?25l")

        if not Term.write("".join(buf)):
            return
        Term.flush()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> int:
        self.term.setup()
        Term.clear()
        try:
            if not config.is_configured(self.sess.cfg):
                self.push("red", "！没有配置主 API Key，发消息会失败。先跑 first.bat。")

            # 初始把当前对话画出来
            if self.sess.conv and self.sess.conv.messages:
                self.do_switch(0)
                self.follow = True
                self.scroll = 0

            last_draw = 0.0
            while self.running:
                key = Term.getkey()
                while key is not None:
                    if not self.on_key(*key):
                        self.running = False
                        break
                    key = Term.getkey()

                self.handle_events()

                now = time.time()
                if self.dirty and now - last_draw > 0.03:
                    self.draw()
                    last_draw = now
                    self.dirty = False

                # 控制台/管道没了（WinError 233），别再刷屏，直接收摊
                if Term.dead:
                    self.running = False
                    break
                time.sleep(0.01)
        finally:
            # 终端已经没了就别再往里写，否则又是一次 WinError 233
            if not Term.dead:
                self.term.show_cursor()
                Term.clear()
            self.term.restore()
        return 0


def _paint(color: str, text: str) -> str:
    if color in ("rst", "", None):
        return text
    return C.get(color, "") + text + C["rst"]


def _note_env() -> None:
    """记一行运行环境，下次再出问题好判断是 conhost 还是 ConPTY。"""
    import datetime

    try:
        term = "windows-terminal" if os.environ.get("WT_SESSION") else (
            os.environ.get("TERM_PROGRAM") or "conhost")
        cols, rows = Term.size()
        with open(config.BASE_DIR / "data" / "tui.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} start  "
                    f"term={term}  size={cols}x{rows}  "
                    f"stdout_tty={sys.stdout.isatty()}\n")
    except Exception:
        pass


def _note_dead_terminal() -> None:
    """终端句柄被外部关闭时的记录。窗口已经没了，只能写文件。"""
    import datetime

    try:
        log = config.BASE_DIR / "data" / "tui.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            f.write("控制台连接被外部断开（WinError 233 / 管道无进程）。\n")
            f.write("成因通常是：窗口或 Windows Terminal 标签被关、\n")
            f.write("conhost.exe 崩溃（中文输入法在 raw 控制台里上屏时偶发）。\n")
    except Exception:
        pass


def run(cfg: dict | None = None) -> int:
    try:
        app = TuiApp(cfg)
        _note_env()
        rc = app.run()
        if Term.dead:
            _note_dead_terminal()
            return rc
        print("已退出 TUI。要重开就再跑一次 start.bat tui。")
        return rc
    except KeyboardInterrupt:
        return 0
    except Exception:
        # 终端模式已经 restore 过，这里能正常 print。
        # 打完再等一下，别让窗口一闪就没；同时落一份日志，免得事后没法查。
        import datetime
        import traceback

        tb = traceback.format_exc()
        try:
            log = config.BASE_DIR / "data" / "crash.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} tui =====\n")
                f.write(tb)
        except Exception:
            pass
        if Term.dead:
            return 1
        try:
            traceback.print_exc()
            print("\n  详细错误已写入 data\\crash.log")
            input("\n  程序异常退出，按回车关闭窗口… ")
        except Exception:
            pass
        return 1
