# -*- coding: utf-8 -*-
"""工具实现：bash 执行、截屏、鼠标键盘。

说明：sandbox 不是真正的容器隔离，它做两件事：
  1. 把工作目录钉死在 workspace/，命令里 cd 不出去（靠 cwd 参数 + 前缀检查）
  2. 拦截高危命令
真隔离要靠虚拟机或容器，这个功能做不到，别指望它。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config

SCREENSHOT_MAX_WIDTH = 1600   # 仅显式要求缩放时使用，默认原图直出

# 虚拟桌面左上角在屏幕坐标系里的位置。
# 截图坐标 + 这个偏移 = 真实屏幕坐标。副屏在主屏右侧时通常是 (0, 0)。
_SCREEN_ORIGIN = (0, 0)

# 高危命令：命中后不是直接拒绝，而是弹窗让用户自己拍板。
# 每条 = (正则, 风险说明)
DANGEROUS_PATTERNS = [
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/(?:\s|$|\*)", "递归删除根目录"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+~(?:\s|$)", "递归删除用户主目录"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*-{0,2}[a-zA-Z]*r[a-zA-Z]*\s+", "递归删除（rm -r / -rf）"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bdd\s+if=.*of=/dev/", "直接写裸设备"),
    (r"\bformat\s+[a-zA-Z]:", "格式化磁盘分区"),
    (r"\bdel\s+/[fFsSqQ].*[a-zA-Z]:\\\\?\*", "强制删除整个分区"),
    (r"\brd\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\\\?\s*$", "静默删除目录树"),
    (r"\bshutdown\b", "关机 / 重启"),
    (r"\breboot\b.*--force", "强制重启"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork 炸弹"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"\bbcdedit\b", "修改启动配置"),
    (r"\bvssadmin\s+delete", "删除卷影副本"),
    (r"\bcipher\s+/w", "擦除磁盘空闲空间"),
    (r"\breg\s+delete\b", "删除注册表项"),
    (r"\bnet\s+user\s+\S+\s+/delete", "删除用户账户"),
]


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"", re.S)

# 「把字符串当命令执行」的外壳：bash -c / sh -c / cmd /c / powershell -Command。
# 只有这种写法，引号里的内容才真的要当命令跑。
_EXEC_SHELL = re.compile(
    r"\b(?:bash|sh|zsh|cmd|powershell|pwsh)\b[^\n]{0,30}?(?:\s-[a-zA-Z]*c[a-zA-Z]*\b|\s/[ck]\b)",
    re.I,
)


def _strip_quoted(cmd: str) -> str:
    """把引号包裹的内容抹成空串。

    grep -n 'shutdown|rm -r' file   这种是「搜这些词」，不是「执行这些词」，
    不抹掉就天天误报。
    """
    return _QUOTED.sub("''", cmd)


def detect_danger(cmd: str) -> str | None:
    """返回命中的风险说明，没命中返回 None。

    判定分两步，避免把搜索词当成命令：
      1. 先只看引号外的部分（真正的命令和参数）。
      2. 只有当命令里带 bash -c / cmd /c / powershell -Command 这类
         「把字符串当命令跑」的外壳时，才连引号里的内容一起查，
         否则 bash -c "shutdown /s" 会被漏掉。
    """
    flat = " ".join(cmd.split())
    outside = _strip_quoted(flat)

    for pat, why in DANGEROUS_PATTERNS:
        if re.search(pat, outside, flags=re.I):
            return why

    if _EXEC_SHELL.search(outside):
        for pat, why in DANGEROUS_PATTERNS:
            if re.search(pat, flat, flags=re.I):
                return why

    return None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _truncate(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n...[输出过长，省略 {len(text) - limit} 字符]...\n\n{tail}"


# --------------------------------------------------------------------------
# bash
# --------------------------------------------------------------------------

def shell_description(cfg: dict) -> str:
    if cfg.get("agent_mode") == "screen":
        return "无（当前为纯截屏模式，只能用屏幕工具）"
    bash = config.find_bash(cfg)
    if bash:
        return f"Git Bash（{bash}），可以用 ls/grep/cat/python 等类 Unix 命令"
    return "cmd.exe（没有找到 Git Bash，命令请用 Windows CMD 语法）"


def _kill_tree(pid: int) -> None:
    """连子进程一起干掉。"""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def run_bash(cfg: dict, cmd: str, confirm=None, cancel=None,
             workdir: str | None = None, timeout: int | None = None) -> str:
    """执行命令。

    confirm: callable(cmd, reason) -> bool。命中高危规则时调用它问用户。
             传 None 表示没有交互环境，直接拒绝。
    cancel:  threading.Event。置位后立刻杀掉整棵进程树并返回。
    workdir: 指定工作目录（skill_run 用来把 cwd 钉在技能目录里）。
             不传就按 sandbox 走 workspace/ 或项目根。
    timeout: 覆盖 cfg 里的 bash_timeout。

    输出走临时文件而不是管道 —— 因为 GUI 程序（start xxx.exe）会继承管道
    句柄并且不退出，用 capture_output 会永远等不到 EOF，界面就卡在"执行中"。
    """
    if not cmd.strip():
        return "错误：命令为空。"

    sandbox = bool(cfg.get("sandbox", True))

    # 高危检测始终生效，不跟 sandbox 绑在一起。
    # sandbox 只管「工作目录锁在 workspace/」，关掉它不该顺手把确认也一起关掉 ——
    # 否则用户只是想放开目录限制，却等于同意了 rm -rf / 直接执行。
    why = detect_danger(cmd)
    if why:
        if confirm is None:
            return (
                f"【已拦截】这条命令命中了高危规则（{why}），"
                "当前没有交互环境可以确认，已拒绝执行。\n"
                "如果确实需要执行，请在有界面的模式下重新发一次。"
            )
        if not confirm(cmd, why):
            return (
                f"【用户拒绝】用户没有批准这条命令（{why}）。\n"
                "换个安全的方式重做，或者先问清楚用户到底想干什么。"
            )

    bash = config.find_bash(cfg)
    if workdir:
        cwd_path = Path(workdir)
        try:
            cwd_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            cwd_path = config.WORK_DIR
    else:
        cwd_path = config.WORK_DIR if sandbox else config.BASE_DIR
    cwd = str(cwd_path)
    cwd_path.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    env["PYTHONIOENCODING"] = "utf-8"

    if bash:
        env["PATH"] = f"{Path(bash).parent};{env.get('PATH', '')}"
        argv = [bash, "-c", cmd]
    else:
        argv = ["cmd.exe", "/c", cmd]

    shell_flag = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    timeout = int(timeout or cfg.get("bash_timeout", 120))

    tmp_dir = config.DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{int(time.time() * 1000) % 1000}"
    out_path = tmp_dir / f"bash_{stamp}.out"
    err_path = tmp_dir / f"bash_{stamp}.err"

    started = time.time()
    returncode = None
    timed_out = False
    cancelled = False

    try:
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=fo,
                stderr=fe,
                creationflags=shell_flag,
                start_new_session=(sys.platform != "win32"),
            )
            while True:
                rc = proc.poll()
                if rc is not None:
                    returncode = rc
                    break
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    _kill_tree(proc.pid)
                    returncode = proc.wait()
                    break
                if time.time() - started > timeout:
                    timed_out = True
                    _kill_tree(proc.pid)
                    returncode = proc.wait()
                    break
                time.sleep(0.05)
    except FileNotFoundError as e:
        return f"【执行失败】找不到 shell：{e}"
    except Exception as e:
        return f"【执行失败】{type(e).__name__}: {e}"

    try:
        out = _decode(out_path.read_bytes()).strip()
    except Exception:
        out = ""
    try:
        err = _decode(err_path.read_bytes()).strip()
    except Exception:
        err = ""

    for p in (out_path, err_path):
        try:
            p.unlink()
        except OSError:
            pass   # GUI 子进程还占着句柄，删不掉就算了

    elapsed = time.time() - started

    if cancelled:
        return (
            f"【已中断】用户点了停止，命令被杀掉了（跑了 {elapsed:.1f}s）。\n"
            "不要重试同一条命令，先问用户想怎么办。"
        )
    if timed_out:
        hint = ""
        if "start " in cmd or "&" in cmd:
            hint = "\n（如果这是在启动 GUI 程序，用 start 启动后应该立刻返回；" \
                   "卡住说明进程没有脱离终端，换 `cmd //c start \"\" xxx.exe` 的写法。）"
        return f"【超时】命令超过 {timeout}s 没有结束，已连同子进程一起终止。{hint}"

    parts = [f"$ {cmd}", f"[退出码 {returncode} · {elapsed:.1f}s · cwd={cwd}]"]
    if out:
        parts.append("--- stdout ---\n" + _truncate(out))
    if err:
        parts.append("--- stderr ---\n" + _truncate(err))
    if not out and not err:
        parts.append(
            "（没有任何输出。如果这条命令是启动 GUI 程序，程序已经在后台跑起来了，"
            "不要重复执行，用 wait 然后 screenshot 确认。）"
        )
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 屏幕
# --------------------------------------------------------------------------

def virtual_origin() -> tuple[int, int]:
    """虚拟桌面左上角在屏幕坐标里的位置（多显示器时可能是负数）。"""
    if sys.platform != "win32":
        return 0, 0
    try:
        import ctypes

        u = ctypes.windll.user32
        # SM_XVIRTUALSCREEN = 76, SM_YVIRTUALSCREEN = 77
        return int(u.GetSystemMetrics(76)), int(u.GetSystemMetrics(77))
    except Exception:
        return 0, 0


def screen_origin() -> tuple[int, int]:
    """上一次截图对应的屏幕偏移。"""
    return _SCREEN_ORIGIN


def grab_screen(scale: float = 1.0) -> tuple[Path, int, int]:
    """截整个虚拟桌面（包含所有显示器），默认不缩放。

    返回 (文件路径, 图片宽, 图片高)。
    图片左上角 = 虚拟桌面左上角，真实屏幕坐标 = 图片坐标 + screen_origin()。
    不缩放时图片像素和屏幕像素一一对应，AI 看到的坐标可以直接用。
    """
    from PIL import ImageGrab

    global _SCREEN_ORIGIN
    config.SHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = config.SHOT_DIR / f"shot_{stamp}.png"

    try:
        img = ImageGrab.grab(all_screens=True)
    except TypeError:          # 老版本 Pillow 没有 all_screens 参数
        img = ImageGrab.grab()

    _SCREEN_ORIGIN = virtual_origin()
    orig_w, orig_h = img.size

    if scale and scale < 1.0:
        img = img.resize((max(1, int(orig_w * scale)), max(1, int(orig_h * scale))))

    img.save(path, "PNG")

    # 桌面截图用 PNG 动辄好几 MB，base64 之后容易顶爆请求体积上限。
    # 转 JPEG 只改编码不改尺寸，坐标映射完全不受影响。
    try:
        if path.stat().st_size > 2_500_000:
            jpg = path.with_suffix(".jpg")
            img.convert("RGB").save(jpg, "JPEG", quality=82, optimize=True)
            path.unlink(missing_ok=True)
            path = jpg
    except OSError:
        pass

    return path, img.size[0], img.size[1]


VISION_PROMPT = """这是一张 Windows 屏幕截图，原始分辨率 {w}x{h} 像素（图像可能被等比缩小）。
请严格按下面的格式回答，不要加多余的话：

