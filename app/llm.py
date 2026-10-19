# -*- coding: utf-8 -*-
"""OpenAI 兼容接口客户端（自写 SSE 流式，不依赖 openai 库）。

只要求 base_url 指向 /v1 这种根，或者直接给完整的 /chat/completions。
"""
from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Generator

import requests


class LLMError(Exception):
    pass


def endpoint(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    if not b:
        b = "https://api.openai.com/v1"
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {(api_key or '').strip()}",
        "Content-Type": "application/json",
    }


def _raise_friendly(resp: requests.Response) -> None:
    if resp.status_code < 400:
        return
    body = resp.text[:600]
    msg = f"HTTP {resp.status_code}: {body}"
    if resp.status_code == 401:
        msg = "API Key 无效或过期（401）。检查设置里的 Key。\n" + body
    elif resp.status_code == 404:
        msg = "接口地址或模型名不对（404）。base_url 一般要带 /v1，模型名要完全一致。\n" + body
    elif resp.status_code == 429:
        msg = "请求太频繁或额度用完（429）。\n" + body
    raise LLMError(msg)


def stream_chat(
    api: dict,
    messages: list[dict],
    temperature: float = 0.7,
    cancel: threading.Event | None = None,
    timeout: int = 300,
    on_reasoning=None,
) -> Generator[str, None, None]:
    """流式产出增量文本。"""
    base_url = api.get("base_url", "")
    api_key = api.get("api_key", "")
    model = api.get("model", "")
    if not api_key:
        raise LLMError("没有配置 API Key，去设置里填一个。")
    if not model:
        raise LLMError("没有配置模型名。")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    url = endpoint(base_url)

    try:
        resp = requests.post(
            url,
            headers=_headers(api_key),
            json=payload,
            stream=True,
            timeout=(20, timeout),
        )
    except requests.RequestException as e:
        raise LLMError(f"连不上接口：{e}") from e

    with resp:
        _raise_friendly(resp)
        for raw in resp.iter_lines(decode_unicode=False):
            if cancel is not None and cancel.is_set():
                return
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                if line == "[DONE]":
                    return
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if isinstance(obj, dict) and obj.get("error"):
                err = obj["error"]
                raise LLMError(
                    err.get("message") if isinstance(err, dict) else str(err)
                )

            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece
            elif on_reasoning is not None and delta.get("reasoning_content"):
                # 推理型模型（deepseek-flash 等）先吐思考内容，正文还没开始
                on_reasoning(delta["reasoning_content"])


def chat_once(api: dict, messages: list[dict], temperature: float = 0.3,
              max_tokens: int = 2048, timeout: int = 180) -> str:
    """非流式，一次性拿完整回复。用于记忆总结、识图。"""
    if not api.get("api_key"):
        raise LLMError("这条 API 没配 Key。")
    payload = {
        "model": api.get("model", ""),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(
            endpoint(api.get("base_url", "")),
            headers=_headers(api.get("api_key", "")),
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise LLMError(f"连不上接口：{e}") from e

    _raise_friendly(resp)
    data = resp.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"返回格式不认识：{str(data)[:400]}")


def data_uri(image_path: str | Path) -> str:
    """把本地图片读成 data: URI，可直接放进 image_url。"""
    p = Path(image_path)
    mime = "image/png"
    suffix = p.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif suffix == ".webp":
        mime = "image/webp"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def ask_vision(api: dict, image_path: str | Path, prompt: str,
               timeout: int = 180) -> str:
    """把本地图片发给视觉模型，返回文字。"""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri(image_path)}},
            ],
        }
    ]
    return chat_once(api, messages, temperature=0.1, max_tokens=3200, timeout=timeout)
