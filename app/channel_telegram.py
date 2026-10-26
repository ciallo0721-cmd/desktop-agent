# -*- coding: utf-8 -*-
"""Telegram 通道：把 agent 接到 TG 上，手机远程遥控电脑。

只依赖 requests（本来就装了），用长轮询收消息，不需要公网 IP 和域名。
配置在 data/config.json：

  "telegram": {
    "enabled": true,
    "bot_token": "123456:ABC-...",        // @BotFather 建机器人拿 token
    "allowed_users": [123456789],         // 只听这些用户 id（@userinfobot 查自己）
    "proxy": "http://127.0.0.1:7890"      // 可选，本机 Clash 之类的代理
  }

命令：
  /new    新建对话
  /stop   停止当前这轮
  其他文字 = 当成用户消息发给 agent

安全约定：
  - allowed_users 是硬门槛，不在名单里的人一个字都不会理。
  - 高危命令在这个通道里没有确认弹窗，会按「无交互环境」直接拒绝。
"""
from __future__ import annotations

import threading
import time

import requests

from . import config

API = "https://api.telegram.org/bot{token}/{method}"
_thread: threading.Thread | None = None

MAX_LEN = 3900          # TG 单条消息上限 4096，留余量


def _api(token: str, method: str, proxies: dict | None = None, **params) -> dict:
    r = requests.post(
        API.format(token=token, method=method), json=params,
        proxies=proxies, timeout=45,
    )
    return r.json()


def _proxies(cfg: dict) -> dict | None:
    p = str((cfg.get("telegram") or {}).get("proxy") or "").strip()
    return {"http": p, "https": p} if p else None


def _send(token: str, chat_id: int, text: str, proxies) -> None:
    text = text or "（空回复）"
    for i in range(0, len(text), MAX_LEN):
        try:
            _api(token, "sendMessage", proxies,
                 chat_id=chat_id, text=text[i:i + MAX_LEN])
        except Exception:
            pass


def _allowed(cfg: dict, user_id: int) -> bool:
    allow = (cfg.get("telegram") or {}).get("allowed_users") or []
    return (not allow) or (user_id in allow)


def push_to_owner(text: str) -> None:
    """给 allowed_users 里的第一个人推一条通知（定时任务完成等场景）。"""
    cfg = config.load_config()
    tg = cfg.get("telegram") or {}
    if not tg.get("enabled") or not str(tg.get("bot_token") or "").strip():
        return
    allow = tg.get("allowed_users") or []
    if not allow:
        return
    _send(str(tg["bot_token"]), int(allow[0]), text, _proxies(cfg))


def _loop() -> None:
    cfg = config.load_config()
    tg = cfg.get("telegram") or {}
    token = str(tg.get("bot_token") or "").strip()
    proxies = _proxies(cfg)
    offset = 0

    from .session import Session

    sess = Session(cfg)
    sess.cfg = cfg

    while True:
        try:
            data = _api(token, "getUpdates", proxies,
                        offset=offset, timeout=30,
                        allowed_updates=["message"])
            for upd in data.get("result") or []:
                offset = max(offset, upd.get("update_id", 0) + 1)
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                user_id = (msg.get("from") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                if not _allowed(cfg, user_id):
                    continue

                if text == "/new":
                    sess.new()
                    _send(token, chat_id, "已新建对话。", proxies)
                    continue
                if text == "/stop":
                    sess.stop()
                    _send(token, chat_id, "已请求停止当前这轮。", proxies)
                    continue

                ok, err = sess.send(text)
                if not ok:
                    _send(token, chat_id, f"发送失败：{err}", proxies)
                    continue

                # 等这轮跑完，把最终回复发回去
                start = time.time()
                reply = ""
                confirmed = False
                while time.time() - start < 900:
                    for evt in sess.poll():
                        t = evt.get("type")
                        if t == "assistant_end":
                            reply = evt.get("text", "")
                        elif t == "confirm":
                            # 远程没有确认弹窗，一律拒绝并告知
                            sess.answer_confirm(False)
                            confirmed = True
                        elif t == "error":
                            reply = (reply or "") + f"\n错误：{evt.get('msg', '')}"
                        elif t in ("done", "_idle"):
                            if confirmed:
                                reply = ("⚠️ 这轮里有高危命令，远程通道没有确认环境，"
                                         "已自动拒绝。要执行请回到电脑前确认。\n\n" + (reply or ""))
                            _send(token, chat_id, reply or "（这轮没有文字回复）", proxies)
                            reply = ""
                            confirmed = False
                            break
                    time.sleep(1.0)
                else:
                    sess.stop()
                    _send(token, chat_id, "这轮超过 15 分钟没跑完，已停止。", proxies)
        except Exception:
            time.sleep(5)       # 网络抖动就等 5s 重试


def start() -> None:
    """如果配置启用了 Telegram，就起后台轮询线程（进程里只起一个）。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    tg = (config.load_config().get("telegram") or {})
    if not tg.get("enabled") or not str(tg.get("bot_token") or "").strip():
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
