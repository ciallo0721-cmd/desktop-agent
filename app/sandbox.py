# -*- coding: utf-8 -*-
"""真隔离：Windows Sandbox（.wsb）执行通道。

和 tools.py 里那个「钉目录 + 正则」的纸糊 sandbox 不同，这里是真的：
命令跑在 Windows 沙箱虚拟机里，和宿主机文件系统、注册表完全隔离。

工作方式：
  1. 把要执行的命令写进 data/sandbox/run.cmd（映射进沙箱，可读写）
  2. 项目根目录只读映射进沙箱（能看代码，改不了）
  3. 生成 session.wsb，用 WindowsSandbox.exe 启动
  4. LogonCommand 自动跑 run.cmd，输出重定向到映射目录里的 out.txt
  5. 宿主机轮询 out.txt，看到结束标记就 taskkill 关掉沙箱，取回输出

前置条件：
  - Windows 10/11 专业版（或企业版/教育版），家庭版没有这个功能
  - 「启用或关闭 Windows 功能」里勾选 Windows 沙盒

限制：
  - 每条命令都要起一个全新沙箱，冷启动 10~20 秒，慢但真隔离
  - 沙箱里没有 Git Bash，命令一律用 CMD 语法
  - 一次只跑一条（沙箱实例加锁），后面的排队等
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config

DONE_MARK = "__PILOT_DONE__"

_lock = threading.Lock()

SANDBOX_EXE = r"C:\Windows\System32\WindowsSandbox.exe"


def wsb_available() -> bool:
    return sys.platform == "win32" and Path(SANDBOX_EXE).exists()


def _guest(host: Path) -> str:
    """宿主机路径 → 沙箱内 Desktop 下的对应路径。"""
    return "C:\\Users\\WDAGUtilityAccount\\Desktop\\" + host.name


def _taskkill_sandbox() -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "WindowsSandbox.exe"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def run_in_sandbox(cfg: dict, cmd: str, workdir: str | None = None,
                   timeout: int | None = None) -> str:
    """在 Windows 沙箱里跑一条 CMD 命令，取回输出。"""
    if not cmd.strip():
        return "错误：命令为空。"
    if not wsb_available():
        return (
            "【真隔离不可用】这台机器没有 Windows Sandbox。"
            "需要 Win10/11 专业版，并在「启用或关闭 Windows 功能」里勾选"
            "「Windows 沙盒」。当前请退回普通模式（sandbox_mode 改回 off）。"
        )
    if not _lock.acquire(blocking=False):
        return "【排队】沙箱一次只能跑一条命令，上一条还没结束，稍后再发。"

    timeout = max(60, int(timeout or cfg.get("bash_timeout", 120) or 120))
    sandbox_dir = config.DATA_DIR / "sandbox"
    try:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        return _run(cfg, cmd, workdir, timeout, sandbox_dir)
    finally:
        _lock.release()


def _run(cfg: dict, cmd: str, workdir: str | None, timeout: int,
         sandbox_dir: Path) -> str:
    run_cmd = sandbox_dir / "run.cmd"
    out_txt = sandbox_dir / "out.txt"
    wsb_file = sandbox_dir / "session.wsb"

    # 清掉上一次的输出
    out_txt.unlink(missing_ok=True)

    out_guest = _guest(sandbox_dir)
    project_guest = _guest(config.BASE_DIR)
    cwd_guest = _guest(Path(workdir)) if workdir else project_guest

    # run.cmd：CMD 语法，输出全部重定向到映射目录
    script = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f"cd /d \"{cwd_guest}\"\r\n"
        f"({cmd}) > \"{out_guest}\\out.txt\" 2>&1\r\n"
        f"echo {DONE_MARK} %ERRORLEVEL% >> \"{out_guest}\\out.txt\"\r\n"
    )
    run_cmd.write_text(script, encoding="utf-8", errors="replace")

    # session.wsb：项目只读，沙箱临时目录可写；可选禁网
    def folder(host: Path, ro: bool) -> str:
        flag = "<ReadOnly>true</ReadOnly>" if ro else "<ReadOnly>false</ReadOnly>"
        return (f"      <MappedFolder><HostFolder>{host}</HostFolder>"
                f"{flag}</MappedFolder>")

    mappings = [
        folder(sandbox_dir, False),
        folder(config.BASE_DIR, True),
    ]
    if workdir:
        try:
            mappings.append(folder(Path(workdir), False))
        except OSError:
            pass
    net = "<Networking>Disable</Networking>" if not cfg.get("sandbox_network", False) else ""
    wsb = (
        "<Configuration>\r\n"
        "  <MappedFolders>\r\n" + "\r\n".join(mappings) + "\r\n  </MappedFolders>\r\n"
        f"  <LogonCommand><Command>C:\\Windows\\System32\\cmd.exe /c "
        f"\"{_guest(sandbox_dir)}\\run.cmd\"</Command></LogonCommand>\r\n"
        f"  {net}\r\n"
        "</Configuration>\r\n"
    )
    wsb_file.write_text(wsb, encoding="utf-8")

    started = time.time()
    try:
        subprocess.Popen([SANDBOX_EXE, str(wsb_file)])
    except Exception as e:
        return f"【沙箱启动失败】{type(e).__name__}: {e}"

    # 轮询 out.txt 等结束标记
    deadline = started + timeout
    raw = ""
    rc = None
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            raw = out_txt.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if DONE_MARK in raw:
            head, _, tail = raw.rpartition(DONE_MARK)
            try:
                rc = int(tail.strip().split()[0])
            except (ValueError, IndexError):
                rc = -1
            raw = head
            break

    _taskkill_sandbox()
    elapsed = time.time() - started

    if rc is None:
        return (
            f"【沙箱超时】命令在沙箱里跑了 {timeout}s 没结束，沙箱已强制关闭。\n"
            "提示：每条沙箱命令都有 10~20s 的启动开销，长任务请调大 bash_timeout。"
        )

    raw = raw.strip()
    parts = [f"$ {cmd}", f"[沙箱隔离执行 · 退出码 {rc} · {elapsed:.1f}s · cwd={cwd_guest}]"]
    if raw:
        parts.append("--- 输出 ---\n" + _clip(raw))
    else:
        parts.append("（没有任何输出）")
    parts.append("（以上输出来自一次性 Windows Sandbox 虚拟机，宿主机未受任何影响）")
    return "\n".join(parts)


def _clip(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n\n...[输出过长，省略 {len(text) - limit} 字符]...\n\n" + text[-limit // 2:]
