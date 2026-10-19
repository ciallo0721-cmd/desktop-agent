# -*- coding: utf-8 -*-
"""永久记忆。

记忆本体就是 prompt/memory.md 这个文件本身 —— 用户随时可以手改。
每轮对话结束后（或每 N 轮），把对话丢给记忆模型总结，然后重写这个文件。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import config, llm, prompts

MEMORY_HEADER = """# 长期记忆

> 这个文件由 agent 自动维护，也可以手动编辑。
> 每次对话结束会把新信息合并进来。

"""
MAX_MEMORY_CHARS = 6000

_lock = threading.Lock()


def memory_path(cfg: dict) -> Path:
    return config.resolve_prompt_path(cfg, "memory")


def read_memory(cfg: dict) -> str:
    p = memory_path(cfg)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(MEMORY_HEADER, encoding="utf-8")
        return ""
    text = p.read_text(encoding="utf-8")
    # 去掉我们自己的文件头，喂给模型时只要正文
    lines = [
        ln for ln in text.splitlines()
        if not ln.startswith("#") and not ln.startswith(">") and ln.strip()
    ]
    return "\n".join(lines).strip()


def write_memory(cfg: dict, body: str) -> None:
    p = memory_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(MEMORY_HEADER + body.strip() + "\n", encoding="utf-8")


SUMMARY_PROMPT = """你在维护一份关于用户的长期记忆档案。

【已有记忆】
{old}

【本次对话】
{transcript}

请输出合并后的完整长期记忆。要求：
1. 保留旧记忆里仍然有效的内容，去掉和新信息冲突、已经过时的部分。
2. 提取本次对话里值得长期记住的信息：用户是谁、在做什么项目、偏好、习惯、踩过的坑、结论、待办。
3. 用简洁的中文条目，每条一行，以 "- " 开头。
4. 不要记录一次性的临时内容（比如"刚才查了个天气"）。
5. 不要写套话、不要写"用户问了一个问题"这种描述。
6. 总长度不超过 {limit} 字。
7. 只输出记忆正文，不要任何解释或标题。"""


def _format_transcript(messages: list[dict], limit: int = 40) -> str:
    rows = []
    for m in messages[-limit:]:
        role = m.get("role", "user")
        content = m.get("content") or ""
        if role == "system":
            continue
        tag = "用户" if role == "user" else "AI"
        if m.get("tool_result"):
            tag = "工具"
        rows.append(f"{tag}: {content[:1200]}")
    return "\n\n".join(rows)


def summarize(cfg: dict, messages: list[dict]) -> str:
    """把对话合并进记忆，返回最新记忆文本。失败时抛异常。"""
    if not messages:
        return read_memory(cfg)

    api = config.api_of(cfg, "memory_api")
    if not api.get("api_key"):
        raise llm.LLMError("记忆 API 没配置")

    old = read_memory(cfg) or "（空）"
    prompt = SUMMARY_PROMPT.format(
        old=old,
        transcript=_format_transcript(messages),
        limit=MAX_MEMORY_CHARS,
    )

    with _lock:
        new_memory = llm.chat_once(
            api,
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=3000,
        )
        if not new_memory.strip():
            raise llm.LLMError("记忆模型返回空内容")
        new_memory = new_memory.strip()[:MAX_MEMORY_CHARS]
        write_memory(cfg, new_memory)
        return new_memory


def append_note(cfg: dict, note: str) -> None:
    """手动追加一条记忆。"""
    old = read_memory(cfg)
    stamp = time.strftime("%Y-%m-%d")
    write_memory(cfg, old + f"\n- [{stamp}] {note.strip()}")
