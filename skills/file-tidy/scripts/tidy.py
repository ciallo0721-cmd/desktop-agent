#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按扩展名整理一个目录。默认只演习，加 --apply 才真挪。

    python scripts/tidy.py "C:/Users/me/Downloads"
    python scripts/tidy.py "C:/Users/me/Downloads" --apply

原则：
  - 只处理目标目录**第一层**的文件，不递归，不进子文件夹。
  - 隐藏文件、desktop.ini、Thumbs.db、.lnk 快捷方式一概跳过。
  - 目标子文件夹里已有同名文件时，自动改名成 name_1.ext，绝不覆盖。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# 分类表；想调粗细改这里。键是子文件夹名，值是扩展名集合（小写，带点）。
CATEGORY: dict[str, set[str]] = {
    "图片": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
             ".heic", ".raw", ".psd"},
    "文档": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
             ".txt", ".md", ".csv", ".epub", ".rtf", ".odt"},
    "视频": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
    "音频": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".ape"},
    "压缩包": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "安装包": {".exe", ".msi", ".apk", ".dmg", ".pkg", ".deb", ".rpm"},
    "代码": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go",
             ".rs", ".sh", ".bat", ".ps1", ".json", ".yaml", ".yml", ".toml",
             ".html", ".css", ".xml", ".sql"},
}

SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
SKIP_SUFFIX = {".lnk", ".tmp", ".part", ".crdownload"}
EXT_INDEX = {ext: cat for cat, exts in CATEGORY.items() for ext in exts}


def safe_target(folder: Path, name: str) -> Path:
    """目标重名就加 _1 _2，不覆盖。"""
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(1, 1000):
        cand = folder / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"重名太多：{name}")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    apply = "--apply" in argv[1:]
    if not args:
        print(__doc__.strip())
        return 2

    root = Path(args[0]).expanduser()
    if not root.is_dir():
        print(f"不是目录：{root}")
        return 2

    plan: list[tuple[Path, Path, str]] = []
    skipped = 0

    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name.lower() in SKIP_NAMES:
            skipped += 1
            continue
        if p.suffix.lower() in SKIP_SUFFIX or not p.suffix:
            skipped += 1
            continue
        cat = EXT_INDEX.get(p.suffix.lower(), "其他")
        plan.append((p, root / cat, cat))

    if not plan:
        print(f"{root}\n没有需要整理的文件（跳过 {skipped} 个）。")
        return 0

    if not apply:
        for src, folder, cat in plan:
            print(f"[计划] {src.name} → {cat}/{safe_target(folder, src.name).name}")
        print(f"\n演习模式，没有动文件。共 {len(plan)} 个待整理，跳过 {skipped} 个。")
        print("确认无误后加 --apply 真挪。")
        return 0

    moved = 0
    for src, folder, _cat in plan:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            dest = safe_target(folder, src.name)
            shutil.move(str(src), str(dest))
            moved += 1
            print(f"[完成] {src.name} → {dest.parent.name}/{dest.name}")
        except OSError as e:
            print(f"[失败] {src.name}：{type(e).__name__}: {e}")

    print(f"\n挪了 {moved} 个，跳过 {skipped} 个。目录：{root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
