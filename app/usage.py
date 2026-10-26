# -*- coding: utf-8 -*-
"""token 花费统计：每次请求记一行，Web 面板看汇总。

优先用接口回传的 usage（精确值，含 reasoning_tokens）；接口不回（部分流式实现）
才回落到字符数估算（中英混按 ~2 字符/token）。行里 exact 字段标明是哪种。

落盘 data/usage.jsonl，一行一条：
{"time": "...", "model": "...", "kind": "chat", "in_chars": 5200,
 "out_chars": 830, "in_tokens": 2600, "out_tokens": 415,
 "reasoning_tokens": 66, "exact": true, "elapsed": 6.2}

Web 面板 /api/usage 汇总：今天 + 最近 7 天按天、按模型。
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import datetime

from . import config

LOCK = threading.Lock()

CHARS_PER_TOKEN = 2.0      # 中英混合的粗估


def usage_path():
    return config.DATA_DIR / "usage.jsonl"


def record(model: str, kind: str, in_chars: int, out_chars: int,
           elapsed: float, in_tokens: int | None = None,
           out_tokens: int | None = None,
           reasoning_tokens: int = 0) -> None:
    """记一行用量。

    in_tokens / out_tokens 传了就用精确值（接口回传的 usage），
    没传才回落到字符数估算。推理模型的思考 token 单独记在 reasoning_tokens，
    它已经包含在 out_tokens 里（计费口径一致），这里只是额外可见。
    """
    exact = in_tokens is not None and out_tokens is not None
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model or "?",
        "kind": kind,
        "in_chars": int(in_chars),
        "out_chars": int(out_chars),
        "in_tokens": int(in_tokens) if exact else int(in_chars / CHARS_PER_TOKEN),
        "out_tokens": int(out_tokens) if exact else int(out_chars / CHARS_PER_TOKEN),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "exact": exact,
        "elapsed": round(float(elapsed), 1),
    }
    try:
        with LOCK:
            with open(usage_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_rows(days: int = 7) -> list[dict]:
    try:
        lines = usage_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    cutoff = time.time() - days * 86400
    for line in lines[-5000:]:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = time.mktime(time.strptime(r["time"], "%Y-%m-%d %H:%M:%S"))
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            r["_ts"] = ts
            out.append(r)
    return out


def summarize(days: int = 7) -> dict:
    """给 Web 面板用的汇总。"""
    rows = _read_rows(days)
    today = datetime.now().strftime("%Y-%m-%d")
    by_day: dict[str, dict] = defaultdict(lambda: {"requests": 0, "tokens": 0})
    by_model: dict[str, dict] = defaultdict(lambda: {"requests": 0, "tokens": 0})
    for r in rows:
        day = r["time"][:10]
        tk = r.get("in_tokens", 0) + r.get("out_tokens", 0)
        d = by_day[day]
        d["requests"] += 1
        d["tokens"] += tk
        m = by_model[r.get("model", "?")]
        m["requests"] += 1
        m["tokens"] += tk
    today_rows = [r for r in rows if r["time"][:10] == today]
    today_tokens = sum(r.get("in_tokens", 0) + r.get("out_tokens", 0) for r in today_rows)
    today_reason = sum(r.get("reasoning_tokens", 0) for r in today_rows)
    return {
        "today": {"requests": len(today_rows), "tokens": today_tokens,
                  "reasoning_tokens": today_reason},
        "by_day": [{"day": k, **v} for k, v in sorted(by_day.items())],
        "by_model": [{"model": k, **v} for k, v in
                     sorted(by_model.items(), key=lambda kv: -kv[1]["tokens"])[:8]],
        "note": "接口回传 usage 的请求为精确值，其余按字符数估算（约 2 字符/token）",
    }
