# -*- coding: utf-8 -*-
"""mock_llm.py —— 本地假 LLM 服务，用来查看 agent 到底发了什么。

用法：
    python mock_llm.py            启动服务（默认端口 16801）
    python mock_llm.py switch     把 config.json 的三个 API 指向本地（原配置自动备份）
    python mock_llm.py restore    把原配置还原回来

每个请求都会：
  1. 完整落盘到 data/mock_llm/<时间>.json（原样 JSON，含系统提示词、全部消息）
  2. 控制台打印一行摘要（几条消息 / 系统提示词多少字符 / 是否流式）

返回内容是一句提示，告诉 agent 这次请求存到了哪个文件。
只依赖标准库。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 16801
BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "mock_llm"
CONFIG = BASE / "data" / "config.json"
BACKUP = BASE / "data" / "mock_llm" / "config-backup.json"

MOCK = {
    "base_url": f"http://127.0.0.1:{PORT}/v1",
    "api_key": "sk-local-mock",
    "model": "mock-model",
}
API_KEYS = ("main_api", "vision_api", "memory_api")


def do_switch() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    old = {k: cfg.get(k) for k in API_KEYS}
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    for k in API_KEYS:
        cfg[k] = dict(MOCK)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已把 main_api / vision_api / memory_api 指向本地 Mock，原配置备份在：")
    print(f"  {BACKUP}")
    print("还原：python mock_llm.py restore")


def do_restore() -> None:
    if not BACKUP.exists():
        print("没有找到备份，没什么可还原的。")
        return
    old = json.loads(BACKUP.read_text(encoding="utf-8"))
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for k, v in old.items():
        if v:
            cfg[k] = v
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    BACKUP.unlink()
    print("已还原原 API 配置。")


def serve() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 关掉默认访问日志
            pass

        def do_POST(self):
            if not self.path.rstrip("/").endswith("chat/completions"):
                self.send_error(404, "only /v1/chat/completions")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) or b"{}"
            try:
                body = json.loads(raw)
            except UnicodeDecodeError:
                # 有的客户端（比如 Git Bash 的 curl）会按本地编码发中文
                body = json.loads(raw.decode("gbk", "replace"))

            ts = time.strftime("%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
            path = OUT / f"{ts}.json"
            path.write_text(
                json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            msgs = body.get("messages") or []
            sys_len = sum(
                len(str(m.get("content") or ""))
                for m in msgs
                if m.get("role") == "system"
            )
            total = sum(len(str(m.get("content") or "")) for m in msgs)
            print(
                f"[{ts}] {len(msgs)} 条消息 | 系统 {sys_len} 字 | 共 {total} 字 | "
                f"stream={body.get('stream')} | -> {path.name}",
                flush=True,
            )

            reply = (
                f"【本地 Mock 收到了】共 {len(msgs)} 条消息，"
                f"系统提示词约 {sys_len} 字符，完整请求已存到 "
                f"data/mock_llm/{path.name}"
            )
            stream = bool(body.get("stream"))
            created = int(time.time())
            mid = f"chatcmpl-mock-{ts}"

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                def chunk(piece: str, finish=None) -> None:
                    data = {
                        "id": mid, "object": "chat.completion.chunk",
                        "created": created, "model": body.get("model"),
                        "choices": [{"index": 0,
                                     "delta": {"content": piece} if piece else {},
                                     "finish_reason": finish}],
                    }
                    self.wfile.write(
                        f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
                    )

                for i in range(0, len(reply), 30):
                    chunk(reply[i:i + 30])
                chunk("", finish="stop")
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                data = {
                    "id": mid, "object": "chat.completion",
                    "created": created, "model": body.get("model"),
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": reply},
                                 "finish_reason": "stop"}],
                }
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

    print(f"Mock LLM:  http://127.0.0.1:{PORT}/v1/chat/completions")
    print(f"请求记录:  {OUT}")
    print("Ctrl+C 停止\n", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "switch":
        do_switch()
    elif cmd == "restore":
        do_restore()
    else:
        serve()
