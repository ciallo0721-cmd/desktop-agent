# -*- coding: utf-8 -*-
"""技能市场：本地注册表 + 一键安装 + 来源签名校验。

注册表在 data/market.json（自己往里加条目）：

[
  {
    "id": "pdf-toolkit",
    "name": "PDF 工具箱",
    "description": "拆分、合并、抽文字",
    "source": "https://example.com/skills/pdf-toolkit.zip",
    "sha256": "SKILL.md 的 sha256（hex，可选）",
    "installed": false          // 安装器自动维护，手写时不用填
  }
]

source 支持三种写法：
  1. 直接指向 SKILL.md 的 URL（raw 链接）→ 装成单文件技能
  2. .zip 的 URL → 下载解压到 skills/<id>/
  3. git 仓库 URL → git clone --depth 1 到 skills/<id>/

签名校验：注册表里写了 sha256 的话，安装后会重算 SKILL.md 的哈希对比，
对不上就拒装 —— 防的就是 OpenClaw 那种「注册表被改了内容」的供应链投毒。
没写 sha256 的条目装的时候会提示「未签名」。

安全底线：
  - 只从 market.json 里列出来的 source 下载，模型报的 URL 一律不认。
  - zip 解压做路径穿越检查（.. / 绝对路径 / 盘符 全部拒）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import requests

from . import config

MARKET_PATH = config.DATA_DIR / "market.json"


def load_market() -> list[dict]:
    try:
        raw = json.loads(MARKET_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _save_market(items: list[dict]) -> None:
    MARKET_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_git_url(url: str) -> bool:
    return url.endswith(".git") or url.startswith("git@")


def _fetch_skillmd(url: str, dest: Path) -> None:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    dest.write_bytes(r.content)


def _fetch_zip(url: str, dest_dir: Path) -> None:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    tmp_zip = config.DATA_DIR / "tmp_market.zip"
    tmp_zip.write_bytes(r.content)
    try:
        with zipfile.ZipFile(tmp_zip) as z:
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/") or ":" in name:
                    raise ValueError(f"压缩包里有可疑路径：{info.filename}，拒绝解压")
            z.extractall(dest_dir)
    finally:
        tmp_zip.unlink(missing_ok=True)

    # 有些 zip 外面套了一层同名目录，把内容提到 skills/<id>/ 根
    entries = [p for p in dest_dir.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in list(inner.iterdir()):
            shutil.move(str(child), str(dest_dir / child.name))
        inner.rmdir()


def _fetch_git(url: str, dest_dir: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest_dir)],
        capture_output=True, timeout=300, check=True,
    )


def install_skill(entry_id: str) -> str:
    """按注册表条目安装一个技能。返回给 agent / 前端看的结果文本。"""
    items = load_market()
    entry = next((x for x in items if x.get("id") == entry_id), None)
    if not entry:
        return (f"【市场里没有这个技能】{entry_id!r}。"
                "market 工具（不带 id）可以列出注册表里有哪些。")
    source = str(entry.get("source") or "").strip()
    if not source:
        return f"【无法安装】{entry_id} 的注册表条目没写 source。"

    sid = entry_id
    dest = config.SKILL_DIR / sid
    if dest.exists():
        return f"【已存在】skills/{sid} 已经装过了。要更新请先删掉旧目录。"

    try:
        if _is_git_url(source):
            _fetch_git(source, dest)
        elif source.lower().endswith(".zip"):
            dest.mkdir(parents=True, exist_ok=True)
            _fetch_zip(source, dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            _fetch_skillmd(source, dest / "SKILL.md")
    except subprocess.CalledProcessError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return f"【克隆失败】git clone 报错：{e.stderr[:300] if e.stderr else e}"
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return f"【安装失败】{type(e).__name__}: {e}"

    skill_file = dest / "SKILL.md"
    if not skill_file.exists():
        shutil.rmtree(dest, ignore_errors=True)
        return f"【装了个空壳】skills/{sid} 里没有 SKILL.md，已回滚。"

    # 签名校验
    want = str(entry.get("sha256") or "").strip().lower()
    if want:
        got = _sha256_of(skill_file)
        if got != want:
            shutil.rmtree(dest, ignore_errors=True)
            return (
                "【签名不符，已拒装并回滚】\n"
                f"注册表 sha256 = {want}\n"
                f"实际下载的     = {got}\n"
                "内容和注册表登记的不一致，很可能是被篡改了。"
                "除非你亲自核实过来源，否则不要改注册表强行装。"
            )
        sign_note = "签名校验通过。"
    else:
        sign_note = "⚠ 这条没有登记 sha256，未经签名——内容请自己过目一遍再用。"

    entry["installed"] = True
    _save_market(items)
    return f"✅ 已安装技能 {sid} 到 skills/{sid}/。{sign_note}"


def market_text() -> str:
    items = load_market()
    if not items:
        return ("【技能市场】注册表 data/market.json 还是空的。"
                "往里加条目（id/name/source/sha256）就能一键安装。")
    rows = []
    for x in items:
        state = "已安装" if x.get("installed") else "未安装"
        sign = "已签名" if x.get("sha256") else "未签名"
        rows.append(f"- {x.get('id')} · {x.get('name', '')} · {state} · {sign}\n"
                    f"  {(x.get('description') or '')[:80]}\n"
                    f"  source: {x.get('source', '')}")
    return ("【技能市场】\n"
            "下面是注册表里的技能。用户同意后用 {\"tool\": \"market\", \"op\": \"install\", "
            "\"id\": \"...\"} 安装；来源不在注册表里的一律不装。\n" + "\n".join(rows))


def ensure_scaffold() -> None:
    if not MARKET_PATH.exists():
        _save_market([{
            "id": "example-skill",
            "name": "示例条目（改掉或删掉）",
            "description": "把 source 换成真实的 SKILL.md raw 链接 / .zip / git 仓库 URL",
            "source": "",
            "sha256": "",
        }])
