# -*- coding: utf-8 -*-
"""用户发进来的图片：校验、缩放、落盘、转 data URI。

为什么要另存一份而不是直接用原路径：
  1. 原图可能在桌面/微信临时目录，随时被删，对话重开就变成坏链接；
  2. 手机直出照片 4000px、8MB，一次请求就把上下文顶爆；
  3. 格式五花八门（bmp/tiff/heic），统一成 png/jpeg 各家模型都吃。

落盘位置：data/images/<对话 id>/<时间戳>_<4位随机>.(png|jpg)
对话里只存相对 BASE_DIR 的路径，整个项目文件夹可以随便搬。
"""
from __future__ import annotations

import base64
import hashlib
import io
import secrets
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import config

MAX_SIDE = 2048                      # 长边上限，再大就等比缩
MAX_SRC_BYTES = 24 * 1024 * 1024     # 单张原图 24MB 上限
JPEG_QUALITY = 88
SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


class ImageError(Exception):
    """给用户看的错误，消息本身就是人话。"""


# ----------------------------------------------------------------------
# 落盘
# ----------------------------------------------------------------------
def _conv_dir(conv_id: str) -> Path:
    d = config.IMAGE_DIR / (conv_id or "_")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize(im: Image.Image) -> tuple[Image.Image, str]:
    """转正方向、缩到上限、决定存成什么格式。返回 (图, 扩展名)。"""
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass                           # EXIF 坏了不影响看图

    w, h = im.size
    if max(w, h) > MAX_SIDE:
        k = MAX_SIDE / float(max(w, h))
        im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)

    has_alpha = im.mode in ("RGBA", "LA") or (
        im.mode == "P" and "transparency" in im.info
    )
    if has_alpha:
        return im.convert("RGBA"), ".png"
    return im.convert("RGB"), ".jpg"


def save(source, conv_id: str) -> str:
    """把一张图存进 data/images/<conv_id>/。

    source 可以是路径、bytes 或 PIL.Image。返回值是相对 BASE_DIR 的路径字符串，
    直接往 messages 里存这个就行。
    """
    config.ensure_dirs()

    try:
        if isinstance(source, Image.Image):
            im = source
        elif isinstance(source, (bytes, bytearray)):
            if len(source) > MAX_SRC_BYTES:
                raise ImageError(
                    f"图片太大（{len(source) / 1048576:.1f}MB），"
                    f"上限 {MAX_SRC_BYTES // 1048576}MB"
                )
            im = Image.open(io.BytesIO(bytes(source)))
        else:
            p = Path(source).expanduser()
            if not p.is_file():
                raise ImageError(f"找不到这个文件：{p}")
            if p.suffix.lower() not in SUPPORTED:
                raise ImageError(f"不支持的格式：{p.suffix or '无扩展名'}")
            size = p.stat().st_size
            if size > MAX_SRC_BYTES:
                raise ImageError(
                    f"图片太大（{size / 1048576:.1f}MB），"
                    f"上限 {MAX_SRC_BYTES // 1048576}MB"
                )
            im = Image.open(p)
    except ImageError:
        raise
    except Exception as e:
        raise ImageError(f"读不出这张图：{e}") from e

    try:
        im.load()
    except Exception as e:
        raise ImageError(f"图片内容损坏：{e}") from e

    im2, ext = _normalize(im)

    folder = _conv_dir(conv_id)
    name = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}{ext}"
    dest = folder / name
    try:
        if ext == ".png":
            im2.save(dest, "PNG", optimize=True)
        else:
            im2.save(dest, "JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as e:
        raise ImageError(f"保存失败：{e}") from e
    finally:
        try:
            im.close()
        except Exception:
            pass

    try:
        return str(dest.relative_to(config.BASE_DIR))
    except ValueError:
        return str(dest)


def from_clipboard(conv_id: str) -> str | None:
    """剪贴板里有图就存下来返回路径；是文字/空就返回 None。"""
    try:
        from PIL import ImageGrab

        data = ImageGrab.grabclipboard()
    except Exception:
        return None

    if isinstance(data, Image.Image):
        return save(data, conv_id)
    if isinstance(data, list):
        # 从资源管理器复制文件时，grabclipboard 返回路径列表
        for item in data:
            p = Path(str(item))
            if p.suffix.lower() in SUPPORTED and p.is_file():
                return save(p, conv_id)
    return None


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------
def resolve(rel: str) -> Path | None:
    """把对话里存的相对路径还原成绝对路径；文件没了返回 None。"""
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = config.BASE_DIR / p
    return p if p.is_file() else None


def to_data_uri(rel: str) -> str:
    p = resolve(rel)
    if not p:
        raise ImageError("图片文件已不存在")
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def open_for_ui(rel: str, max_side: int = 320) -> Image.Image | None:
    """给界面画缩略图用。读不出来返回 None，界面上显示成占位块。"""
    p = resolve(rel)
    if not p:
        return None
    try:
        im = Image.open(p)
        im.load()
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        return im
    except Exception:
        return None


def api_parts(rel: str, note: str | None = None) -> list[dict] | None:
    """转成 OpenAI 的 content 数组片段。失败返回 None。"""
    try:
        uri = to_data_uri(rel)
    except Exception:
        return None
    parts: list[dict] = []
    if note is not None:
        parts.append({"type": "text", "text": note})
    parts.append({"type": "image_url", "image_url": {"url": uri}})
    return parts


def drop_conv(conv_id: str) -> None:
    """对话被删时，把它的图片一起挪进 trash，别留孤儿文件。"""
    src = config.IMAGE_DIR / (conv_id or "_")
    if not src.is_dir():
        return
    try:
        dest = config.CONV_TRASH_DIR / f"images_{conv_id}_{int(time.time())}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
    except OSError:
        pass


def digest(rel: str) -> str:
    """短指纹，前端拿去当 img 的 key 用，避免重复渲染。"""
    p = resolve(rel)
    if not p:
        return "missing"
    try:
        return hashlib.md5(p.read_bytes()[:65536]).hexdigest()[:10]
    except OSError:
        return "err"
