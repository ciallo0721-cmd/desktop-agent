# -*- coding: utf-8 -*-
"""setup_env.py —— 首次运行引导 + 环境自检 + 依赖安装 + 拉起 agent。

first.bat 负责找到 Python，然后调这个脚本；这个脚本负责剩下所有事。
只用标准库，因为第一次跑的时候什么都还没装。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
REQ_PATH = BASE_DIR / "requirements.txt"
VENV_DIR = BASE_DIR / ".venv"

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREY = "\033[90m"

AGENT_ART = r"""
      +---------------------------------------+
      |    __                         __       |
      |   /  \                       /  \      |
      |   \__/                       \__/      |
      |                                       |
      |            +-------------+            |
      |            |  A  G  E  N  T  |         |
      |            +-------------+            |
      |                                       |
      |    +------+  +------+  +-------+      |
      |    | bash |  | shot |  | click |      |
      |    +------+  +------+  +-------+      |
      +------------------+--------------------+
                         |
                     +-------+
                     |  OK ! |
                     +-------+
"""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def enable_ansi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def show(text: str) -> None:
    print(text, flush=True)


def rule(title: str = "") -> None:
    line = "─" * 52
    if title:
        show(f"\n{CYAN}{line}{RESET}\n{BOLD} {title}{RESET}\n{CYAN}{line}{RESET}")
    else:
        show(f"{CYAN}{line}{RESET}")


def ask(label: str, default: str = "", secret: bool = False,
        allow_empty: bool = False) -> str:
    hint = f"{GREY}[{default}]{RESET} " if default else ""
    if secret and default:
        hint = f"{GREY}[已保存，回车保持不变]{RESET} "
    while True:
        try:
            raw = input(f"  {label} {hint}").strip()
        except (EOFError, KeyboardInterrupt):
            show("\n已取消。")
            raise SystemExit(1)
        if not raw:
            if default:
                return default
            if allow_empty:
                return ""
            show(f"  {YELLOW}这个不能空，重新输入一下{RESET}")
            continue
        return raw


def ask_yes(label: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        raw = input(f"  {label} [{d}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(1)
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1")


def ok(msg: str) -> None:
    show(f"  {GREEN}[✔]{RESET} {msg}")


def bad(msg: str) -> None:
    show(f"  {RED}[✘]{RESET} {msg}")


def warn(msg: str) -> None:
    show(f"  {YELLOW}[!]{RESET} {msg}")


def info(msg: str) -> None:
    show(f"  {GREY}·{RESET} {msg}")


# --------------------------------------------------------------------------
# 环境检测
# --------------------------------------------------------------------------

def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def check_env() -> dict:
    rule("环境自检")
    env: dict[str, object] = {}

    # Python
    env["python"] = sys.version.split()[0]
    ok(f"Python {sys.version.split()[0]}  ({sys.executable})")

    # cmd
    cmd_path = which("cmd")
    env["cmd"] = cmd_path
    if cmd_path:
        ok("cmd.exe 正常")
    else:
        warn("没找到 cmd.exe（很少见，可能被环境变量搞坏了）")

    # git
    git = which("git")
    env["git"] = git
    if git:
        try:
            v = subprocess.run([git, "--version"], capture_output=True, text=True,
                               timeout=15).stdout.strip()
        except Exception:
            v = "git"
        ok(f"{v}")
    else:
        bad("没有 Git")

    # git bash
    bash = find_bash_simple()
    env["bash"] = bash
    if bash:
        ok(f"Bash: {bash}")
    else:
        bad("没有 Bash（Git Bash）")

    # Linux shell
    wsl = which("wsl")
    if wsl:
        try:
            r = subprocess.run([wsl, "-l", "-q"], capture_output=True, timeout=12)
            distros = r.stdout.decode("utf-16-le", "ignore").strip() or \
                r.stdout.decode("utf-8", "ignore").strip()
            if distros:
                info(f"WSL 已安装：{', '.join(d for d in distros.splitlines() if d.strip())}")
            else:
                info("WSL 存在但没有装发行版")
        except Exception:
            info("WSL 存在（状态未知）")
    else:
        info("没有 WSL —— 不自动装，需要时自己执行 wsl --install")

    return env


def find_bash_simple() -> str | None:
    for c in (
        r"D:\Git\bin\bash.exe",
        r"D:\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(c).exists():
            return c
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def winget_install(pkg_id: str, name: str) -> bool:
    winget = which("winget")
    if not winget:
        bad(f"没有 winget，没法自动装 {name}")
        show(f"    手动去下：https://git-scm.com/download/win")
        return False

    warn(f"正在用 winget 安装 {name} …")
    cmd = [
        winget, "install", "-e", "--id", pkg_id, "--silent",
        "--accept-source-agreements", "--accept-package-agreements",
    ]
    try:
        rc = subprocess.run(cmd).returncode
    except Exception as e:
        bad(f"安装失败：{e}")
        return False

    if rc == 0:
        ok(f"{name} 装好了")
        return True
    bad(f"{name} 安装失败（winget 返回 {rc}）")
    return False


def ensure_git() -> bool:
    if which("git"):
        return True
    rule("缺少 Git，尝试自动安装")
    if not winget_install("Git.Git", "Git for Windows"):
        return False
    # 装完刷新 PATH
    for p in (
        r"C:\Program Files\Git\cmd",
        r"D:\Git\cmd",
        r"C:\Program Files\Git\bin",
        r"D:\Git\bin",
    ):
        if Path(p).exists() and p not in os.environ.get("PATH", ""):
            os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]
    if which("git"):
        ok("Git 已在 PATH 里")
        return True
    warn("装好了但当前终端 PATH 还没刷新，重启一下终端就正常")
    return True


# --------------------------------------------------------------------------
# 问答
# --------------------------------------------------------------------------

def test_api(base_url: str, api_key: str, model: str) -> bool:
    url = (base_url or "").rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：在的"}],
        "max_tokens": 16,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        msg = body["choices"][0]["message"]["content"]
        ok(f"通了，模型回：{msg.strip()[:30]}")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        bad(f"HTTP {e.code}：{detail}")
    except Exception as e:
        bad(f"{type(e).__name__}: {e}")
    return False


def wizard() -> dict:
    rule("第 1 步 / 4 —— 主对话模型")
    show(f"{GREY}  agent 的大脑。任何 OpenAI 兼容接口都行{RESET}")
    while True:
        base_url = ask("接口地址", "https://api.openai.com/v1")
        api_key = ask("API Key")
        model = ask("模型名", "")
        show(f"{GREY}  正在测试连接 …{RESET}")
        if test_api(base_url, api_key, model):
            break
        if ask_yes("连接没通，重填一次？（选 n 就先存着，之后在设置里改）", True):
            continue
        break

    rule("第 2 步 / 4 —— 识图模型（让 agent 能看见屏幕）")
    show(f"{GREY}  不填也行，只是之后只能用命令行模式。回车跳过{RESET}")
    vis = {"base_url": "", "api_key": "", "model": ""}
    if ask_yes("现在配识图模型？", False):
        vis["base_url"] = ask("接口地址", base_url)
        vis["api_key"] = ask("API Key")
        vis["model"] = ask("模型名（要支持视觉，比如 gpt-4o / qwen-vl-max）")
        show(f"{GREY}  测试中 …{RESET}")
        if not test_api(vis["base_url"], vis["api_key"], vis["model"]):
            warn("识图模型没通过测试，先用着，回头在设置里改")

    rule("第 3 步 / 4 —— 记忆总结模型")
    show(f"{GREY}  每次对话结束用它把内容压成长期记忆。回车 = 用主模型{RESET}")
    mem = {"base_url": "", "api_key": "", "model": ""}
    if ask_yes("单独配一个记忆模型？", False):
        mem["base_url"] = ask("接口地址", base_url)
        mem["api_key"] = ask("API Key")
        mem["model"] = ask("模型名")

    rule("第 4 步 / 4 —— 称呼")
    user_name = ask("你想让我叫你什么", "用户")

    return {
        "user_name": user_name,
        "main_api": {"base_url": base_url, "api_key": api_key, "model": model},
        "vision_api": vis,
        "memory_api": mem,
        "agent_mode": "both" if vis.get("api_key") else "bash",
        "bash_path": "",
        "bash_timeout": 120,
        "max_steps": 12,
        "history_limit": 24,
        "sandbox": True,
        "screen_control": True,
        "autostart": False,
        "temperature": 0.7,
        "memory_every": 6,
        "appearance": "light",
        "skills_enabled": True,
        "skills_disabled": [],
        "skill_timeout": 120,
        "prompts": {
            "system": "prompt/systemprompt.md",
            "role": "prompt/roleprompt.md",
            "memory": "prompt/memory.md",
            "persona": "prompt/人设.md",
        },
    }


# --------------------------------------------------------------------------
# 依赖
# --------------------------------------------------------------------------

def venv_python() -> Path:
    return VENV_DIR / "Scripts" / "python.exe"


def ensure_venv() -> Path:
    rule("Python 虚拟环境")
    py = venv_python()
    if py.exists():
        ok(f".venv 已存在")
        return py

    info("正在创建 .venv（放在项目目录里，不占 C 盘）…")
    rc = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        capture_output=True,
    ).returncode
    if rc != 0 or not py.exists():
        bad("venv 创建失败，退回系统 Python")
        return Path(sys.executable)
    ok(" .venv 创建完成")
    return py


def deps_ok(py: Path) -> bool:
    probe = "import customtkinter, requests, PIL, pyautogui, pyperclip"
    try:
        return subprocess.run(
            [str(py), "-c", probe], capture_output=True, timeout=60
        ).returncode == 0
    except Exception:
        return False


def install_deps(py: Path) -> bool:
    if deps_ok(py):
        ok("依赖齐全")
        return True

    rule("安装依赖")
    if not REQ_PATH.exists():
        bad("requirements.txt 不见了")
        return False

    subprocess.run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        capture_output=True,
    )

    # 先官方源（最全），失败再走国内镜像
    info("从官方 PyPI 安装 …")
    if subprocess.run(
        [str(py), "-m", "pip", "install", "-r", str(REQ_PATH)]
    ).returncode != 0:
        warn("官方源失败，换清华镜像重试")
        subprocess.run(
            [str(py), "-m", "pip", "install", "-r", str(REQ_PATH), "-i", MIRROR]
        )

    if deps_ok(py):
        ok("依赖装好了")
        return True
    bad("依赖还是缺，手动跑：.venv\\Scripts\\python -m pip install -r requirements.txt")
    return False


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def launch(py: Path) -> None:
    runner = py.with_name("pythonw.exe")
    if not runner.exists():
        runner = py

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "agent.log"
    log = open(log_path, "w", encoding="utf-8")

    try:
        subprocess.Popen(
            [str(runner), str(BASE_DIR / "agent.py")],
            cwd=str(BASE_DIR),
            stdout=log,
            stderr=log,
        )
        ok("窗口已经拉起来了")
        info(f"日志：{log_path}")
    except Exception as e:
        bad(f"启动失败：{e}")
        return


def main() -> int:
    force_utf8()
    enable_ansi()

    show(f"\n{BOLD}{CYAN}  >>> Desktop Agent · 启动器 <<<{RESET}\n")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "workspace").mkdir(exist_ok=True)
    (BASE_DIR / "prompt").mkdir(exist_ok=True)
    (BASE_DIR / "skills").mkdir(exist_ok=True)

    # 1. 配置
    cfg: dict | None = None
    forced_setup = "--setup" in sys.argv
    if CONFIG_PATH.exists() and not forced_setup:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = None

    if not cfg or not (cfg.get("main_api") or {}).get("api_key"):
        cfg = wizard()
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rule("配置已保存")
        ok(str(CONFIG_PATH))
    else:
        ok(f"配置已存在，直接启动（想重配就跑 first.bat --setup）")
        info(f"模型：{(cfg.get('main_api') or {}).get('model')}")

    # 2. 环境
    check_env()
    ensure_git()

    # 3. 依赖
    py = ensure_venv()
    if not install_deps(py):
        input("\n  按回车退出 …")
        return 1

    # 4. 定妆
    show(GREEN + AGENT_ART + RESET)
    show(f"{BOLD}  {GREEN}一切就绪，启动 agent …{RESET}\n")

    launch(py)

    show(f"{GREY}  这个控制台可以关掉了。{RESET}\n")
    time.sleep(1.2)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        show("\n 已中断。")
        raise SystemExit(1)
