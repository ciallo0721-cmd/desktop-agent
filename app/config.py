# -*- coding: utf-8 -*-
"""路径常量 + 配置文件读写 + 开机自启。

所有路径都相对项目根目录，整个文件夹可以随便搬。
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONV_DIR = DATA_DIR / "conversations"
CONV_TRASH_DIR = DATA_DIR / "trash"
CONV_BACKUP_DIR = DATA_DIR / "backup"
SHOT_DIR = DATA_DIR / "screenshots"
WORK_DIR = BASE_DIR / "workspace"
PROMPT_DIR = BASE_DIR / "prompt"
CONFIG_PATH = DATA_DIR / "config.json"

AUTOSTART_NAME = "WorkBuddyDesktopAgent"


DEFAULT_CONFIG = {
    "user_name": "用户",
    # 主对话模型
    "main_api": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
    },
    # 识图模型（用来"看屏幕"）
    "vision_api": {"base_url": "", "api_key": "", "model": ""},
    # 记忆总结模型（留空则复用主模型）
    "memory_api": {"base_url": "", "api_key": "", "model": ""},

    # agent 交互方式: "bash" | "screen" | "both"
    "agent_mode": "both",
    "bash_path": "",
    "bash_timeout": 120,
    "max_steps": 12,
    "history_limit": 24,

    "sandbox": True,
    "screen_control": True,
    # 联网：默认开。不填 Key 也能用（抓 360 / Bing 结果页），
    # 填了 provider + api_key 就走 Tavily / Serper 的正式接口。
    "web_search": True,
    "search_results": 8,
    "search_api": {"provider": "", "api_key": ""},
    # 截图直接塞给主模型看，不走「识图模型写描述再转述」——后者必然丢信息
    "vision_passthrough": True,
    # 截图缩放比，1.0 = 原图不缩放，坐标零误差
    "screenshot_scale": 1.0,
    "autostart": False,
    "temperature": 0.7,
    "memory_every": 6,
    "appearance": "light",

    "prompts": {
        "system": "prompt/systemprompt.md",
        "role": "prompt/roleprompt.md",
        "memory": "prompt/memory.md",
        "persona": "prompt/人设.md",
    },
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, CONV_DIR, CONV_TRASH_DIR, CONV_BACKUP_DIR,
              SHOT_DIR, WORK_DIR, PROMPT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 合并进 base，dict 递归，其他直接覆盖。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_configured(cfg: dict) -> bool:
    return bool((cfg.get("main_api") or {}).get("api_key", "").strip())


def api_of(cfg: dict, which: str) -> dict:
    """取某一路 API 配置；memory 未配时回落到 main。"""
    section = cfg.get(which) or {}
    if which == "memory_api" and not section.get("api_key"):
        return cfg.get("main_api") or {}
    return section


def resolve_prompt_path(cfg: dict, key: str) -> Path:
    rel = (cfg.get("prompts") or {}).get(key) or f"prompt/{key}.md"
    p = Path(rel)
    return p if p.is_absolute() else BASE_DIR / p


def set_autostart(enable: bool) -> bool:
    """写 / 删 HKCU 的 Run 键。返回是否成功。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = str(pyw if pyw.exists() else exe)
    script = str(BASE_DIR / "agent.py")
    cmd = f'"{runner}" "{script}"'

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as k:
            if enable:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def is_autostart_on() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
        return True
    except Exception:
        return False


def find_bash(cfg: dict | None = None) -> str | None:
    """找一个可用的 bash（优先 Git Bash，跳过 WSL 的 System32\\bash.exe）。"""
    import shutil

    if cfg:
        custom = (cfg.get("bash_path") or "").strip()
        if custom and Path(custom).exists():
            return custom

    candidates = [
        r"D:\Git\bin\bash.exe",
        r"D:\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c

    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found

    wt = shutil.which("wsl")
    if wt:
        return None  # 有 wsl 但不当作 bash 用
    return None
