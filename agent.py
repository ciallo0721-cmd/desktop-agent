# -*- coding: utf-8 -*-
"""agent.py —— PawPilot 桌面 Agent 入口。

四种前端，共用同一套对话和工具：
    python agent.py            web 界面（默认，自动开浏览器）
    python agent.py gui        图形界面（web 起不来时自动兜底）
    python agent.py cli        命令行
    python agent.py tui        终端界面

想要别的默认前端，改 data/config.json 的 default_mode。
直接双击 first.bat 会先做环境检查再调这个文件。
"""
from __future__ import annotations

import os
import socket
import sys
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

MODES = ("web", "gui", "cli", "tui")
FALLBACK_MODE = "web"


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


def _parse_mode(argv: list[str]) -> str | None:
    """认 `agent.py cli`、`agent.py --mode cli`、`agent.py -m cli` 三种写法。

    没指定就返回 None，交给 _default_mode() 决定。
    """
    for i, a in enumerate(argv[1:]):
        low = a.lower()
        if low in ("--mode", "-m"):
            if i + 2 < len(argv):
                return argv[i + 2].lower()
            return None
        if low.lstrip("-") in MODES:
            return low.lstrip("-")
    return None


def _default_mode() -> str:
    """配置里的 default_mode，读不到就用 web。"""
    try:
        from app import config

        m = str(config.load_config().get("default_mode") or "").strip().lower()
        return m if m in MODES else FALLBACK_MODE
    except Exception:
        return FALLBACK_MODE


def _gui_ready() -> tuple[bool, Exception | None]:
    """GUI 依赖是否齐。返回 (就绪, 出错异常)。"""
    try:
        import customtkinter  # noqa: F401
        import requests  # noqa: F401
        from PIL import Image  # noqa: F401

        return True, None
    except Exception as e:
        return False, e


def _port_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _run_web() -> int:
    from app.ui_web import PORT, run as run_web

    try:
        rc = run_web()
    except Exception as e:
        print(f"Web 模式启动失败：{type(e).__name__}: {e}")
        return 1
    if rc == 0:
        return 0
    # 端口占用通常是「已经开着一个了」——直接把人带到那个界面去
    if _port_alive(PORT):
        url = f"http://127.0.0.1:{PORT}"
        print(f"检测到 {url} 已有实例在跑，直接打开浏览器。")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return 0
    return rc


def _run_gui() -> int:
    ready, err = _gui_ready()
    if not ready:
        _missing_dep(err)  # type: ignore[arg-type]
        return 1
    from app.ui_main import run

    run()
    return 0


def main() -> int:
    requested = _parse_mode(sys.argv)
    if requested is not None and requested not in MODES:
        print(f"模式只能是 {' / '.join(MODES)}，收到：{requested}")
        return 2
    mode = requested or _default_mode()

    # 只有 GUI 需要图形库，其余模式不碰 tkinter
    if mode == "gui":
        ready, err = _gui_ready()
        if not ready:
            _missing_dep(err)  # type: ignore[arg-type]
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

    if mode == "web":
        rc = _run_web()
        if rc == 0:
            return 0
        print("Web 起不来，回退图形界面兜底喵。")
        if _gui_ready()[0]:
            return _run_gui()
        print("GUI 也起不来（依赖不全），试试：python agent.py cli")
        return rc

    if mode == "gui":
        return _run_gui()

    if mode == "cli":
        from app.ui_cli import run as run_cli
        return run_cli()

    if mode == "tui":
        from app.ui_tui import run as run_tui
        return run_tui()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
