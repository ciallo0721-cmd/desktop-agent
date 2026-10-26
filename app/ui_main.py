# -*- coding: utf-8 -*-
"""主窗口：左边对话列表，右边聊天区。

两个线程边界：
- 后台线程跑 AgentRunner，只能往 self.q 里塞事件，绝不碰控件。
- 主线程 _drain 每 25ms 抽一次队列，负责改界面。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

from . import agent_core, config, images, memory
from .ui_settings import SettingsWindow
from .ui_skills import SkillWindow

FONT = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

C_BG = "#FFFFFF"
C_SIDE = "#F5F6F8"
C_BORDER = "#E3E6EB"
C_USER = "#DCE9FF"
C_AI = "#F1F3F5"
C_TOOL = "#FFF8E1"
C_MUTED = "#8A9099"
C_ERR = "#FDECEC"


def copy_to_clipboard(text: str) -> bool:
    """往系统剪贴板写文本。

    先走 pyperclip（Win32 API，内容能一直留在剪贴板，不怕程序退出），
    失败再退回 tkinter 自带剪贴板。
    """
    if not text:
        return False
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    try:
        root = tk._default_root
        if root is None:
            return False
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        return True
    except Exception:
        return False


def bind_tree(widget, sequence: str, handler) -> None:
    """把事件绑到整棵子树（含 customtkinter 内部的 canvas/label）。

    CTkFrame 的 bind 只作用于 frame 本身，而它上面盖着 canvas，
    鼠标点击实际落在 canvas 上，所以必须递归绑。
    """
    seen = set()

    def walk(w):
        if id(w) in seen:
            return
        seen.add(id(w))
        try:
            w.bind(sequence, handler, add="+")
        except Exception:
            pass
        for ch in w.winfo_children():
            walk(ch)

    walk(widget)


# ==========================================================================
# 气泡
# ==========================================================================

class ChatBubble(ctk.CTkFrame):
    """一条消息。

    文字用 tk.Text 渲染（不是 CTkLabel）——CTkLabel 是 canvas 画的图，
    选不中、拖不了、Ctrl+C 拿不到东西。Text 才能拖拽选区。
    """

    MAX_W = 600
    OUT_X = 8
    OUT_Y = 6
    TXT_PADX = 6
    TXT_PADY = 6

    def __init__(self, master, role: str):
        super().__init__(master, fg_color="transparent")
        self.role = role
        self._text = ""
        self._timer = None
        self._fit_job = None
        self.img_holder = None
        self._photos: list = []      # 防 CTkImage 被 GC
        self._img_h = 0
        self._img_w = 0
        is_user = role == "user"
        bg = C_USER if is_user else C_AI
        self._font = tkfont.Font(family=FONT, size=13)
        self._lh = self._font.metrics("linespace")

        # 一行：气泡 + 复制按钮
        self.row = ctk.CTkFrame(self, fg_color="transparent")
        self.row.pack(
            side="right" if is_user else "left",
            padx=(80, 8) if is_user else (12, 80),
            pady=4,
            anchor="e" if is_user else "w",
        )

        self.box = ctk.CTkFrame(self.row, fg_color=bg, corner_radius=9,
                                width=220, height=self._lh + 26)
        self.box.pack_propagate(False)

        self.txt = tk.Text(
            self.box, bg=bg, fg="#1F2328", font=(FONT, 13), bd=0, relief="flat",
            highlightthickness=0, wrap="word",
            padx=self.TXT_PADX, pady=self.TXT_PADY,
            cursor="xterm", state="disabled", insertwidth=0,
            selectbackground="#9CC6FF", selectforeground="#101010",
            spacing1=0, spacing3=2, takefocus=1, inactiveselectbackground="#C7DCF7",
        )
        self.txt.pack(fill="both", expand=True,
                      padx=self.OUT_X, pady=self.OUT_Y)

        # 拖拽选区由 Text 原生支持；这几个键得自己接，Tk 的 Ctrl+A 是"回到行首"
        for seq in ("<Control-a>", "<Control-A>"):
            self.txt.bind(seq, self._select_all)
        for seq in ("<Control-c>", "<Control-C>"):
            self.txt.bind(seq, self._copy_sel)
        self.txt.bind("<MouseWheel>", self._wheel)
        self.txt.bind("<Button-4>", self._wheel)
        self.txt.bind("<Button-5>", self._wheel)

        self.btn = self._make_copy_btn(self.row)
        if is_user:
            self.box.pack(side="right")
            self.btn.pack(side="right", padx=(0, 4))
        else:
            self.box.pack(side="left")
            self.btn.pack(side="left", padx=(4, 0))

        bind_tree(self, "<Button-3>", self._popup)

    # -- 图片 ----------------------------------------------------------
    def add_images(self, paths) -> int:
        """在文字上方插缩略图，返回成功加载的张数。"""
        got = []
        for rel in (paths or []):
            im = images.open_for_ui(rel, 340)
            if im is None:
                continue
            got.append((rel, im))
        if not got:
            return 0

        if self.img_holder is None:
            self.img_holder = ctk.CTkFrame(self.box, fg_color="transparent")
            self.img_holder.pack(fill="x", padx=self.OUT_X,
                                 pady=(self.OUT_Y + 2, 0), before=self.txt)

        for rel, im in got:
            ck = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
            self._photos.append(ck)
            lab = ctk.CTkLabel(self.img_holder, image=ck, text="",
                               fg_color="transparent")
            lab.pack(anchor="w", pady=(0, 4))
            lab.bind("<Button-1>", lambda e, r=rel: self._open_viewer(r))
            lab.configure(cursor="hand2")
            self._img_h += im.size[1] + 4
            self._img_w = max(self._img_w, im.size[0])

        self._schedule_fit()
        return len(got)

    def _open_viewer(self, rel: str) -> None:
        """点缩略图看大图。"""
        p = images.resolve(rel)
        if not p:
            return
        try:
            im = Image.open(p)
            im.load()
        except Exception:
            return
        try:
            sw = self.winfo_screenwidth() - 140
            sh = self.winfo_screenheight() - 180
            k = min(1.0, sw / im.width, sh / im.height)
            if k < 1:
                im = im.resize((max(1, int(im.width * k)),
                                max(1, int(im.height * k))), Image.LANCZOS)
            win = tk.Toplevel(self)
            win.title(f"{p.name}  ·  {im.width}×{im.height}")
            ck = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
            lab = ctk.CTkLabel(win, image=ck, text="")
            lab.image = ck
            lab.pack()
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

    # -- 尺寸 ----------------------------------------------------------
    def _scroll_canvas(self):
        p = self.master
        while p is not None:
            c = getattr(p, "_parent_canvas", None)
            if c is not None:
                return c
            p = getattr(p, "master", None)
        return None

    def _wheel(self, e):
        """把滚轮让给外层聊天区，不然 Text 会吃掉滚动。"""
        try:
            c = self._scroll_canvas()
            if c is not None:
                if getattr(e, "num", None) == 4:
                    step = -1
                elif getattr(e, "num", None) == 5:
                    step = 1
                else:
                    step = -1 if e.delta > 0 else 1
                c.yview_scroll(step, "units")
        except Exception:
            pass
        return "break"

    def _schedule_fit(self) -> None:
        if self._fit_job:
            return
        try:
            # 50ms 节流：流式时把多次 set_text 合并成一次重排，不然每帧 update_idletasks 太重
            self._fit_job = self.after(50, self._fit)
        except Exception:
            self._fit_job = None

    def _fit(self) -> None:
        """按内容算气泡像素尺寸。先估算，再用真实渲染行数校正。"""
        self._fit_job = None
        try:
            if not self.winfo_exists():
                return
            text = self._text or " "
            lines = text.split("\n")
            px = [self._font.measure(ln) for ln in lines]
            need = max(px) if px else 0
            pad = 2 * (self.OUT_X + self.TXT_PADX)
            box_w = min(self.MAX_W, max(80, need + pad + 10))
            if self._img_w:
                box_w = max(box_w, min(self.MAX_W, self._img_w + 2 * self.OUT_X))
            inner = max(24, box_w - pad - 6)

            n = 0
            for w in px:
                n += 1 if w <= inner else (int(w) + int(inner) - 1) // int(inner)
            n = max(1, n)

            box_h = (n * (self._lh + 2) + 2 * (self.OUT_Y + self.TXT_PADY) + 8
                     + self._img_h)
            self.box.configure(width=int(box_w), height=int(box_h))
            self.box.update_idletasks()

            try:
                # displaylines 数的是「跨过几次换行边界」，行数要 +1
                real = self.txt.count("1.0", "end-1c", "displaylines")
                if isinstance(real, (tuple, list)):
                    real = real[0] if real else 0
                real = int(real or 0) + 1
                if real > 0:
                    h2 = (real * (self._lh + 2)
                          + 2 * (self.OUT_Y + self.TXT_PADY) + 8 + self._img_h)
                    # 宁可多几像素留白，也不要裁掉文字
                    if h2 > box_h:
                        box_h = h2
                    self.box.configure(height=int(box_h))
            except Exception:
                pass
        except Exception:
            pass

    # -- 复制按钮 ------------------------------------------------------
    def _make_copy_btn(self, parent) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent, text="复制", width=44, height=22, corner_radius=6,
            fg_color="transparent", hover_color="#E4E8EE", border_width=0,
            text_color="#A8AEB6", font=ctk.CTkFont(family=FONT, size=10),
            command=self.copy_text,
        )

    def _flash(self, text: str, color: str = "#2E7D32") -> None:
        try:
            if not self.btn.winfo_exists():
                return
            self.btn.configure(text=text, text_color=color)
            if self._timer:
                self.after_cancel(self._timer)
            self._timer = self.after(1200, self._unflash)
        except Exception:
            pass

    def _unflash(self) -> None:
        try:
            if self.btn.winfo_exists():
                self.btn.configure(text="复制", text_color="#A8AEB6")
        except Exception:
            pass

    # -- 文本与选区 ----------------------------------------------------
    def set_text(self, text: str) -> None:
        new = text or ""
        old = self._text
        try:
            self.txt.configure(state="normal")
            if old and new.startswith(old):
                self.txt.insert("end-1c", new[len(old):])
            else:
                self.txt.delete("1.0", "end")
                self.txt.insert("1.0", new)
            self.txt.configure(state="disabled")
        except Exception:
            pass
        self._text = new
        self._schedule_fit()

    def _select_all(self, _e=None):
        try:
            self.txt.tag_add("sel", "1.0", "end-1c")
            self.txt.mark_set("insert", "1.0")
        except Exception:
            pass
        return "break"

    def _copy_sel(self, _e=None):
        """有选区就复制选区，没选区复制整条。"""
        try:
            sel = self.txt.get("sel.first", "sel.last")
        except tk.TclError:
            sel = ""
        target = sel or self._text
        if not target:
            return "break"
        if copy_to_clipboard(target):
            self._flash("已复制选中" if sel else "已复制")
        else:
            self._flash("复制失败", "#C0392B")
        return "break"

    def _popup(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制选中", command=self._copy_selection_only)
        menu.add_command(label="复制这一条", command=self.copy_text)
        menu.add_separator()
        menu.add_command(label="全选", command=self._select_all)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_selection_only(self) -> None:
        try:
            sel = self.txt.get("sel.first", "sel.last")
            copy_to_clipboard(sel)
        except tk.TclError:
            pass

    def copy_text(self) -> None:
        if not self._text:
            self._flash("空消息", "#C0392B")
            return
        if copy_to_clipboard(self._text):
            self._flash("已复制")
        else:
            self._flash("复制失败", "#C0392B")


class ErrorBubble(ctk.CTkFrame):
    def __init__(self, master, text: str):
        super().__init__(master, fg_color="transparent")
        self._text = text or ""
        box = ctk.CTkFrame(self, fg_color=C_ERR, corner_radius=10,
                           border_width=1, border_color="#F3C9C9")
        box.pack(side="left", padx=(12, 60), pady=4, anchor="w")
        ctk.CTkLabel(
            box, text=text, justify="left", anchor="w", wraplength=580,
            font=ctk.CTkFont(family=FONT, size=12), text_color="#A32A2A",
        ).pack(padx=13, pady=9)
        bind_tree(self, "<Button-3>", self._popup)

    def _popup(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制报错", command=lambda: copy_to_clipboard(self._text))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


class ToolCard(ctk.CTkFrame):
    """一次工具调用：头部一行命令，下面是结果。"""

    def __init__(self, master, name: str, detail: str):
        super().__init__(master, fg_color="transparent")
        self.name = name
        self.done = False
        self._full = ""
        self._timer = None

        self.row = ctk.CTkFrame(self, fg_color="transparent")
        self.row.pack(side="left", padx=(12, 60), pady=3, anchor="w")

        self.box = ctk.CTkFrame(self.row, fg_color=C_TOOL, corner_radius=8,
                                border_width=1, border_color="#F0E0A8")
        self.box.pack(side="left")

        head = name if not detail else f"{name}  {detail}"
        self.head_label = ctk.CTkLabel(
            self.box, text="› " + head, justify="left", anchor="w",
            wraplength=600, font=ctk.CTkFont(family=FONT_MONO, size=11),
            text_color="#7A6A2A",
        )
        self.head_label.pack(padx=10, pady=(7, 2), anchor="w")

        self.body = ctk.CTkLabel(
            self.box, text="执行中…", justify="left", anchor="w",
            wraplength=600, font=ctk.CTkFont(family=FONT_MONO, size=11),
            text_color="#9A8B55",
        )
        self.body.pack(padx=10, pady=(0, 7), anchor="w")

        self.btn = ctk.CTkButton(
            self.row, text="复制", width=44, height=22, corner_radius=6,
            fg_color="transparent", hover_color="#EFE2B8", border_width=0,
            text_color="#B0A170", font=ctk.CTkFont(family=FONT, size=10),
            command=self._copy,
        )
        self.btn.pack(side="left", padx=(4, 0))
        bind_tree(self, "<Button-3>", self._popup)

    def set_result(self, text: str) -> None:
        self.done = True
        self._full = text or ""
        t = (text or "").strip() or "（无输出）"
        if len(t) > 900:
            t = t[:900] + f"\n…（还有 {len(text) - 900} 字没显示）"
        self.body.configure(text=t, text_color="#57503A")

    def _popup(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制完整输出", command=self._copy)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _flash(self, text: str, color: str = "#2E7D32") -> None:
        try:
            if not self.btn.winfo_exists():
                return
            self.btn.configure(text=text, text_color=color)
            if self._timer:
                self.after_cancel(self._timer)
            self._timer = self.after(1200, self._unflash)
        except Exception:
            pass

    def _unflash(self) -> None:
        try:
            if self.btn.winfo_exists():
                self.btn.configure(text="复制", text_color="#B0A170")
        except Exception:
            pass

    def _copy(self) -> None:
        if not self._full:
            self._flash("无内容", "#C0392B")
            return
        if copy_to_clipboard(self._full):
            self._flash("已复制")
        else:
            self._flash("复制失败", "#C0392B")


# ==========================================================================
# 高危命令确认框
# ==========================================================================

class ConfirmDialog(ctk.CTkToplevel):
    def __init__(self, master, cmd: str, reason: str, on_answer):
        super().__init__(master)
        self.on_answer = on_answer
        self.answered = False
        self.title("确认执行高危命令")
        self.geometry("600x460")
        self.minsize(520, 400)
        self.transient(master)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(False))

        # 底部按钮先 pack，保证内容再长也不会把它挤没
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(
            btns, text="拒绝", width=100, fg_color="#9CA3AF", hover_color="#6B7280",
            command=lambda: self._answer(False),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btns, text="允许执行", width=110, fg_color="#C0392B", hover_color="#96281B",
            command=lambda: self._answer(True),
        ).pack(side="right")

        ctk.CTkLabel(
            self,
            text="这条命令可能会不可逆地删除或修改数据。看清楚了再点。",
            anchor="w", font=ctk.CTkFont(family=FONT, size=11),
            text_color=C_MUTED,
        ).pack(side="bottom", fill="x", padx=16, pady=(0, 6))

        ctk.CTkLabel(
            self, text="AI 想要执行一条高危命令", anchor="w",
            font=ctk.CTkFont(family=FONT, size=15, weight="bold"),
        ).pack(fill="x", padx=16, pady=(14, 2))

        ctk.CTkLabel(
            self, text=f"风险类型：{reason}", anchor="w",
            font=ctk.CTkFont(family=FONT, size=12), text_color="#C0392B",
        ).pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(
            self, text="命令内容：", anchor="w",
            font=ctk.CTkFont(family=FONT, size=12),
        ).pack(fill="x", padx=16)

        box = ctk.CTkTextbox(
            self, height=120, font=ctk.CTkFont(family=FONT_MONO, size=12),
            fg_color="#FAFAFA", border_width=1, border_color=C_BORDER,
            wrap="word", activate_scrollbars=False,
        )
        box.pack(fill="both", expand=True, padx=16, pady=(2, 10))
        box.insert("1.0", cmd)

        self.after(150, self.lift)
        self.after(250, self.grab_set)

    def _answer(self, ok: bool) -> None:
        if self.answered:
            return
        self.answered = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self.on_answer(ok)


# ==========================================================================
# 主窗口
# ==========================================================================

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = config.load_config()
        ctk.set_appearance_mode(self.cfg.get("appearance", "light"))
        ctk.set_default_color_theme("blue")

        self.title("Agent")
        self.geometry("1140x760")
        self.minsize(920, 600)

        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.current: agent_core.Conversation | None = None

        self._stream_bubble: ChatBubble | None = None
        self._stream_buf = ""
        self._think_bubble: ChatBubble | None = None
        self._think_buf = ""
        self._tool_card: ToolCard | None = None
        self._welcome = False
        self._last_ai_text = ""
        self.pending_images: list[str] = []
        self._rounds_since_memory = 0
        self._mem_running = False
        self._closing = False
        self._close_wait = 0
        self._confirm_dlg = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Ctrl+Shift+C 复制上一条 AI 回复；鼠标右键复制单条消息
        for seq in ("<Control-Shift-c>", "<Control-Shift-C>",
                    "<Control-Shift-KeyPress-c>"):
            self.bind_all(seq, self._copy_last_ai)
        self.after(25, self._drain)

        self._reload_conv_list()
        convs = agent_core.list_conversations()
        if convs:
            self._select_conversation(convs[0])
        else:
            self._new_conversation()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, fg_color=C_SIDE, corner_radius=0, width=210)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.pack_propagate(False)   # 子控件是 pack 进来的，得关 pack 的传播

        ctk.CTkLabel(
            side, text="  Agent", anchor="w",
            font=ctk.CTkFont(family=FONT, size=16, weight="bold"),
            text_color="#1F2328",
        ).pack(fill="x", pady=(14, 8))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh_conv_list())
        ctk.CTkEntry(
            side, textvariable=self.search_var, placeholder_text="搜索对话…",
            height=30, font=ctk.CTkFont(family=FONT, size=12),
        ).pack(fill="x", padx=10, pady=(0, 10))

        head = ctk.CTkFrame(side, fg_color="transparent")
        head.pack(fill="x", padx=12)
        ctk.CTkLabel(
            head, text="对话", anchor="w",
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
            text_color="#5B6169",
        ).pack(side="left")
        ctk.CTkButton(
            head, text="+", width=26, height=24, corner_radius=6,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._new_conversation,
        ).pack(side="right")

        self.conv_list = ctk.CTkScrollableFrame(
            side, fg_color="transparent", corner_radius=0,
        )
        self.conv_list.pack(fill="both", expand=True, padx=6, pady=6)

        ctk.CTkButton(
            side, text="🧩   技能", height=34, corner_radius=8,
            fg_color="#E6E9EE", hover_color="#D8DCE3", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=12),
            command=self._open_skills,
        ).pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkButton(
            side, text="⚙   设置", height=34, corner_radius=8,
            fg_color="#E6E9EE", hover_color="#D8DCE3", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=12),
            command=self._open_settings,
        ).pack(fill="x", padx=10, pady=(0, 12))

    def _build_main(self) -> None:
        self.grid_rowconfigure(0, weight=1)

        wrap = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        wrap.grid(row=0, column=1, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(1, weight=1)

        # ---- 顶栏 ----
        top = ctk.CTkFrame(wrap, fg_color=C_BG, corner_radius=0, height=44)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_propagate(False)

        self.model_label = ctk.CTkLabel(
            top, text="", anchor="w", text_color=C_MUTED,
            font=ctk.CTkFont(family=FONT, size=11),
        )
        self.model_label.pack(side="left", padx=14)

        ctk.CTkButton(
            top, text="新对话", width=72, height=26, corner_radius=6,
            fg_color="#E6E9EE", hover_color="#D8DCE3", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=11),
            command=self._new_conversation,
        ).pack(side="right", padx=12)

        line = ctk.CTkFrame(wrap, fg_color=C_BORDER, height=1, corner_radius=0)
        line.grid(row=0, column=0, sticky="sew")

        # ---- 聊天区 ----
        self.chat = ctk.CTkScrollableFrame(wrap, fg_color=C_BG, corner_radius=0)
        self.chat.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)

        # ---- 输入区 ----
        bar = ctk.CTkFrame(wrap, fg_color=C_BG, corner_radius=0)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)

        # 待发图片条：有图才占位置
        self.attach_bar = ctk.CTkFrame(
            bar, fg_color="#F7F8FA", corner_radius=8,
            border_width=1, border_color=C_BORDER,
        )
        self.attach_bar.grid(row=0, column=0, columnspan=3, sticky="ew",
                             padx=12, pady=(8, 0))
        self.attach_bar.grid_remove()
        self.attach_inner = ctk.CTkFrame(self.attach_bar, fg_color="transparent")
        self.attach_inner.pack(fill="x", padx=8, pady=6)

        self.input_box = ctk.CTkTextbox(
            bar, height=64, corner_radius=8, border_width=1,
            border_color=C_BORDER, font=ctk.CTkFont(family=FONT, size=13),
            wrap="word", fg_color="#FCFCFD",
        )
        self.input_box.grid(row=1, column=0, sticky="ew", padx=(12, 8), pady=(8, 4))
        self.input_box.bind("<Return>", self._on_enter)
        self.input_box.bind("<Shift-Return>", lambda e: None)
        self._bind_paste()

        self.img_btn = ctk.CTkButton(
            bar, text="图片", width=54, height=64, corner_radius=8,
            fg_color="#E6E9EE", hover_color="#D8DCE3", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=11),
            command=self._pick_images,
        )
        self.img_btn.grid(row=1, column=1, padx=(0, 8), pady=(8, 4))

        self.send_btn = ctk.CTkButton(
            bar, text="发送", width=76, height=64, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
            command=self._on_send_click,
        )
        self.send_btn.grid(row=1, column=2, padx=(0, 12), pady=(8, 4))

        self.status_label = ctk.CTkLabel(
            bar, text="Enter 发送 · Shift+Enter 换行 · Ctrl+V 粘贴截图", anchor="w",
            text_color=C_MUTED, font=ctk.CTkFont(family=FONT, size=10),
        )
        self.status_label.grid(row=2, column=0, columnspan=3, sticky="w", padx=16,
                               pady=(0, 8))

        self._update_model_label()

    def _update_model_label(self) -> None:
        api = self.cfg.get("main_api") or {}
        mode = {
            "screen": "截屏操作",
            "both": "命令行+截屏",
        }.get(self.cfg.get("agent_mode", "both"), "命令行 Bash")
        vision = "有识图" if (self.cfg.get("vision_api") or {}).get("api_key") else "无识图"
        sandbox = "沙箱开" if self.cfg.get("sandbox", True) else "沙箱关"
        self.model_label.configure(
            text=f"{api.get('model') or '未配置模型'}   ·   {mode}   ·   {vision}   ·   {sandbox}"
        )

    # ------------------------------------------------------------------
    # 对话列表
    # ------------------------------------------------------------------
    def _reload_conv_list(self) -> None:
        self._convs = agent_core.list_conversations()
        self._refresh_conv_list()

    def _refresh_conv_list(self) -> None:
        for w in self.conv_list.winfo_children():
            w.destroy()

        kw = (self.search_var.get() or "").strip().lower()
        cur_id = self.current.id if self.current else None
        shown = 0
        for conv in agent_core.list_conversations():
            if kw and kw not in conv.title.lower():
                continue
            shown += 1
            selected = conv.id == cur_id
            btn = ctk.CTkButton(
                self.conv_list,
                text="  " + (conv.title or "新对话")[:22],
                anchor="w", height=30, corner_radius=6,
                fg_color="#E6E9EE" if selected else "transparent",
                hover_color="#E9ECF1",
                text_color="#1F2328", font=ctk.CTkFont(family=FONT, size=12),
                command=lambda c=conv: self._select_conversation(c),
            )
            btn.pack(fill="x", pady=1)
            btn.bind("<Button-3>", lambda e, c=conv: self._conv_menu(e, c))

        if not shown:
            ctk.CTkLabel(
                self.conv_list, text="还没有对话", text_color=C_MUTED,
                font=ctk.CTkFont(family=FONT, size=11),
            ).pack(pady=14)

    def _conv_menu(self, event, conv) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="删除这个对话",
                         command=lambda: self._delete_conversation(conv))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _delete_conversation(self, conv) -> None:
        if not messagebox.askyesno(
            "删除对话",
            f"删掉「{conv.title}」？\n\n会移到 data/trash，想捞回来可以去那个目录翻。",
            parent=self,
        ):
            return
        conv.delete()
        if self.current and self.current.id == conv.id:
            self.current = None
            self._clear_chat()
            convs = agent_core.list_conversations()
            if convs:
                self._select_conversation(convs[0])
            else:
                self._new_conversation()
        self._reload_conv_list()

    def _select_conversation(self, conv) -> None:
        if self.busy:
            self.cancel.set()
        self.current = conv
        self._rounds_since_memory = 0
        self._reset_attachments()
        self._render_conversation()
        self._refresh_conv_list()

    def _new_conversation(self) -> None:
        if self.busy:
            self.cancel.set()
        self.current = agent_core.Conversation()
        self._rounds_since_memory = 0
        self._reset_attachments()
        self._render_conversation()
        self._refresh_conv_list()
        self.input_box.focus_set()

    def _reset_attachments(self) -> None:
        """换对话时把待发图片扔掉，别飘到别的对话里。"""
        if not self.pending_images:
            return
        self.pending_images.clear()
        try:
            self._render_attach()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _clear_chat(self) -> None:
        for w in self.chat.winfo_children():
            w.destroy()
        self._stream_bubble = None
        self._tool_card = None
        self._think_bubble = None
        self._think_buf = ""
        self._welcome = False
        # 必须重置画布：子控件 destroy 后 scrollregion 不会自己收缩，
        # 视图会停在旧位置，新内容看着像一片空白。
        self._reset_scroll()

    def _render_conversation(self) -> None:
        self._clear_chat()
        if not self.current:
            return

        msgs = self.current.visible()
        if not msgs:
            name = self.cfg.get("user_name", "你")
            ctk.CTkLabel(
                self.chat, text=f"HI！{name}",
                font=ctk.CTkFont(family=FONT, size=26, weight="bold"),
                text_color="#1F2328",
            ).pack(pady=(90, 6))
            ctk.CTkLabel(
                self.chat, text="我是跑在这台电脑上的 agent。要我干什么？",
                font=ctk.CTkFont(family=FONT, size=12), text_color=C_MUTED,
            ).pack()
            self._welcome = True
            return

        for m in msgs:
            if m.get("tool_result"):
                card = ToolCard(self.chat, m.get("tool", "tool"), "")
                card.pack(fill="x")
                card.set_result(m.get("content", ""))
                continue
            role = m.get("role", "user")
            text = m.get("content", "")
            if role == "assistant":
                text = agent_core.strip_actions(text)
            pics = m.get("images") or []
            if not text.strip() and not pics:
                continue
            b = ChatBubble(self.chat, role)
            b.pack(fill="x")
            b.set_text(text)
            if pics:
                b.add_images(pics)

        self._scroll_to_bottom()

    def _reset_scroll(self) -> None:
        """把画布滚回顶部并把滚动区域清零。"""
        try:
            canvas = self.chat._parent_canvas
            self.chat.update_idletasks()
            canvas.configure(scrollregion=(0, 0, 0, 0))
            canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _scroll_to_bottom(self) -> None:
        try:
            self.chat.update_idletasks()
            canvas = self.chat._parent_canvas
            box = canvas.bbox("all")
            if box:
                canvas.configure(scrollregion=box)
            canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _drop_welcome(self) -> None:
        """第一条真实消息出现时，把欢迎语扔掉。"""
        if not self._welcome:
            return
        self._welcome = False
        for w in self.chat.winfo_children():
            w.destroy()

    def _add_bubble(self, role: str, text: str, pics=None) -> ChatBubble:
        self._drop_welcome()
        b = ChatBubble(self.chat, role)
        b.pack(fill="x")
        b.set_text(text)
        if pics:
            b.add_images(pics)
        if role == "assistant":
            self._last_ai_text = text
        self._scroll_to_bottom()
        return b

    def _copy_last_ai(self, _event=None):
        if not self._last_ai_text:
            self._status("还没有可复制的 AI 回复")
            return "break"
        if copy_to_clipboard(self._last_ai_text):
            self._status("已复制上一条 AI 回复")
        else:
            self._status("复制失败，试试右键菜单")
        return "break"

    def _add_tool_card(self, name: str, detail: str) -> ToolCard:
        self._drop_welcome()
        card = ToolCard(self.chat, name, detail)
        card.pack(fill="x")
        self._tool_card = card
        self._scroll_to_bottom()
        return card

    def _status(self, text: str, ttl: int = 4000) -> None:
        self.status_label.configure(text=text)
        if ttl:
            self.after(ttl, lambda: self.status_label.configure(
                text="Enter 发送 · Shift+Enter 换行"))

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 发图片
    # ------------------------------------------------------------------
    def _pick_images(self) -> None:
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            parent=self, title="选图片，可以多选",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff"),
                ("所有文件", "*.*"),
            ],
        )
        for p in paths or ():
            self._attach_path(p)

    def _attach_path(self, src) -> None:
        """压缩落盘放后台做：手机直出的大图在主线程 resize 会卡住窗口。"""
        if not self.current:
            return
        cid = self.current.id

        def work() -> None:
            try:
                rel = images.save(src, cid)
            except images.ImageError as e:
                msg = str(e)
                self._post(lambda: self._status(f"没加上：{msg}"))
                return
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self._post(lambda: self._status(f"没加上：{msg}"))
                return
            self._post(lambda: self._add_pending(rel))

        threading.Thread(target=work, daemon=True).start()

    def _post(self, fn) -> None:
        """从后台线程回主线程。窗口已经关掉时 after 会抛，别让线程带毒退出。"""
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _add_pending(self, rel: str) -> None:
        if rel not in self.pending_images:
            self.pending_images.append(rel)
        self._render_attach()
        self._status(f"已附上 {len(self.pending_images)} 张图片，敲回车一起发出去")

    def _render_attach(self) -> None:
        for w in self.attach_inner.winfo_children():
            w.destroy()
        if not self.pending_images:
            self.attach_bar.grid_remove()
            return
        self.attach_bar.grid()
        for rel in list(self.pending_images):
            card = ctk.CTkFrame(self.attach_inner, fg_color="#FFFFFF",
                                corner_radius=6, border_width=1,
                                border_color=C_BORDER)
            card.pack(side="left", padx=(0, 8))

            im = images.open_for_ui(rel, 96)
            if im is not None:
                ck = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                lab = ctk.CTkLabel(card, image=ck, text="")
                lab.image = ck
                lab.pack(padx=5, pady=(5, 0))
            else:
                ctk.CTkLabel(
                    card, text="读不出", width=56,
                    font=ctk.CTkFont(family=FONT, size=10),
                    text_color="#B0442F",
                ).pack(padx=8, pady=14)

            ctk.CTkButton(
                card, text="×", width=18, height=16, corner_radius=9,
                fg_color="transparent", hover_color="#F3DCDC",
                text_color="#A05252", font=ctk.CTkFont(size=12),
                command=lambda r=rel: self._drop_pending(r),
            ).pack(pady=(0, 4))

    def _drop_pending(self, rel: str) -> None:
        try:
            self.pending_images.remove(rel)
        except ValueError:
            pass
        self._render_attach()
        if not self.pending_images:
            self._status("Enter 发送 · Shift+Enter 换行 · Ctrl+V 粘贴截图")
        else:
            self._status(f"已附上 {len(self.pending_images)} 张图片")

    def _bind_paste(self) -> None:
        """把 Ctrl+V 抢到 Tk 默认粘贴之前。

        CTkTextbox.bind() 是 add=True，绑定排在类绑定之后 —— 那样文字已经
        粘进去了，再判断就晚了。所以往 bindtags 最前面插一个自己的 tag。
        """
        try:
            tb = getattr(self.input_box, "_textbox", None)
            if tb is None:
                return
            tags = tuple(tb.bindtags())
            if "AgentPaste" in tags:
                return
            tb.bindtags(("AgentPaste",) + tags)
            for seq in ("<Control-v>", "<Control-V>"):
                tb.bind_class("AgentPaste", seq, self._on_paste)
        except Exception:
            # 抢不到就退回普通绑定，至少按钮和右键还能用
            try:
                self.input_box.bind("<Control-v>", self._on_paste)
            except Exception:
                pass

    def _on_paste(self, event):
        """剪贴板里是图就收下；是文字就交给 Tk 自己粘。"""
        try:
            from PIL import ImageGrab

            data = ImageGrab.grabclipboard()
        except Exception:
            return None

        if isinstance(data, Image.Image):
            self._attach_path(data)
            return "break"
        if isinstance(data, list) and data:
            first = Path(str(data[0]))
            if first.suffix.lower() in images.SUPPORTED and first.is_file():
                self._attach_path(first)
                return "break"
        return None

    def _on_enter(self, event):
        if event.state & 0x0001:   # Shift → 换行
            return None
        self._send()
        return "break"

    def _on_send_click(self) -> None:
        if self.busy:
            self.cancel.set()
            self._status("正在停止…", 0)
            return
        self._send()

    def _send(self) -> None:
        if self.busy or not self.current:
            return
        text = self.input_box.get("1.0", "end").strip()
        pics = list(self.pending_images)
        if not text and not pics:
            return
        if not config.is_configured(self.cfg):
            messagebox.showwarning("没配 API", "先去设置里填主 API 的 Key。", parent=self)
            self._open_settings()
            return

        self.input_box.delete("1.0", "end")
        self.pending_images.clear()
        self._render_attach()

        b = self._add_bubble("user", text)
        if pics:
            b.add_images(pics)

        self.busy = True
        self.cancel = threading.Event()
        self.send_btn.configure(text="停止")
        self._status("思考中…", 0)

        conv = self.current
        cfg = dict(self.cfg)
        runner = agent_core.AgentRunner(cfg, conv)

        # 事件打上对话 id：用户切走后，旧对话的流不能再往新对话里画
        cid = conv.id

        def emit(evt: dict) -> None:
            evt = dict(evt)
            evt["_cid"] = cid
            self.q.put(evt)

        threading.Thread(
            target=runner.run,
            args=(text, emit, self.cancel, self._ask_confirm),
            kwargs={"images": pics},
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # 高危命令确认（在后台线程里被调用，必须阻塞等结果）
    # ------------------------------------------------------------------
    def _ask_confirm(self, cmd: str, reason: str) -> bool:
        ev = threading.Event()
        box = {"ok": False}

        def show():
            def answer(ok: bool):
                box["ok"] = ok
                ev.set()

            self._confirm_dlg = ConfirmDialog(self, cmd, reason, answer)

        try:
            self.after(0, show)
        except RuntimeError:
            return False

        ev.wait(timeout=300)
        return bool(box.get("ok"))

    # ------------------------------------------------------------------
    # 事件泵
    # ------------------------------------------------------------------
    def _drain(self) -> None:
        try:
            while True:
                evt = self.q.get_nowait()
                try:
                    self._handle(evt)
                except tk.TclError:
                    return
                except Exception:
                    # 单个事件处理失败不能拖死整个事件泵
                    traceback.print_exc()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(25, self._drain)
        except tk.TclError:
            pass

    def _flush_think(self) -> None:
        """思考结束：长思考折叠掉，别把正文顶出屏幕。"""
        if self._think_bubble is None:
            return
        n = len(self._think_buf)
        if n > 800:
            self._think_bubble.set_text(
                "〈思考〉" + self._think_buf[:800] + f"\n…（共 {n} 字，已折叠）"
            )
        self._think_bubble = None
        self._think_buf = ""

    def _handle(self, evt: dict) -> None:
        kind = evt.get("type")
        cid = evt.get("_cid")
        stale = bool(cid) and (
            self.current is None or self.current.id != cid
        )
        # 已经切到别的对话了，旧对话的流式内容和工具卡片都不要画
        if stale and kind in (
            "thinking", "reasoning", "delta", "assistant_end",
            "tool_start", "tool_result", "ask",
        ):
            return

        if kind == "thinking":
            if not self.cfg.get("show_reasoning", True):
                return
            if self._think_bubble is None:
                self._drop_welcome()
                self._think_bubble = ChatBubble(self.chat, "assistant")
                self._think_bubble.pack(fill="x")
                self._think_buf = ""
                self._think_bubble.set_text("〈思考〉正在思考…")
                self._scroll_to_bottom()

        elif kind == "reasoning":
            if not self.cfg.get("show_reasoning", True):
                return
            if self._think_bubble is None:
                self._drop_welcome()
                self._think_bubble = ChatBubble(self.chat, "assistant")
                self._think_bubble.pack(fill="x")
                self._think_buf = ""
            self._think_buf += evt.get("text", "")
            self._think_bubble.set_text("〈思考〉" + self._think_buf)
            self._scroll_to_bottom()

        elif kind == "delta":
            self._flush_think()
            if self._stream_bubble is None:
                self._drop_welcome()
                self._stream_bubble = ChatBubble(self.chat, "assistant")
                self._stream_bubble.pack(fill="x")
                self._stream_buf = ""
            self._stream_buf += evt.get("text", "")
            shown = agent_core.strip_streaming(self._stream_buf)
            # 整段都是动作标记时别留个空气泡
            self._stream_bubble.set_text(shown or "…")
            self._scroll_to_bottom()

        elif kind == "assistant_end":
            self._flush_think()
            shown = agent_core.strip_actions(evt.get("text", ""))
            if shown:
                self._last_ai_text = shown
            if self._stream_bubble is not None:
                if shown:
                    self._stream_bubble.set_text(shown)
                else:
                    self._stream_bubble.destroy()
            elif shown:
                self._add_bubble("assistant", shown)
            self._stream_bubble = None
            self._stream_buf = ""

        elif kind == "tool_start":
            self._flush_think()
            if evt.get("name") != "done":     # done 只是终点标记，不用画卡片
                self._add_tool_card(evt.get("name", "tool"), evt.get("detail", ""))

        elif kind == "tool_result":
            if self._tool_card is not None:
                self._tool_card.set_result(evt.get("text", ""))
                self._tool_card = None
            self._scroll_to_bottom()

        elif kind == "ask":
            text = (evt.get("text") or "").strip()
            if text:
                self._add_bubble("assistant", text)

        elif kind == "done":
            self._flush_think()
            self._finish_turn()
            self._maybe_memory()

        elif kind == "cancel":
            self._flush_think()
            self._finish_turn()
            self._status("已停止", 2500)

        elif kind == "error":
            self._flush_think()
            self._stream_bubble = None
            self._stream_buf = ""
            self._finish_turn()
            b = ErrorBubble(self.chat, evt.get("msg", "未知错误"))
            b.pack(fill="x")
            self._scroll_to_bottom()

        elif kind == "mem_ok":
            self._mem_running = False
            self._status("记忆已更新", 3000)

        elif kind == "mem_fail":
            self._mem_running = False
            self._status(f"记忆更新失败：{str(evt.get('msg'))[:60]}", 5000)

        elif kind == "close":
            self.destroy()

    def _finish_turn(self) -> None:
        self.busy = False
        self.send_btn.configure(text="发送")
        self._rounds_since_memory += 1
        self._refresh_conv_list()
        if self.status_label.cget("text") == "思考中…":
            self._status("Enter 发送 · Shift+Enter 换行", 0)

    # ------------------------------------------------------------------
    # 记忆
    # ------------------------------------------------------------------
    def _maybe_memory(self) -> None:
        if not self.current or self._mem_running:
            return
        every = max(1, int(self.cfg.get("memory_every", 6)))
        if self._rounds_since_memory < every:
            return
        if self.current.user_turns() < 1:
            return

        self._rounds_since_memory = 0
        self._mem_running = True
        cfg = dict(self.cfg)
        msgs = list(self.current.messages)
        self._status("正在整理记忆…", 0)

        def work():
            try:
                memory.summarize(cfg, msgs)
                self.q.put({"type": "mem_ok"})
            except Exception as e:
                self.q.put({"type": "mem_fail", "msg": str(e)})

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _open_settings(self) -> None:
        def saved(new_cfg: dict) -> None:
            self.cfg = new_cfg
            try:
                ctk.set_appearance_mode(new_cfg.get("appearance", "light"))
            except Exception:
                pass
            self._update_model_label()

        SettingsWindow(self, self.cfg, on_saved=saved)

    def _open_skills(self) -> None:
        def changed(new_cfg: dict) -> None:
            self.cfg = new_cfg

        win = SkillWindow(self, self.cfg, on_changed=changed)
        win.after(200, win.lift)

    # ------------------------------------------------------------------
    # 关闭：先把记忆存下来再退出
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.cancel.set()

        # 关窗时如果还挂着确认框，按拒绝处理，别让后台线程干等
        try:
            if self._confirm_dlg is not None and self._confirm_dlg.winfo_exists():
                self._confirm_dlg._answer(False)
        except Exception:
            pass

        need = (
            self.current is not None
            and self.current.user_turns() > 0
            and config.is_configured(self.cfg)
        )
        if not need:
            self.destroy()
            return

        self._status("正在把这次对话写进记忆…", 0)
        cfg = dict(self.cfg)
        msgs = list(self.current.messages)

        def work():
            try:
                memory.summarize(cfg, msgs)
            except Exception:
                pass
            self.q.put({"type": "close"})

        threading.Thread(target=work, daemon=True).start()
        # 兜底：万一网络卡死，20 秒后强退
        self.after(20000, self._force_close)

    def _force_close(self) -> None:
        try:
            self.destroy()
        except Exception:
            pass


def run() -> None:
    config.ensure_dirs()
    app = App()
    app.mainloop()
