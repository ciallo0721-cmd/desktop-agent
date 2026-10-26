# -*- coding: utf-8 -*-
"""agent.py —— PawPilot 桌面 Agent 入口。

四种前端，共用同一套对话和工具：
    python agent.py            图形界面（默认）
    python agent.py cli        命令行
    python agent.py tui        终端界面
    python agent.py web        http://127.0.0.1:16800

直接双击 first.bat 会先做环境检查再调这个文件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

MODES = ("gui", "cli", "tui", "web")


def _missing_dep(exc: Exception) -> None:
    msg = (
        "缺少依赖，先跑一遍 first.bat。\n\n"
        f"具体错误：{exc}\n\n"
        "手动修复：\n"
        "  .venv\\Scripts\\python -m pip install -r requirements.txt"
    )
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("启动失败", msg)
        root.destroy()
    except Exception:
        print(msg)


def _parse_mode(argv: list[str]) -> str:
    """认 `agent.py cli`、`agent.py --mode cli`、`agent.py -m cli` 三种写法。"""
    mode = "gui"
    for i, a in enumerate(argv[1:]):
        low = a.lower()
        if low in ("--mode", "-m"):
            if i + 2 < len(argv):
                return argv[i + 2].lower()
            return "gui"
        if low.lstrip("-") in MODES:
            mode = low.lstrip("-")
    return mode


def main() -> int:
    mode = _parse_mode(sys.argv)
    if mode not in MODES:
        print(f"模式只能是 {' / '.join(MODES)}，收到：{mode}")
        return 2

    # GUI 需要图形库，其余模式不碰 tkinter，别因为没装就起不来
    if mode == "gui":
        try:
            import customtkinter  # noqa: F401
            import requests  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as e:
            _missing_dep(e)
            return 1

    from app.agent_core import daily_backup

    daily_backup()

    # 后台服务：定时任务 + Telegram 通道（没启用就自动跳过）
    try:
        from app import config, market, scheduler, channel_telegram

        market.ensure_scaffold()
        _cfg = config.load_config()
        if config.is_configured(_cfg):
            if _cfg.get("scheduler_enabled", True):
                scheduler.start()
            channel_telegram.start()
    except Exception:
        pass

    if mode == "gui":
        from app.ui_main import run
        run()
        return 0

    if mode == "cli":
        from app.ui_cli import run as run_cli
        return run_cli()

    if mode == "tui":
        from app.ui_tui import run as run_tui
        return run_tui()

    from app.ui_web import run as run_web
    return run_web()


if __name__ == "__main__":
    raise SystemExit(main())