屏幕描述：
（用中文说明现在是什么界面、有哪些窗口、主要文字内容，3-8 行）

可交互元素：
元素名称 (x, y)
元素名称 (x, y)
...

坐标要求：
- 所有坐标必须是相对于原始 {w}x{h} 图像的像素坐标，原点在左上角。
- 图像里如果被缩小了，请按比例换算回原图坐标。
- 只列真正能点/能输入的元素：按钮、输入框、菜单项、图标、链接、标签页。
- 最多列 25 个，优先级从高到低。"""


def screenshot(cfg: dict) -> dict:
    """截屏。

    返回 {"text": 给模型看的文字, "image": 图片路径(可选)}

    vision_passthrough 开（默认）：把原图直接交给主模型，它自己看。
    这和"先让识图模型写段描述再转述"是完全不同的两件事——后者必然丢信息。
    """
    scale = float(cfg.get("screenshot_scale", 1.0) or 1.0)
    try:
        path, w, h = grab_screen(scale)
    except Exception as e:
        return {"text": f"【截屏失败】{type(e).__name__}: {e}"}

    ox, oy = _SCREEN_ORIGIN
    size_note = f"图片尺寸 {w}x{h} 像素"
    if ox or oy:
        size_note += f"，图片原点对应屏幕坐标 ({ox}, {oy})"

    if cfg.get("vision_passthrough", True):
        hint = (
            f"截图完成，{size_note}。完整画面在下面附带的图片里，你自己看。\n"
            "click / move 用的坐标就是你在这张图里量到的像素坐标，"
            "程序会自动换算到真实屏幕。不要自己乘缩放比例，图上多少就是多少。\n"
            "如果这一步只是想确认刚才的操作有没有生效，看完图片直接判断就行。"
        )
        if scale < 1.0:
            hint += f"\n注意：图片被等比缩放到 {scale:.2f} 倍，量坐标时以图片像素为准即可。"
        return {"text": hint, "image": str(path)}

    # ---- 兜底：主模型不能看图，才走识图模型转述 ----
    vis = cfg.get("vision_api") or {}
    if not vis.get("api_key"):
        return {
            "text": (
                f"截图已保存：{path}（{size_note}）\n"
                "但是没有配置识图 API，也关掉了图片直传，所以我看不到画面内容。"
                "告诉用户去设置里打开「截图直接给模型看」，或者填一个识图模型。"
            )
        }

    try:
        from . import llm

        desc = llm.ask_vision(vis, path, VISION_PROMPT.format(w=w, h=h))
    except Exception as e:
        return {"text": f"截屏已保存：{path}，但识图调用失败：{type(e).__name__}: {e}"}

    if not desc.strip():
        return {
            "text": (
                f"截屏已保存：{path}（{size_note}）\n"
                "识图模型返回了空内容（常见原因是推理过程把 max_tokens 用光了）。"
                "建议在设置里打开「截图直接给模型看」，跳过识图中转。"
            )
        }

    return {"text": f"[屏幕截图 {w}x{h} · 文件 {path}]\n\n{desc}\n\n（坐标直接用于 click 工具）"}


# --------------------------------------------------------------------------
# 区域截图
# --------------------------------------------------------------------------

def _grab_box(box: tuple[int, int, int, int]):
    """按屏幕坐标截一块，返回 PIL.Image。"""
    from PIL import ImageGrab

    try:
        full = ImageGrab.grab(all_screens=True)
    except TypeError:
        full = ImageGrab.grab()
    ox, oy = virtual_origin()
    x0, y0, x1, y1 = box
    return full.crop((x0 - ox, y0 - oy, x1 - ox, y1 - oy))


def screenshot_region(cfg: dict, x, y, w, h) -> dict:
    """截取指定屏幕区域，交给模型直看。

    区域坐标用真实屏幕坐标（左上角 x,y，宽高 w,h）。
    """
    try:
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (TypeError, ValueError):
        return {"text": "【区域错误】region 需要 4 个整数：[x, y, w, h]。"}

    if w <= 0 or h <= 0:
        return {"text": f"【区域错误】宽高必须为正数，收到 {w}x{h}。"}

    try:
        img = _grab_box((x, y, x + w, y + h))
    except Exception as e:
        return {"text": f"【区域截屏失败】{type(e).__name__}: {e}"}

    config.SHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = config.SHOT_DIR / f"region_{stamp}.png"
    img.save(path, "PNG")
    try:
        if path.stat().st_size > 2_500_000:
            jpg = path.with_suffix(".jpg")
            img.convert("RGB").save(jpg, "JPEG", quality=85, optimize=True)
            path.unlink(missing_ok=True)
            path = jpg
    except OSError:
        pass

    return {
        "text": (
            f"已截取屏幕区域：左上角 ({x}, {y})，尺寸 {w}x{h}。\n"
            f"这张图的 (0,0) 对应屏幕 ({x}, {y})。"
            f"要在图里点 (a, b)，click 请给绝对坐标 ({x}+a, {y}+b)。"
        ),
        "image": str(path),
    }


def copy_region(x, y, w, h) -> str:
    """截取屏幕区域并放进系统剪贴板，用户可以直接 Ctrl+V 粘到别处。"""
    try:
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (TypeError, ValueError):
        return "【区域错误】需要 4 个整数：[x, y, w, h]。"
    if w <= 0 or h <= 0:
        return f"【区域错误】宽高必须为正数，收到 {w}x{h}。"

    try:
        img = _grab_box((x, y, x + w, y + h))
    except Exception as e:
        return f"【截取失败】{type(e).__name__}: {e}"

    ok, msg = copy_image(img)
    if ok:
        return f"已把区域 ({x}, {y}, {w}x{h}) 复制到剪贴板，现在可以 Ctrl+V 粘贴。"
    return f"【复制失败】{msg}"


# --------------------------------------------------------------------------
# 剪贴板
# --------------------------------------------------------------------------

def clipboard_read() -> str:
    try:
        import pyperclip

        text = pyperclip.paste()
    except Exception as e:
        return f"【读取剪贴板失败】{type(e).__name__}: {e}"

    if not text:
        return "剪贴板是空的（或者里面不是文字）。"
    if len(text) > 4000:
        return f"剪贴板内容（{len(text)} 字，已截断前 4000 字）：\n{text[:4000]}"
    return f"剪贴板内容：\n{text}"


def clipboard_write(text: str) -> str:
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception as e:
        return f"【写入剪贴板失败】{type(e).__name__}: {e}"
    preview = text if len(text) <= 60 else text[:60] + "…"
    return f"已复制到剪贴板（{len(text)} 字）：{preview}"


def copy_image(img) -> tuple[bool, str]:
    """把 PIL 图片放进 Windows 剪贴板（CF_DIB）。"""
    if sys.platform != "win32":
        return False, "只有 Windows 支持把图片放进剪贴板。"

    import ctypes
    import io
    from ctypes import wintypes

    rgb = img.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, "BMP")
    data = buf.getvalue()[14:]      # 去掉 BMP 文件头，留 BITMAPINFOHEADER

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    GMEM_MOVEABLE = 0x0002
    CF_DIB = 8

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False, "GlobalAlloc 失败"
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False, "GlobalLock 失败"
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)

    for _ in range(6):              # 剪贴板被别的程序占着时重试
        if user32.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        kernel32.GlobalFree(handle)
        return False, "剪贴板被其他程序占用"

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, handle):
            kernel32.GlobalFree(handle)
            return False, "SetClipboardData 失败"
    finally:
        user32.CloseClipboard()
    return True, ""


def _pyautogui():
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.12
    return pyautogui


def click(x, y, button: str = "left") -> str:
    try:
        gui = _pyautogui()
        ix, iy = int(x), int(y)          # 模型给的：截图坐标
        ox, oy = _SCREEN_ORIGIN
        x, y = ix + ox, iy + oy          # 真实屏幕坐标
        if button == "double":
            gui.doubleClick(x, y)
            return f"已在 ({ix}, {iy}) 双击。"
        if button == "right":
            gui.rightClick(x, y)
            return f"已在 ({ix}, {iy}) 右键点击。"
        gui.click(x, y)
        return f"已在 ({ix}, {iy}) 左键点击。"
    except Exception as e:
        return f"【点击失败】{type(e).__name__}: {e}"


def move(x, y) -> str:
    try:
        gui = _pyautogui()
        ix, iy = int(x), int(y)
        ox, oy = _SCREEN_ORIGIN
        gui.moveTo(ix + ox, iy + oy, duration=0.2)
        return f"鼠标已移动到 ({ix}, {iy})。"
    except Exception as e:
        return f"【移动失败】{type(e).__name__}: {e}"


def type_text(text: str) -> str:
    """中文走剪贴板，ASCII 直接打。"""
    try:
        gui = _pyautogui()
    except Exception as e:
        return f"【输入失败】{type(e).__name__}: {e}"

    if not text:
        return "没有要输入的文字。"

    if text.isascii():
        try:
            gui.write(text, interval=0.02)
            return f"已输入 {len(text)} 个字符。"
        except Exception as e:
            return f"【输入失败】{type(e).__name__}: {e}"

    try:
        import pyperclip

        pyperclip.copy(text)
        time.sleep(0.1)
        gui.hotkey("ctrl", "v")
        return f"已通过剪贴板粘贴 {len(text)} 个字符。"
    except Exception as e:
        return f"【输入失败】{type(e).__name__}: {e}"


def press_key(keys: str) -> str:
    try:
        gui = _pyautogui()
        combo = [k.strip().lower() for k in str(keys).replace(" ", "").split("+") if k.strip()]
        if not combo:
            return "按键为空。"
        if len(combo) == 1:
            gui.press(combo[0])
        else:
            gui.hotkey(*combo)
        return f"已按下 {keys}。"
    except Exception as e:
        return f"【按键失败】{type(e).__name__}: {e}"


def scroll(amount) -> str:
    try:
        gui = _pyautogui()
        n = int(amount)
        gui.scroll(n)
        return f"已滚动 {n} 格。"
    except Exception as e:
        return f"【滚动失败】{type(e).__name__}: {e}"


def wait(seconds) -> str:
    try:
        s = max(0.0, min(float(seconds), 30.0))
    except (TypeError, ValueError):
        s = 1.0
    time.sleep(s)
    return f"已等待 {s} 秒。"
