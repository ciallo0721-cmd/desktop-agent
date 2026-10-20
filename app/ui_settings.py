# -*- coding: utf-8 -*-
"""设置窗口。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from . import config, llm


class Field:
    """一个 label + entry 的小分组。"""

    def __init__(self, master, label: str, value: str, show: str | None = None,
                 width: int = 230):
        self.row = ctk.CTkFrame(master, fg_color="transparent")
        self.row.pack(fill="x", pady=3)
        ctk.CTkLabel(self.row, text=label, width=90, anchor="w").pack(side="left")
        self.var = tk.StringVar(value=value or "")
        self.entry = ctk.CTkEntry(self.row, textvariable=self.var, width=width,
                                  show=show or "")
        self.entry.pack(side="left", fill="x", expand=True)

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, v: str) -> None:
        self.var.set(v or "")


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, master, cfg: dict, on_saved=None):
        super().__init__(master)
        self.cfg = cfg
        self.on_saved = on_saved
        self.title("设置")
        self.geometry("640x760")
        self.transient(master)

        self.after(120, self.lift)
        self.after(200, self.grab_set)

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=4, pady=4)

        # ---------- 基本 ----------
        self._section(scroll, "基本")
        self.f_user = Field(scroll, "用户名", cfg.get("user_name", ""))

        # ---------- 主模型 ----------
        self._section(scroll, "主对话 API")
        main = cfg.get("main_api") or {}
        self.f_main_url = Field(scroll, "接口地址", main.get("base_url", ""))
        self.f_main_key = Field(scroll, "API Key", main.get("api_key", ""), show="*")
        self.f_main_model = Field(scroll, "模型名", main.get("model", ""))
        ctk.CTkLabel(
            scroll, text="接口地址填到 /v1 就行，不要带 /chat/completions",
            text_color="#888888", anchor="w", font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=8)

        # ---------- 识图 ----------
        self._section(scroll, "识图 API（看屏幕用，可留空）")
        vis = cfg.get("vision_api") or {}
        self.f_vis_url = Field(scroll, "接口地址", vis.get("base_url", ""))
        self.f_vis_key = Field(scroll, "API Key", vis.get("api_key", ""), show="*")
        self.f_vis_model = Field(scroll, "模型名", vis.get("model", ""))

        # ---------- 记忆 ----------
        self._section(scroll, "记忆总结 API（留空则复用主模型）")
        mem = cfg.get("memory_api") or {}
        self.f_mem_url = Field(scroll, "接口地址", mem.get("base_url", ""))
        self.f_mem_key = Field(scroll, "API Key", mem.get("api_key", ""), show="*")
        self.f_mem_model = Field(scroll, "模型名", mem.get("model", ""))

        # ---------- Agent ----------
        self._section(scroll, "Agent 行为")
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text="交互方式", width=90, anchor="w").pack(side="left")
        _mode_label = {
            "screen": "截屏操作",
            "both": "两者都用",
        }.get(cfg.get("agent_mode", "both"), "命令行 Bash")
        self.mode_var = tk.StringVar(value=_mode_label)
        ctk.CTkOptionMenu(
            row, values=["两者都用", "命令行 Bash", "截屏操作"],
            variable=self.mode_var, width=160,
        ).pack(side="left")
        ctk.CTkLabel(
            row, text="", width=10
        ).pack(side="left")

        self.f_bash = Field(scroll, "Bash 路径", cfg.get("bash_path", ""))
        detected = config.find_bash(cfg) or "未找到"
        ctk.CTkLabel(scroll, text=f"自动检测：{detected}", text_color="#888888",
                     anchor="w", font=ctk.CTkFont(size=11)).pack(fill="x", padx=8)

        self.sw_sandbox = self._switch(
            scroll, "沙箱模式（命令工作目录锁在 workspace/）",
            bool(cfg.get("sandbox", True)),
        )
        self.sw_screen = self._switch(
            scroll, "允许屏幕控制（点击 / 打字 / 按键）",
            bool(cfg.get("screen_control", True)),
        )
        self.sw_passthrough = self._switch(
            scroll, "截图直接给模型看（不走识图中转，强烈建议开）",
            bool(cfg.get("vision_passthrough", True)),
        )
        self.sw_web = self._switch(
            scroll, "联网搜索（web_search / web_fetch）",
            bool(cfg.get("web_search", True)),
        )
        self.sw_auto = self._switch(
            scroll, "开机自启", bool(cfg.get("autostart", False)),
        )

        row2 = ctk.CTkFrame(scroll, fg_color="transparent")
        row2.pack(fill="x", pady=3)
        ctk.CTkLabel(row2, text="最大连续步数", width=90, anchor="w").pack(side="left")
        self.v_steps = tk.StringVar(value=str(cfg.get("max_steps", 12)))
        ctk.CTkEntry(row2, textvariable=self.v_steps, width=55).pack(side="left")
        ctk.CTkLabel(row2, text="  记忆每", anchor="w").pack(side="left")
        self.v_memevery = tk.StringVar(value=str(cfg.get("memory_every", 6)))
        ctk.CTkEntry(row2, textvariable=self.v_memevery, width=45).pack(side="left")
        ctk.CTkLabel(row2, text=" 轮总结一次", anchor="w").pack(side="left")

        # ---------- Prompt 文件 ----------
        self._section(scroll, "Prompt 文件")
        btns = ctk.CTkFrame(scroll, fg_color="transparent")
        btns.pack(fill="x", pady=4)
        for key, label in (
            ("system", "系统提示"), ("role", "角色"), ("persona", "人设"), ("memory", "记忆"),
        ):
            ctk.CTkButton(
                btns, text=label, width=80, height=28,
                command=lambda k=key: self._open_prompt(k),
            ).pack(side="left", padx=3)

        # ---------- Skill ----------
        self._section(scroll, "Skill 技能")
        self.sw_skills = self._switch(
            scroll, "启用技能（skills/ 里的技能包会写进系统提示词）",
            bool(cfg.get("skills_enabled", True)),
        )
        ctk.CTkLabel(
            scroll,
            text=f"技能目录：{config.SKILL_DIR}\n"
                 "主界面左下角「技能」可以逐个开关、新建模板。",
            text_color="#888888", anchor="w", justify="left",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=8)

        # ---------- 底部按钮 ----------
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=10, pady=8)
        ctk.CTkButton(bottom, text="测试连接", width=90,
                      fg_color="#6b7280", hover_color="#4b5563",
                      command=self._test).pack(side="left")
        ctk.CTkButton(bottom, text="取消", width=80,
                      fg_color="#9ca3af", hover_color="#6b7280",
                      command=self.destroy).pack(side="right", padx=4)
        ctk.CTkButton(bottom, text="保存", width=100,
                      command=self._save).pack(side="right")

        self.status = ctk.CTkLabel(self, text="", text_color="#888888", anchor="w",
                                   wraplength=600, justify="left")
        self.status.pack(side="bottom", fill="x", padx=14, pady=(0, 6))

    # ------------------------------------------------------------------
    def _section(self, master, title: str) -> None:
        ctk.CTkLabel(
            master, text=title, anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(fill="x", padx=8, pady=(12, 2))

    def _switch(self, master, text: str, value: bool) -> ctk.CTkSwitch:
        var = tk.BooleanVar(value=value)
        sw = ctk.CTkSwitch(master, text=text, variable=var, onvalue=True,
                           offvalue=False)
        sw.pack(fill="x", padx=8, pady=4)
        sw.var = var
        return sw

    def _open_prompt(self, key: str) -> None:
        path = config.resolve_prompt_path(self.cfg, key)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {key}\n", encoding="utf-8")
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showwarning("打不开", str(e), parent=self)

    # ------------------------------------------------------------------
    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        cfg["user_name"] = self.f_user.get() or "用户"
        cfg["main_api"] = {
            "base_url": self.f_main_url.get(),
            "api_key": self.f_main_key.get(),
            "model": self.f_main_model.get(),
        }
        cfg["vision_api"] = {
            "base_url": self.f_vis_url.get(),
            "api_key": self.f_vis_key.get(),
            "model": self.f_vis_model.get(),
        }
        cfg["memory_api"] = {
            "base_url": self.f_mem_url.get(),
            "api_key": self.f_mem_key.get(),
            "model": self.f_mem_model.get(),
        }
        cfg["agent_mode"] = {
            "截屏操作": "screen",
            "两者都用": "both",
        }.get(self.mode_var.get(), "bash")
        cfg["bash_path"] = self.f_bash.get()
        cfg["sandbox"] = bool(self.sw_sandbox.var.get())
        cfg["screen_control"] = bool(self.sw_screen.var.get())
        cfg["vision_passthrough"] = bool(self.sw_passthrough.var.get())
        cfg["web_search"] = bool(self.sw_web.var.get())
        cfg["skills_enabled"] = bool(self.sw_skills.var.get())
        cfg["autostart"] = bool(self.sw_auto.var.get())
        try:
            cfg["max_steps"] = max(1, int(self.v_steps.get()))
        except ValueError:
            cfg["max_steps"] = 12
        try:
            cfg["memory_every"] = max(1, int(self.v_memevery.get()))
        except ValueError:
            cfg["memory_every"] = 6
        return cfg

    def _save(self) -> None:
        cfg = self._collect()
        if not cfg["main_api"]["api_key"]:
            messagebox.showwarning("缺少 Key", "主 API Key 是必填的。", parent=self)
            return

        old_auto = bool(self.cfg.get("autostart", False))
        if cfg["autostart"] != old_auto or cfg["autostart"]:
            ok = config.set_autostart(cfg["autostart"])
            if cfg["autostart"] and not ok:
                messagebox.showwarning(
                    "开机自启失败", "写注册表失败，可能被安全软件拦了。", parent=self
                )
                cfg["autostart"] = False

        config.save_config(cfg)
        self.cfg = cfg
        if self.on_saved:
            self.on_saved(cfg)
        self.destroy()

    def _test(self) -> None:
        cfg = self._collect()
        self.status.configure(text="正在测试主模型 …")

        def work():
            try:
                reply = llm.chat_once(
                    cfg["main_api"],
                    [{"role": "user", "content": "回复两个字：在的"}],
                    max_tokens=20,
                )
                text = f"主模型 OK：{reply[:40]}"
            except Exception as e:
                text = f"主模型失败：{e}"

            vis = cfg["vision_api"]
            if vis.get("api_key") and vis.get("model"):
                try:
                    llm.chat_once(
                        vis,
                        [{"role": "user", "content": "回复两个字：在的"}],
                        max_tokens=20,
                    )
                    text += " | 识图模型 OK"
                except Exception as e:
                    text += f" | 识图失败：{str(e)[:60]}"

            self.after(0, lambda: self.status.configure(text=text[:200]))

        threading.Thread(target=work, daemon=True).start()
