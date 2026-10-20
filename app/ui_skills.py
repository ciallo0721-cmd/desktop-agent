# -*- coding: utf-8 -*-
"""技能（Skill）管理窗口。

只做四件事：看有哪些技能、开关、打开目录、新建一个空技能模板。
真正读技能正文 / 跑技能脚本的是 agent_core 里的 skill / skill_run 工具。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import customtkinter as ctk

from . import config, skills

FONT = "Microsoft YaHei UI"

C_BG = "#F5F6F8"
C_BORDER = "#E1E4E9"
C_MUTED = "#7A8089"


class SkillWindow(ctk.CTkToplevel):
    def __init__(self, master, cfg: dict, on_changed=None):
        super().__init__(master)
        self.cfg = cfg
        self.on_changed = on_changed

        self.title("技能 Skill")
        self.geometry("640x680")
        self.minsize(520, 480)
        self.configure(fg_color=C_BG)
        self.transient(master)
        self.after(120, self.lift)

        skills.ensure_scaffold()

        # ---- 顶部 ----
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))

        self.master_var = ctk.BooleanVar(
            value=bool(cfg.get("skills_enabled", True)))
        self.sw_master = ctk.CTkSwitch(
            top, text="启用技能", variable=self.master_var,
            onvalue=True, offvalue=False, command=self._toggle_master,
        )
        self.sw_master.pack(side="left")

        ctk.CTkButton(
            top, text="打开目录", width=80, height=26, corner_radius=6,
            fg_color="#E6E9EE", hover_color="#D8DCE3", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=11),
            command=self._open_dir,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            top, text="刷新", width=60, height=26, corner_radius=6,
            fg_color="#E6E9EE", hover_color="#D8DCE3", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=11),
            command=self._reload,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            top, text="+ 新建", width=72, height=26, corner_radius=6,
            font=ctk.CTkFont(family=FONT, size=11),
            command=self._create,
        ).pack(side="right")

        ctk.CTkLabel(
            self,
            text=(f"技能放在 {config.SKILL_DIR}\n"
                  "每个技能一个文件夹，SKILL.md 开头的 description 会进系统提示词。"
                  "模型自己决定什么时候读正文。"),
            text_color=C_MUTED, anchor="w", justify="left",
            font=ctk.CTkFont(family=FONT, size=11),
        ).pack(fill="x", padx=16, pady=(0, 8))

        line = ctk.CTkFrame(self, fg_color=C_BORDER, height=1)
        line.pack(fill="x")

        # ---- 列表 ----
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.pack(fill="both", expand=True, padx=6, pady=6)

        self.status = ctk.CTkLabel(
            self, text="", text_color=C_MUTED, anchor="w",
            font=ctk.CTkFont(family=FONT, size=11),
        )
        self.status.pack(fill="x", padx=16, pady=(0, 8))

        self._reload()

    # ------------------------------------------------------------------
    def _reload(self) -> None:
        for w in self.list.winfo_children():
            w.destroy()

        items = skills.list_skills(self.cfg, fresh=True)
        if not items:
            ctk.CTkLabel(
                self.list, text="还没有技能。点右上角「+ 新建」建一个模板。",
                text_color=C_MUTED, font=ctk.CTkFont(family=FONT, size=12),
            ).pack(pady=24)
            self.status.configure(text="0 个技能")
            return

        for sk in items:
            self._row(sk)

        on = sum(1 for s in items if s.enabled)
        self.status.configure(text=f"共 {len(items)} 个技能，启用 {on} 个")

    def _row(self, sk: skills.Skill) -> None:
        card = ctk.CTkFrame(
            self.list, fg_color="#FFFFFF", corner_radius=8,
            border_width=1, border_color=C_BORDER,
        )
        card.pack(fill="x", pady=4, padx=6)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=10, pady=(8, 0))

        ctk.CTkLabel(
            head, text=sk.name, anchor="w",
            font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
            text_color="#1F2328",
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=f"  {sk.sid}", anchor="w",
            text_color=C_MUTED, font=ctk.CTkFont(family=FONT, size=10),
        ).pack(side="left")

        var = ctk.BooleanVar(value=sk.enabled)
        sw = ctk.CTkSwitch(
            head, text="", width=44, variable=var, onvalue=True, offvalue=False,
            command=lambda s=sk.sid, v=var: self._toggle_one(s, v),
        )
        sw.pack(side="right")

        desc = " ".join((sk.description or "（没写 description，模型看不出它干什么）").split())
        ctk.CTkLabel(
            card, text=desc, anchor="w", justify="left", wraplength=520,
            text_color="#4A5058", font=ctk.CTkFont(family=FONT, size=11),
        ).pack(fill="x", padx=12, pady=(2, 0))

        files = sk.attachments()
        foot = " · ".join(files[:4]) + (" …" if len(files) > 4 else "")
        ctk.CTkLabel(
            card, text=foot, anchor="w", justify="left", wraplength=520,
            text_color=C_MUTED, font=ctk.CTkFont(family=FONT, size=10),
        ).pack(fill="x", padx=12)

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkButton(
            btns, text="编辑 SKILL.md", width=116, height=24, corner_radius=6,
            fg_color="#EDEFF3", hover_color="#DEE2E9", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=10),
            command=lambda p=sk.skill_file: self._open_path(p),
        ).pack(side="left")
        ctk.CTkButton(
            btns, text="所在文件夹", width=96, height=24, corner_radius=6,
            fg_color="#EDEFF3", hover_color="#DEE2E9", text_color="#33383F",
            font=ctk.CTkFont(family=FONT, size=10),
            command=lambda p=sk.folder: self._open_path(p),
        ).pack(side="left", padx=6)

    # ------------------------------------------------------------------
    def _toggle_one(self, sid: str, var) -> None:
        skills.set_enabled(self.cfg, sid, bool(var.get()))
        self._notify()
        self.status.configure(
            text=f"{sid} 已{'启用' if var.get() else '关闭'}（下一轮对话生效）")

    def _toggle_master(self) -> None:
        self.cfg["skills_enabled"] = bool(self.master_var.get())
        config.save_config(self.cfg)
        self._notify()
        self.status.configure(
            text="技能总开关：" + ("开" if self.master_var.get() else "关"))

    def _notify(self) -> None:
        if self.on_changed:
            try:
                self.on_changed(self.cfg)
            except Exception:
                pass

    def _open_dir(self) -> None:
        skills.ensure_scaffold()
        self._open_path(config.SKILL_DIR)

    def _open_path(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            self.status.configure(text=f"打不开：{e}")

    def _create(self) -> None:
        root = config.SKILL_DIR
        root.mkdir(parents=True, exist_ok=True)
        base = "new-skill"
        folder = root / base
        n = 2
        while folder.exists():
            folder = root / f"{base}-{n}"
            n += 1
        try:
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"""---
name: {folder.name}
description: 一句话说明这个技能干什么、什么时候该用（这句会进系统提示词）
when: 更具体的触发条件，可以留空
enabled: true
---
正文写清楚步骤。比如：

1. 先检查 xxx 是否存在：`ls workspace/`
2. 需要跑脚本就用 skill_run，命令是 `python scripts/run.py`
3. 结果怎么汇报给用户

只写模型自己猜不到的东西，通用常识不用抄。
""",
                encoding="utf-8",
            )
        except OSError as e:
            self.status.configure(text=f"创建失败：{e}")
            return
        self._reload()
        self._open_path(folder / "SKILL.md")

    # ------------------------------------------------------------------
    def refresh_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.master_var.set(bool(cfg.get("skills_enabled", True)))
        self._reload()
