# -*- coding: utf-8 -*-
"""agent.py —— 桌面 Agent 入口。

直接双击 first.bat 会先做环境检查再调这个文件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)


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


def main() -> int:
    try:
        import customtkinter  # noqa: F401
        import requests  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as e:
        _missing_dep(e)
        return 1

    from app.ui_main import run
    from app.agent_core import daily_backup

    daily_backup()
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
