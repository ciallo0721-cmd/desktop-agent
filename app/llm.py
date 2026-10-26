# -*- coding: utf-8 -*-
"""OpenAI 兼容接口客户端（自写 SSE 流式，不依赖 openai 库）。

只要求 base_url 指向 /v1 这种根，或者直接给完整的 /chat/completions。
"""
from __future__ import annotations

import base64
import json
import threading
import time
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


# 这些字段是协议核心，extra_params 里出现也一律忽略，不然整个请求就废了
RESERVED_PARAMS = {"model", "messages", "stream", "tools"}


def apply_extra(payload: dict, extra: dict | None,
                block: set[str] | None = None) -> dict:
    """把用户的自定义请求参数合并进 payload。

    值是 None 的键直接跳过（前端清空输入框时常见）。
    block 用来在特定调用里额外禁用某些键（比如 chat_once 自己管 max_tokens）。
    """
    if not extra or not isinstance(extra, dict):
        return payload
    banned = RESERVED_PARAMS | (block or set())
    for k, v in extra.items():
        if not isinstance(k, str) or not k.strip() or v is None:
            continue
        if k in banned:
            continue
        payload[k] = v
    return payload


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
    tools: list[dict] | None = None,
    out: dict | None = None,
    extra: dict | None = None,
) -> Generator[str, None, None]:
    """流式产出增量文本。

    tools: OpenAI tools 定义（function calling 双轨的原生轨），None = 不带。
    out:   传入 dict 时，流结束后回填：
             out["tool_calls"]  模型吐的原生 tool_calls（没吐就是空）
             out["finish_reason"]
             out["usage"]       接口回传的 usage（很多接口不回，可能没有）
    extra: 自定义请求参数（top_p / max_tokens / response_format…），原样合并。
    """
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
    if tools:
        payload["tools"] = tools
    apply_extra(payload, extra)

    if out is not None:
        out["tool_calls"] = []
        out["finish_reason"] = ""
        out["usage"] = None

    # tool_calls 是按 index 分片吐的，这里拼回完整对象
    tc_buf: dict[int, dict] = {}

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
                    break
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

            if isinstance(obj, dict) and obj.get("usage") and out is not None:
                out["usage"] = obj["usage"]

            choices = obj.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if out is not None and choice.get("finish_reason"):
                out["finish_reason"] = choice["finish_reason"]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece
            elif on_reasoning is not None and delta.get("reasoning_content"):
                # 推理型模型（deepseek-flash 等）先吐思考内容，正文还没开始
                on_reasoning(delta["reasoning_content"])

            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index") or 0)
                slot = tc_buf.setdefault(
                    idx, {"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]

    if out is not None and tc_buf:
        out["tool_calls"] = [tc_buf[i] for i in sorted(tc_buf)]


def chat_once(api: dict, messages: list[dict], temperature: float = 0.3,
              max_tokens: int = 2048, timeout: int = 180, kind: str = "aux",
              extra: dict | None = None) -> str:
    """非流式，一次性拿完整回复。用于记忆总结、识图。

    推理模型（deepseek-flash / reasoner 之类）的思考 token 也算在 max_tokens 里。
    如果思考把额度吃光，接口会返回 content="" + finish_reason="length"，
    正文一个字都没有。以前这里原样返回空串，调用方以为模型没话说，静默失败。
    现在：碰到这种情况自动把 max_tokens 翻倍重试一次，还空就抛明确的错误。
    """
    if not api.get("api_key"):
        raise LLMError("这条 API 没配 Key。")

    budget = max(64, int(max_tokens or 2048))
    attempts = 2
    last_reason = ""
    for i in range(attempts):
        payload = {
            "model": api.get("model", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": budget,
        }
        # max_tokens 由这里的重试逻辑管，不接受外部覆盖
        apply_extra(payload, extra, block={"max_tokens"})
        started = time.time()
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
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            result = (msg.get("content") or "").strip()
            last_reason = choice.get("finish_reason") or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"返回格式不认识：{str(data)[:400]}")

        # 精确用量优先，没有就留空让 usage 回落到字符估算
        u = data.get("usage") or {}
        in_tk = u.get("prompt_tokens")
        out_tk = u.get("completion_tokens")
        reason_tk = ((u.get("completion_tokens_details") or {})
                     .get("reasoning_tokens") or 0)

        try:
            from . import usage

            in_chars = sum(len(m.get("content") or "") if isinstance(m.get("content"), str)
                           else 0 for m in messages)
            usage.record(str(api.get("model") or "?"), kind, in_chars, len(result),
                         time.time() - started,
                         in_tokens=in_tk, out_tokens=out_tk,
                         reasoning_tokens=reason_tk)
        except Exception:
            pass

        if result:
            return result

        # 空正文。两种成因都见过：
        #   1) finish_reason=length —— 推理模型的思考把额度吃光了，加大额度重试
        #   2) finish_reason=stop   —— 模型偶发跑偏（reasoning 里冒出 <ds_xxx> 控制 token，
        #      正文被过滤成空串），原样重试一次基本能好
        if i + 1 < attempts:
            if last_reason == "length":
                budget = min(budget * 4, 16000)
            continue

        if last_reason == "length":
            raise LLMError(
                f"模型把 {budget} 个 token 全用在思考上，正文没写出来（finish_reason=length）。"
                "这是推理模型的固有行为，把 max_tokens 调大再试，或者换个非推理模型。"
            )
        snippet = str(data)[:300]
        raise LLMError(
            f"接口连续两次返回空正文（finish_reason={last_reason or '?'}）。"
            f"多为模型端偶发跑偏，稍后重试；持续这样换个模型。原始返回：{snippet}"
        )

    return ""


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
