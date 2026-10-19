# -*- coding: utf-8 -*-
"""联网搜索 + 网页抓取。

不依赖任何搜索 API Key 也能用：直接抓 Bing / 360 的结果页解析。
如果用户在设置里填了 Tavily / Serper 的 Key，就优先走 API（结果更干净）。

设计原则：
  - 纯 requests + 正则，不引入 bs4 / lxml（少装两个包，venv 更快）
  - 任何一路失败都继续试下一路，全挂了才报错
  - 抓回来的正文一律压成纯文本，去掉 script/style/nav，防止把几万字塞进上下文
"""
from __future__ import annotations

import html as html_lib
import json
import re
from typing import Iterable

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

MAX_PAGE_CHARS = 12000     # 单页正文最多回传这么多字
REQUEST_TIMEOUT = 20


# --------------------------------------------------------------------------
# HTML → 纯文本
# --------------------------------------------------------------------------

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|iframe|head|nav|footer|form)\b[^>]*>.*?</\1\s*>",
    re.S | re.I,
)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK_TAG = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|header|table|ul|ol|pre|blockquote)\b[^>]*>",
    re.I,
)
_ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    """粗暴但够用的 HTML → 文本。"""
    if not raw:
        return ""
    s = _COMMENT.sub(" ", raw)
    s = _DROP_BLOCKS.sub(" ", s)
    s = _BLOCK_TAG.sub("\n", s)
    s = _ANY_TAG.sub("", s)   # 行内标签不能补空格，否则 "Python.org" 会变成 "Python .org"
    s = html_lib.unescape(s)
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    lines = []
    for line in s.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line:
            lines.append(line)
    # 连续重复行去掉（导航栏常刷屏）
    out: list[str] = []
    for line in lines:
        if out and out[-1] == line:
            continue
        out.append(line)
    return "\n".join(out)


def _clip(text: str, limit: int = MAX_PAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（正文还有 {len(text) - limit} 字未显示）"


# --------------------------------------------------------------------------
# 搜索后端
# --------------------------------------------------------------------------

def _get(url: str, params: dict | None = None, timeout: int = REQUEST_TIMEOUT):
    import requests

    return requests.get(url, params=params, headers=HEADERS, timeout=timeout)


def _search_bing(query: str, count: int) -> list[dict]:
    r = _get("https://cn.bing.com/search",
             {"q": query, "setlang": "zh-CN", "count": str(max(count, 10))})
    if r.status_code != 200:
        return []
    return _parse_bing(r.text, count)


def _parse_bing(page: str, count: int) -> list[dict]:
    out: list[dict] = []
    chunks = page.split('<li class="b_algo"')
    for chunk in chunks[1:]:
        head = re.search(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                         chunk, re.S | re.I)
        if not head:
            continue
        url = html_lib.unescape(head.group(1))
        title = html_to_text(head.group(2)).replace("\n", " ").strip()
        snip = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S | re.I)
        desc = html_to_text(snip.group(1)).replace("\n", " ").strip() if snip else ""
        if not desc:
            # 有些结果用 <div class="b_caption"><p>
            snip = re.search(r'class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</', chunk, re.S | re.I)
            desc = html_to_text(snip.group(1)).replace("\n", " ").strip() if snip else ""
        if not title or url.startswith("javascript:"):
            continue
        out.append({"title": title, "url": url, "snippet": desc[:400]})
        if len(out) >= count:
            break
    return out


def _search_360(query: str, count: int) -> list[dict]:
    """360 搜索。结果比 cn.bing.com 的无 Cookie 版本准得多，所以放第一位。

    跳转链接里带 data-mdurl，那就是真实地址，优先用它。
    """
    r = _get("https://www.so.com/s", {"q": query})
    if r.status_code != 200:
        return []
    page = r.text
    out: list[dict] = []
    pattern = re.compile(r'<h3[^>]*class="res-title[^"]*"[^>]*>(.*?)</h3>', re.S | re.I)
    for m in pattern.finditer(page):
        block = m.group(1)
        link = re.search(r'<a[^>]+href="([^"]+)"', block, re.I)
        if not link:
            continue
        raw = html_lib.unescape(link.group(1))
        real = re.search(r'data-mdurl="([^"]+)"', block, re.I)
        url = html_lib.unescape(real.group(1)) if real else raw
        title = html_to_text(block).replace("\n", " ").strip()
        if not title:
            continue
        # 摘要紧跟在 h3 后面，取最近的 res-desc
        tail = page[m.end(): m.end() + 4000]
        snip = re.search(r'<p[^>]*class="res-desc"[^>]*>(.*?)</p>', tail, re.S | re.I)
        desc = html_to_text(snip.group(1)).replace("\n", " ").strip() if snip else ""
        if "短视频大全" in title or "ai.so.com" in url:
            continue   # 这两类是聚合页/站内 AI 页，没信息量
        out.append({"title": title, "url": url, "snippet": desc[:400]})
        if len(out) >= count:
            break
    return out


