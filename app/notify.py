# -*- coding: utf-8 -*-
"""通知推送：定时任务跑完后告诉用户一声。

两条通道，可独立开关（data/config.json 的 notify 字段）：
  windows    Windows 系统通知（右下角气泡，走 PowerShell，零依赖）
  telegram   TG 推给 allowed_users 里的第一个人（要 TG 通道已配好）

config:
  "notify": {
    "task_done": true,     // 总开关：定时任务完成时通知
    "windows": true,
    "telegram": true
  }
"""
from __future__ import annotations

import subprocess
import sys
import threading

from . import config

# PowerShell 弹 Windows 通知：NotifyIcon 气泡，不需要任何第三方库
_PS_TEMPLATE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.Visible = $true;"
    "$n.ShowBalloonTip(6000, '{title}', '{text}', "
    "[System.Windows.Forms.ToolTipIcon]::Info);"
    "Start-Sleep -Seconds 7;$n.Dispose()"
)


def _windows_toast(title: str, text: str) -> None:
    if sys.platform != "win32":
        return
    # PowerShell 单引号字符串里把 ' 翻倍转义；剥掉换行防止注入
    t = title.replace("'", "''").replace("\r", " ").replace("\n", " ")[:120]
    m = text.replace("'", "''").replace("\r", " ").replace("\n", " ")[:200]
    ps = _PS_TEMPLATE.format(title=t, text=m)
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def _telegram_push(text: str) -> None:
    try:
        from . import channel_telegram

        channel_telegram.push_to_owner(text)
    except Exception:
        pass


def push(cfg: dict, title: str, text: str) -> None:
    """按配置把通知发出去。Windows 通道异步，TG 通道在后台线程里也异步。"""
    conf = cfg.get("notify") or {}
    if conf.get("windows", True):
        threading.Thread(
            target=_windows_toast, args=(title, text), daemon=True
        ).start()
    if conf.get("telegram", True):
        threading.Thread(
            target=_telegram_push, args=(f"{title}\n{text}",), daemon=True
        ).start()


def push_task_done(cfg: dict, task_name: str, ok: bool, summary: str) -> None:
    if not (cfg.get("notify") or {}).get("task_done", True):
        return
    head = "✅ 定时任务完成" if ok else "❌ 定时任务失败"
    body = f"{task_name}\n{(summary or '（无输出）')[:300]}"
    push(cfg, head, body)