def _search_tavily(api_key: str, query: str, count: int) -> list[dict]:
    import requests

    r = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": count,
            "search_depth": "basic",
            "include_answer": False,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return [
        {
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": (it.get("content") or "")[:400],
        }
        for it in data.get("results", [])[:count]
    ]


def _search_serper(api_key: str, query: str, count: int) -> list[dict]:
    import requests

    r = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        data=json.dumps({"q": query, "num": count}),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return [
        {
            "title": it.get("title", ""),
            "url": it.get("link", ""),
            "snippet": (it.get("snippet") or "")[:400],
        }
        for it in data.get("organic", [])[:count]
    ]


def search(cfg: dict, query: str, count: int = 8) -> tuple[list[dict], str]:
    """返回 (结果列表, 说明)。说明用于告诉 AI 这次走的哪条路。"""
    query = (query or "").strip()
    if not query:
        return [], "查询词是空的"
    count = max(1, min(int(count or 8), 15))

    api = cfg.get("search_api") or {}
    provider = (api.get("provider") or "").strip().lower()
    key = (api.get("api_key") or "").strip()

    note = ""
    if key and provider in ("tavily", "serper"):
        try:
            if provider == "tavily":
                return _search_tavily(key, query, count), "tavily"
            return _search_serper(key, query, count), "serper"
        except Exception as e:  # API 挂了就降级到免费源
            note = f"{provider} 调用失败（{type(e).__name__}: {e}），已降级到免费搜索。\n"

    for name, fn in (("360", _search_360), ("bing", _search_bing)):
        try:
            hits = fn(query, count)
        except Exception:
            hits = []
        if hits:
            return hits, (note + f"{name}（免费源）").strip()
    return [], (note + "所有搜索源都没返回结果").strip()


def format_results(query: str, hits: Iterable[dict], source: str) -> str:
    hits = list(hits)
    if not hits:
        return (
            f"【搜索无结果】查询词：{query}\n"
            f"来源：{source}\n"
            "换个更短更具体的关键词重试，或者直接用 web_fetch 打开你已知的网址。"
        )
    lines = [f"【搜索结果】{query}  （来源：{source}）", ""]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. {h['title']}")
        lines.append(f"   {h['url']}")
        if h.get("snippet"):
            lines.append(f"   {h['snippet']}")
        lines.append("")
    lines.append("提示：摘要往往不完整，需要原文细节就用 web_fetch 打开上面某个链接。")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# 抓网页
# --------------------------------------------------------------------------

def _main_region(page: str) -> str:
    """尽量把正文容器切出来，避免导航栏/侧边栏污染开头。

    找不到就原样返回，反正后面还有 html_to_text 兜着。
    """
    best = ""
    for pat in (
        r"<article\b[^>]*>.*?</article>",
        r"<main\b[^>]*>.*?</main>",
        r'<div[^>]+(?:id|class)="[^"]*(?:article|content|post|entry|markdown|main)[^"]*"[^>]*>.*?</div>',
    ):
        for m in re.finditer(pat, page, re.S | re.I):
            if len(m.group(0)) > len(best):
                best = m.group(0)
    if len(best) > 800:
        return best
    return page


def fetch(url: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    url = (url or "").strip()
    if not url:
        return "【抓取失败】URL 是空的。"
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    try:
        r = _get(url, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        return f"【抓取失败】{url}\n{type(e).__name__}: {e}"

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "text" not in ctype and "json" not in ctype:
        return (
            f"【不是网页】{url}\nContent-Type: {ctype or '未知'}\n"
            "这个链接是图片/文件/二进制，web_fetch 读不了。"
        )

    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"

    text = html_to_text(_main_region(r.text))
    if not text.strip():
        return f"【抓取成功但没有正文】{url}\n（可能是纯 JS 渲染页面，试试搜索摘要或换个来源）"

    title_m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    title = html_to_text(title_m.group(1)).strip() if title_m else ""

    head = f"【网页正文】{url}"
    if title:
        head += f"\n标题：{title}"
    head += f"\nHTTP {r.status_code} · 正文 {len(text)} 字"
    return head + "\n\n" + _clip(text, max_chars)
