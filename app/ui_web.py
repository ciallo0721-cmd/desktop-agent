# -*- coding: utf-8 -*-
"""Web 模式：浏览器访问 http://127.0.0.1:16800

只用标准库的 http.server，不依赖 Flask —— 少一个要装的包。
SSE 长连接，流式表现跟 GUI 一致；默认只监听本机回环，外人扫不到。

远程模式（web_remote: true）：监听 0.0.0.0，局域网/公网穿透后都能访问。
远程必须配 web_token，所有请求带 ?token=xxx 或 X-Agent-Token 头，
token 不对一律 403 —— 否则等于把整台电脑的门钥匙挂在网上。
"""
from __future__ import annotations

import base64
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import agent_core, config, images, market, scheduler, skills, usage
from .session import Session

HOST = "127.0.0.1"
PORT = 16800


def _sse(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


# --------------------------------------------------------------------------
# 请求参数设置（温度 / 思考显示 / 自定义参数）
# --------------------------------------------------------------------------
MAX_EXTRA_KEYS = 24


BOOL_KEYS = (
    "show_reasoning", "screen_control", "vision_passthrough",
    "web_search", "skills_enabled", "web_remote",
)


def _mask_key(key: str) -> str:
    """API Key 只回显头尾，前端留空表示不修改。"""
    k = str(key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:4]}***{k[-4:]}"


def _sandbox_level(cfg: dict) -> str:
    if str(cfg.get("sandbox_mode") or "").strip().lower() == "wsb":
        return "wsb"
    return "dir" if cfg.get("sandbox", True) else "off"


def _api_snapshot(cfg: dict, name: str) -> dict:
    sec = cfg.get(name)
    if not isinstance(sec, dict):
        sec = {}
    return {
        "base_url": str(sec.get("base_url") or ""),
        "model": str(sec.get("model") or ""),
        "key_hint": _mask_key(sec.get("api_key")),
        "has_key": bool(str(sec.get("api_key") or "").strip()),
    }


def _settings_snapshot(cfg: dict) -> dict:
    extra = cfg.get("extra_params")
    if not isinstance(extra, dict):
        extra = {}
    token = str(cfg.get("web_token") or "").strip()
    return {
        "temperature": float(cfg.get("temperature", 0.7) or 0),
        "show_reasoning": bool(cfg.get("show_reasoning", True)),
        "extra_params": extra,
        "native_tools": str(cfg.get("native_tools", "auto")),
        "model": str((cfg.get("main_api") or {}).get("model") or ""),
        "user_name": str(cfg.get("user_name") or "用户"),
        "main_api": _api_snapshot(cfg, "main_api"),
        "vision_api": _api_snapshot(cfg, "vision_api"),
        "memory_api": _api_snapshot(cfg, "memory_api"),
        "agent_mode": str(cfg.get("agent_mode", "both")),
        "max_steps": int(cfg.get("max_steps", 12) or 12),
        "memory_every": int(cfg.get("memory_every", 6) or 6),
        "sandbox_level": _sandbox_level(cfg),
        "sandbox_network": bool(cfg.get("sandbox_network", False)),
        "screen_control": bool(cfg.get("screen_control", True)),
        "vision_passthrough": bool(cfg.get("vision_passthrough", True)),
        "web_search": bool(cfg.get("web_search", True)),
        "skills_enabled": bool(cfg.get("skills_enabled", True)),
        "web_remote": bool(cfg.get("web_remote")),
        "token_set": bool(token),
        "token_hint": _mask_key(token),
    }


def _apply_api_block(cfg: dict, name: str, sec) -> str:
    """写一个 api 段。api_key 留空 = 保持原值。返回错误文本，空串 = 成功。"""
    if not isinstance(sec, dict):
        return f"{name} 格式不对"
    cur = cfg.get(name)
    cur = dict(cur) if isinstance(cur, dict) else {}
    for field in ("base_url", "model"):
        if field in sec:
            cur[field] = str(sec.get(field) or "").strip()
    key = sec.get("api_key")
    if isinstance(key, str):
        key = key.strip()
        # 前端把掩码值回传时忽略，避免把 sk-3***3791 存成真 key
        if key and "***" not in key:
            cur["api_key"] = key
    cfg[name] = cur
    return ""


def _apply_settings(cfg: dict, data: dict) -> tuple[bool, str]:
    """把前端提交的设置写进 cfg（不落盘，调用方负责 save）。返回 (ok, err)。"""
    if "temperature" in data:
        try:
            t = float(data.get("temperature"))
        except (TypeError, ValueError):
            return False, "温度必须是数字"
        if not (0.0 <= t <= 2.0):
            return False, "温度范围 0 ~ 2"
        cfg["temperature"] = round(t, 3)

    for name in ("main_api", "vision_api", "memory_api"):
        if name in data:
            err = _apply_api_block(cfg, name, data.get(name))
            if err:
                return False, err

    if "user_name" in data:
        cfg["user_name"] = str(data.get("user_name") or "").strip() or "用户"

    if "agent_mode" in data:
        mode = str(data.get("agent_mode") or "").strip().lower()
        if mode not in ("bash", "screen", "both"):
            return False, "交互方式只能是 bash / screen / both"
        cfg["agent_mode"] = mode

    for key, lo, hi, label in (
        ("max_steps", 1, 60, "最大步数"),
        ("memory_every", 1, 60, "记忆轮数"),
    ):
        if key in data:
            try:
                v = int(data.get(key))
            except (TypeError, ValueError):
                return False, f"{label}必须是整数"
            if not (lo <= v <= hi):
                return False, f"{label}范围 {lo} ~ {hi}"
            cfg[key] = v

    if "sandbox_level" in data:
        lvl = str(data.get("sandbox_level") or "").strip().lower()
        if lvl not in ("off", "dir", "wsb"):
            return False, "沙箱档位只能是 off / dir / wsb"
        cfg["sandbox"] = lvl != "off"
        cfg["sandbox_mode"] = "wsb" if lvl == "wsb" else "off"

    if "sandbox_network" in data:
        cfg["sandbox_network"] = bool(data.get("sandbox_network"))

    for key in BOOL_KEYS:
        if key in data:
            cfg[key] = bool(data.get(key))

    if "web_token" in data:
        tok = str(data.get("web_token") or "").strip()
        if tok and "***" not in tok:
            cfg["web_token"] = tok

    if "extra_params" in data:
        raw = data.get("extra_params")
        if raw in (None, ""):
            raw = {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except json.JSONDecodeError as e:
                return False, f"自定义参数不是合法 JSON：{e}"
        if not isinstance(raw, dict):
            return False, "自定义参数必须是 JSON 对象（{...}）"
        if len(raw) > MAX_EXTRA_KEYS:
            return False, f"自定义参数最多 {MAX_EXTRA_KEYS} 个键"
        clean: dict = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                return False, "参数名必须是非空字符串"
            if k in llm_reserved():
                return False, f"「{k}」是协议保留字段，不能自定义"
            clean[k.strip()] = v
        cfg["extra_params"] = clean

    return True, ""


def llm_reserved() -> set[str]:
    from .llm import RESERVED_PARAMS

    return set(RESERVED_PARAMS)


def _visible(conv) -> list[dict]:
    out = []
    if not conv:
        return out
    for m in conv.visible():
        if m.get("tool_result"):
            out.append({"role": "tool", "name": m.get("tool", "tool"),
                        "text": m.get("content", "")})
        else:
            role = m.get("role", "user")
            text = m.get("content", "")
            if role == "assistant":
                text = agent_core.strip_actions(text)
            out.append({"role": role, "text": text,
                        "images": m.get("images") or []})
    return out


def _safe_image(rel: str):
    """只允许读 data/images 里的文件，挡住 ../ 穿越。"""
    p = images.resolve(rel)
    if p is None:
        return None
    try:
        p.resolve().relative_to(config.IMAGE_DIR.resolve())
    except ValueError:
        return None
    return p


class SessionHub:
    """把 Session 的事件分发给所有连上来的浏览器。"""

    def __init__(self, sess: Session):
        self.sess = sess
        self.subs: list[list] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def subscribe(self) -> list:
        q: list = []
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q: list) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def _pump(self) -> None:
        while True:
            evts = self.sess.poll()
            if evts:
                with self.lock:
                    for e in evts:
                        for q in self.subs:
                            q.append(e)
            time.sleep(0.04)


def make_handler(hub: SessionHub, sess: Session):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "DesktopAgent/1.0"

        # ---------------- 基础 ----------------
        def log_message(self, *_a):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b""
            try:
                return json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return {}

        def _auth(self) -> bool:
            """token 校验。没配 web_token（只监听本机）时直接放行。"""
            token = str((sess.cfg.get("web_token") or "")).strip()
            if not token:
                return True
            q = parse_qs(urlparse(self.path).query)
            given = (q.get("token") or [""])[0] or \
                    (self.headers.get("X-Agent-Token") or "")
            return hmac.compare_digest(given.strip(), token)

        # ---------------- GET ----------------
        def do_GET(self):
            path = self.path.split("?")[0]

            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return

            # 页面本身可以空手打开，但所有数据接口都要验 token
            if (path.startswith("/api/") or path == "/img") and not self._auth():
                self._json({"ok": False, "err": "token 不对，拒绝访问"}, 403)
                return

            if path == "/api/conversations":
                self._json({"items": [{
                    "id": c.id,
                    "title": c.title,
                    "turns": c.user_turns(),
                    "updated": c.updated,
                    "current": bool(sess.conv and c.id == sess.conv.id),
                } for c in sess.conversations()]})
                return

            if path == "/api/messages":
                self._json({"messages": _visible(sess.conv)})
                return

            if path == "/api/stream":
                self._stream()
                return

            if path == "/api/skills":
                self._json({
                    "enabled": bool(sess.cfg.get("skills_enabled", True)),
                    "dir": str(config.SKILL_DIR),
                    "items": [{
                        "id": s.sid,
                        "name": s.name,
                        "desc": " ".join((s.description or "").split()),
                        "enabled": s.enabled,
                        "files": s.attachments()[:6],
                    } for s in skills.list_skills(sess.cfg, fresh=True)],
                })
                return

            if path == "/api/schedule":
                self._json({"items": scheduler.load_tasks()})
                return

            if path == "/api/usage":
                self._json(usage.summarize(7))
                return

            if path == "/api/settings":
                self._json(_settings_snapshot(sess.cfg))
                return

            if path == "/api/market":
                market.ensure_scaffold()
                self._json({"items": market.load_market()})
                return

            if path == "/img":
                self._image()
                return

            self._json({"ok": False, "err": "not found"}, 404)

        def _image(self) -> None:
            q = parse_qs(urlparse(self.path).query)
            rel = (q.get("p") or [""])[0]
            p = _safe_image(rel)
            if p is None:
                self._json({"ok": False, "err": "没有这张图"}, 404)
                return
            try:
                body = p.read_bytes()
            except OSError:
                self._json({"ok": False, "err": "读不出来"}, 404)
                return
            ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            self._send(200, body, ctype)

        # ---------------- POST ----------------
        def do_POST(self):
            if not self._auth():
                self._json({"ok": False, "err": "token 不对，拒绝访问"}, 403)
                return
            path = self.path.split("?")[0]
            data = self._body()

            if path == "/api/send":
                imgs = data.get("images") or []
                if not isinstance(imgs, list):
                    imgs = []
                ok, err = sess.send((data.get("text") or "").strip(),
                                    [str(x) for x in imgs])
                self._json({"ok": ok, "err": err})
                return

            if path == "/api/upload":
                raw = data.get("data") or ""
                if not isinstance(raw, str) or not raw:
                    self._json({"ok": False, "err": "没收到图片数据"})
                    return
                if raw.startswith("data:") and "," in raw:
                    raw = raw.split(",", 1)[1]
                try:
                    blob = base64.b64decode(raw, validate=False)
                except Exception:
                    self._json({"ok": False, "err": "图片数据不合法"})
                    return
                cid = sess.conv.id if sess.conv else "_"
                try:
                    rel = images.save(blob, cid)
                except images.ImageError as e:
                    self._json({"ok": False, "err": str(e)})
                    return
                self._json({"ok": True, "path": rel})
                return

            if path == "/api/stop":
                sess.stop()
                self._json({"ok": True})
                return

            if path == "/api/confirm":
                sess.answer_confirm(bool(data.get("ok")))
                self._json({"ok": True})
                return

            if path == "/api/new":
                sess.new()
                self._json({"ok": True})
                return

            if path == "/api/switch":
                cid = data.get("id")
                for c in sess.conversations():
                    if c.id == cid:
                        sess.switch(c)
                        self._json({"ok": True,
                                    "messages": _visible(sess.conv)})
                        return
                self._json({"ok": False, "err": "没有这个对话"})
                return

            if path == "/api/schedule":
                op = str(data.get("op") or "").strip()
                if op == "add":
                    ok, msg = scheduler.add_task(data.get("task") or {})
                elif op == "remove":
                    ok, msg = scheduler.remove_task(str(data.get("id") or ""))
                elif op == "toggle":
                    ok, msg = scheduler.toggle_task(str(data.get("id") or ""),
                                                    bool(data.get("enabled", True)))
                else:
                    ok, msg = False, f"未知 op：{op!r}"
                self._json({"ok": ok, "err": msg if not ok else ""})
                return

            if path == "/api/market/install":
                msg = market.install_skill(str(data.get("id") or ""))
                self._json({"ok": msg.startswith("✅"), "msg": msg})
                return

            if path == "/api/skills/toggle":
                if "master" in data:
                    sess.cfg["skills_enabled"] = bool(data.get("master"))
                    try:
                        config.save_config(sess.cfg)
                    except OSError:
                        pass
                    self._json({"ok": True})
                    return
                sid = str(data.get("id") or "").strip()
                if not sid:
                    self._json({"ok": False, "err": "缺 id"})
                    return
                skills.set_enabled(sess.cfg, sid, bool(data.get("enabled")))
                self._json({"ok": True})
                return

            if path == "/api/settings":
                ok, err = _apply_settings(sess.cfg, data)
                if not ok:
                    self._json({"ok": False, "err": err})
                    return
                try:
                    config.save_config(sess.cfg)
                except OSError as e:
                    self._json({"ok": False, "err": f"写配置失败：{e}"})
                    return
                self._json({"ok": True, **_settings_snapshot(sess.cfg)})
                return

            self._json({"ok": False, "err": "not found"}, 404)

        # ---------------- SSE ----------------
        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True

            q = hub.subscribe()
            buf = ""
            try:
                while True:
                    batch, q[:] = q[:], []
                    if not batch:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        time.sleep(0.05)
                        continue

                    for e in batch:
                        kind = e.get("type")
                        out = None

                        if kind == "_idle":
                            out = {"type": "idle"}
                        elif kind == "delta":
                            buf += e.get("text", "")
                            out = {"type": "stream",
                                   "text": agent_core.strip_streaming(buf)}
                        elif kind == "thinking":
                            out = {"type": "thinking"}
                        elif kind == "reasoning":
                            out = {"type": "reasoning", "text": e.get("text", "")}
                        elif kind == "assistant_end":
                            out = {"type": "assistant",
                                   "text": agent_core.strip_actions(e.get("text", ""))}
                            buf = ""
                        elif kind == "tool_start":
                            out = {"type": "tool_start", "name": e.get("name"),
                                   "detail": e.get("detail", "")}
                        elif kind == "tool_result":
                            out = {"type": "tool_result", "name": e.get("name"),
                                   "text": e.get("text", "")}
                        elif kind == "confirm":
                            out = {"type": "confirm", "cmd": e.get("cmd", ""),
                                   "reason": e.get("reason", "")}
                        elif kind in ("done", "cancel", "error", "ask",
                                      "mem_ok", "mem_fail"):
                            out = e

                        if out:
                            self.wfile.write(_sse(out))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def _is_our_instance(port: int) -> bool:
    """端口上是不是已经在跑一个 PawPilot（200 正常 / 401 需要 token 都算）。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/settings", timeout=2.0
        ) as resp:
            body = resp.read(400)
        return b"temperature" in body
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)
    except Exception:
        return False


def run(cfg: dict | None = None) -> int:
    sess = Session(cfg)
    hub = SessionHub(sess)

    conf = sess.cfg
    token = str(conf.get("web_token") or "").strip()
    remote = bool(conf.get("web_remote"))
    if remote and not token:
        print("！开了 web_remote 但没设 web_token，拒绝远程监听（等于裸奔）。")
        print("  在 data/config.json 里给 web_token 随便编一个长随机串，再启动。")
        return 1
    host = "0.0.0.0" if remote else HOST
    port = PORT

    url = f"http://{HOST}:{port}" + (f"/?token={token}" if remote else "")

    # Windows 的 SO_REUSEADDR 允许两个进程绑同一端口，会变成两个服务抢请求。
    # 所以先探一下：已经有 PawPilot 在跑就不要再绑，直接把人送过去。
    if _is_our_instance(port):
        print(f"端口 {port} 上已经有一个 PawPilot 在跑了，直接打开浏览器。")
        print(f"  {url}")
        print("  要重开一个就先关掉旧的（Ctrl+C 或关掉那个窗口）。")
        _open_browser(url)
        return 0

    model = (conf.get("main_api") or {}).get("model") or "未配置"
    print("=" * 46)
    print("  PawPilot · Web")
    if remote:
        try:
            import socket

            lan = socket.gethostbyname(socket.gethostname())
        except Exception:
            lan = "<本机IP>"
        print(f"  http://{lan}:{port}/?token={token}")
        print("  远程模式已开启：只把这个链接给信得过的人。")
    else:
        print(f"  http://{HOST}:{port}")
    print(f"  模型 {model}  ·  方式 {conf.get('agent_mode', 'both')}")
    print("  Ctrl+C 关闭服务")
    print("=" * 46)

    if not config.is_configured(conf):
        print("！没有配置主 API Key，发消息会失败。先跑 first.bat。")

    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(hub, sess))
    except OSError as e:
        print(f"端口 {port} 起不来：{e}")
        print("可能已经开着一个了，先关掉再试。")
        return 1

    httpd.daemon_threads = True
    if bool(conf.get("web_open_browser", True)):
        threading.Timer(0.8, lambda: _open_browser(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


# ======================================================================
# 前端页面（单文件，不依赖 CDN，断网也能开）
# ======================================================================

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PawPilot</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: "Microsoft YaHei UI", "Segoe UI", system-ui, sans-serif;
    background: #fff; color: #1f2328; display: flex; overflow: hidden;
  }
  #side {
    width: 216px; flex: 0 0 216px; background: #f5f6f8;
    border-right: 1px solid #e3e6eb; display: flex; flex-direction: column;
  }
  #side h1 { font-size: 15px; margin: 16px 14px 10px; }
  #convList { overflow-y: auto; padding: 0 8px; flex: 1; }
  .conv {
    padding: 7px 9px; border-radius: 6px; font-size: 12.5px; cursor: pointer;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    display: flex; justify-content: space-between; gap: 6px;
  }
  .conv:hover { background: #e9ecf1; }
  .conv.on { background: #e0e7f2; font-weight: 600; }
  .conv span { color: #9aa1ab; font-size: 11px; flex: 0 0 auto; }
  #sideBtns { padding: 8px; }
  #sideBtns button {
    width: 100%; padding: 8px; border: 0; border-radius: 7px; cursor: pointer;
    background: #e6e9ee; color: #33383f; font-size: 12.5px;
    font-family: inherit;
  }
  #sideBtns button:hover { background: #d8dce3; }
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #top {
    height: 44px; border-bottom: 1px solid #e3e6eb; display: flex;
    align-items: center; padding: 0 16px; font-size: 12px; color: #8a9099;
    gap: 12px;
  }
  #top .sp { flex: 1; }
  #top button {
    border: 0; background: #e6e9ee; color: #33383f; border-radius: 6px;
    padding: 5px 12px; font-size: 12px; cursor: pointer; font-family: inherit;
  }
  #chat { flex: 1; overflow-y: auto; padding: 12px 0 20px; }
  .row { display: flex; padding: 3px 16px; }
  .row.user { justify-content: flex-end; }
  .row.ai, .row.tool { justify-content: flex-start; }
  .bubble {
    max-width: 78%; padding: 9px 13px; border-radius: 10px;
    font-size: 13.5px; line-height: 1.62; white-space: pre-wrap;
    word-break: break-word; user-select: text;
  }
  .user .bubble { background: #dce9ff; }
  .ai .bubble { background: #f1f3f5; }
  .tool .bubble {
    background: #fff8e1; border: 1px solid #f0e0a8; font-size: 12px;
    font-family: Consolas, "Courier New", monospace; color: #57503a;
    max-width: 82%; white-space: pre-wrap;
  }
  .tool .bubble b { color: #7a6a2a; font-weight: 600; display: block;
                    margin-bottom: 3px; }
  .err { background: #fdecec; border: 1px solid #f3c9c9; color: #a32a2a; }
  .meta { font-size: 11px; color: #9aa1ab; margin-top: 4px; }
  #bar { border-top: 1px solid #e3e6eb; padding: 10px 16px 14px; }
  #attach { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  #attach.hide { display: none; }
  .thumb {
    position: relative; border: 1px solid #e3e6eb; border-radius: 7px;
    overflow: hidden; background: #fff;
  }
  .thumb img { display: block; width: 76px; height: 76px; object-fit: cover; }
  .thumb b {
    position: absolute; top: 3px; right: 3px; width: 17px; height: 17px;
    border-radius: 9px; background: rgba(0,0,0,.55); color: #fff;
    font-size: 11px; line-height: 17px; text-align: center; cursor: pointer;
    font-weight: 400;
  }
  .msgimg {
    display: block; max-width: 100%; max-height: 330px; border-radius: 8px;
    margin-bottom: 6px; cursor: zoom-in;
  }
  #imgBtn {
    height: 62px; width: 54px; border: 0; border-radius: 8px; cursor: pointer;
    background: #e6e9ee; color: #33383f; font-size: 12px; font-family: inherit;
  }
  #imgBtn:hover { background: #d8dce3; }
  #inputWrap { display: flex; gap: 8px; align-items: flex-end; }
  textarea {
    flex: 1; resize: none; height: 62px; border: 1px solid #e3e6eb;
    border-radius: 8px; padding: 9px 11px; font-size: 13.5px; outline: none;
    font-family: inherit; line-height: 1.5;
  }
  textarea:focus { border-color: #9cc6ff; }
  #send {
    height: 62px; width: 78px; border: 0; border-radius: 8px; cursor: pointer;
    background: #2f6fed; color: #fff; font-size: 13.5px; font-weight: 600;
    font-family: inherit;
  }
  #send.stop { background: #c0392b; }
  #hint { font-size: 10.5px; color: #9aa1ab; margin-top: 6px; }
  #overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.35); display: none;
    align-items: center; justify-content: center; z-index: 9;
  }
  #overlay.on { display: flex; }
  #dlg {
    background: #fff; border-radius: 12px; width: 620px; max-width: 92vw;
    padding: 20px 22px; box-shadow: 0 18px 50px rgba(0,0,0,.22);
  }
  #dlg h2 { margin: 0 0 12px; font-size: 15px; color: #a32a2a; }
  #dlgCmd {
    background: #f5f6f8; border-radius: 7px; padding: 10px 12px;
    font-family: Consolas, monospace; font-size: 12.5px; white-space: pre-wrap;
    word-break: break-all; max-height: 210px; overflow: auto; margin-bottom: 8px;
  }
  #dlgWhy { font-size: 12px; color: #8a9099; margin-bottom: 16px; }
  #dlgBtns { display: flex; justify-content: flex-end; gap: 10px; }
  #dlgBtns button {
    padding: 8px 18px; border-radius: 7px; border: 0; cursor: pointer;
    font-size: 13px; font-family: inherit;
  }
  #no { background: #9ca3af; color: #fff; }
  #yes { background: #c0392b; color: #fff; }
  #welcome { text-align: center; color: #9aa1ab; margin-top: 90px; }
  #welcome h2 { font-size: 24px; color: #1f2328; margin: 0 0 8px; }
  .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
         background: #b9c0c9; margin-right: 5px; }
  .dot.live { background: #35c46a; }
  /* ---- 面板（技能 / 定时任务 / 统计 / 参数 共用样式） ---- */
  #skillPanel, #taskPanel, #statPanel, #setPanel {
    position: fixed; top: 52px; right: 16px; width: 372px; max-height: 76vh;
    background: #fff; border: 1px solid #e3e6eb; border-radius: 10px;
    box-shadow: 0 12px 34px rgba(20,26,36,.16); display: none;
    flex-direction: column; z-index: 40; overflow: hidden;
  }
  #skillPanel.on, #taskPanel.on, #statPanel.on, #setPanel.on { display: flex; }
  #skHead, #tkHead, #stHead, #setHead {
    display: flex; align-items: center; gap: 8px; padding: 10px 12px;
    border-bottom: 1px solid #eef0f3; font-size: 13px; font-weight: 600;
  }
  #skHead .sp, #tkHead .sp, #setHead .sp { flex: 1; }
  #skHead label { font-size: 11.5px; font-weight: 400; color: #5f6670;
                  display: flex; align-items: center; gap: 4px; }
  #skList { overflow-y: auto; padding: 8px; }
  #tkList, #stList { overflow-y: auto; padding: 8px; }
  /* ---- 参数面板 ---- */
  #setPanel { width: 460px; max-height: 84vh; }
  #setBody { overflow-y: auto; padding: 10px 12px; }
  .setGroup {
    font-size: 11px; font-weight: 600; color: #9aa1ab; letter-spacing: .08em;
    margin: 14px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #f0f2f5;
  }
  .setGroup:first-child { margin-top: 0; }
  .setBox {
    border: 1px solid #eef0f3; border-radius: 7px; padding: 7px 9px;
    margin-bottom: 10px; background: #fbfcfd;
  }
  .setBox summary {
    font-size: 12px; font-weight: 600; color: #33383f; cursor: pointer;
    list-style: none; outline: none;
  }
  .setBox summary::-webkit-details-marker { display: none; }
  .setBox summary::before { content: "▸ "; color: #9aa1ab; }
  .setBox[open] summary::before { content: "▾ "; }
  .setBox input { margin-top: 7px; }
  .setRow { margin-bottom: 14px; }
  .setRow > label {
    display: block; font-size: 12px; color: #33383f; font-weight: 600;
    margin-bottom: 5px;
  }
  .setRow .sub { font-size: 11px; color: #9aa1ab; margin-top: 4px;
                 line-height: 1.5; }
  .setRow .val { float: right; font-weight: 400; color: #2f6fed;
                 font-family: Consolas, monospace; }
  .setRow input[type=text], .setRow input[type=number], .setRow input:not([type]),
  .setBox input, .setRow select {
    width: 100%; border: 1px solid #e3e6eb; border-radius: 6px;
    padding: 6px 8px; font-size: 12px; font-family: inherit; background: #fff;
    margin-bottom: 6px; color: #33383f;
  }
  .setRow select { cursor: pointer; }
  .setRow input:focus, .setRow select:focus, .setBox input:focus {
    border-color: #9cc6ff; outline: none;
  }
  .setPair { display: flex; gap: 8px; }
  .setPair > div { flex: 1; }
  #tSlider { width: 100%; margin: 2px 0; accent-color: #2f6fed; }
  #tPresets { display: flex; gap: 5px; margin-top: 6px; flex-wrap: wrap; }
  #tPresets button {
    border: 1px solid #e3e6eb; background: #f7f8fa; color: #5f6670;
    border-radius: 5px; font-size: 11px; padding: 3px 8px; cursor: pointer;
    font-family: inherit;
  }
  #tPresets button:hover { background: #e9ecf1; color: #2f6fed; }
  #setBody textarea {
    width: 100%; height: 96px; resize: vertical;
    font-family: Consolas, monospace; font-size: 11.5px; line-height: 1.55;
    border: 1px solid #e3e6eb; border-radius: 6px; padding: 7px 8px;
  }
  #setBody textarea:focus { border-color: #9cc6ff; outline: none; }
  .setChk { display: flex; align-items: center; gap: 6px; font-size: 12px;
            margin: 6px 0; }
  .setChk input { width: auto; margin: 0; }
  #setSave {
    width: 100%; border: 0; border-radius: 7px; padding: 8px; cursor: pointer;
    background: #2f6fed; color: #fff; font-size: 12.5px; font-family: inherit;
  }
  #setMsg { font-size: 11px; margin-top: 7px; color: #9aa1ab; min-height: 15px; }
  #setMsg.bad { color: #c0392b; }
  /* ---- 思考过程 ---- */
  .row.think { padding: 1px 16px; }
  .think details {
    max-width: 78%; border-left: 2px solid #d7dbe2; padding: 2px 0 2px 10px;
    font-size: 12px; color: #7b828c;
  }
  .think summary {
    cursor: pointer; color: #9aa1ab; font-size: 11.5px; outline: none;
    user-select: none;
  }
  .think summary:hover { color: #5f6670; }
  .think .thinkBody {
    margin-top: 5px; max-height: 260px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-word; line-height: 1.6;
  }
  .sk {
    border: 1px solid #eef0f3; border-radius: 8px; padding: 8px 10px;
    margin-bottom: 7px;
  }
  .sk .h { display: flex; align-items: center; gap: 6px; }
  .sk .h b { font-size: 12.5px; }
  .sk .h i { font-size: 10.5px; color: #9aa1ab; font-style: normal; }
  .sk .h input { margin-left: auto; }
  .sk p { margin: 4px 0 0; font-size: 11.5px; color: #5f6670; line-height: 1.5; }
  .sk small { display: block; margin-top: 4px; color: #a8aeb7; font-size: 10.5px;
              word-break: break-all; }
  #skFoot { padding: 8px 12px; border-top: 1px solid #eef0f3; font-size: 11px;
            color: #9aa1ab; word-break: break-all; }
  .tkForm { padding: 10px 12px; border-top: 1px solid #eef0f3; display: flex;
            flex-direction: column; gap: 6px; font-size: 12px; }
  .tkForm input, .tkForm select {
    border: 1px solid #e3e6eb; border-radius: 6px; padding: 5px 8px;
    font-size: 12px; font-family: inherit;
  }
  .tkForm .row2 { display: flex; gap: 6px; }
  .tkForm .row2 > * { flex: 1; }
  .tkForm button {
    border: 0; border-radius: 6px; padding: 7px; cursor: pointer;
    background: #2f6fed; color: #fff; font-size: 12.5px; font-family: inherit;
  }
  .del { border: 0; background: none; color: #c0392b; cursor: pointer;
         font-size: 11px; font-family: inherit; }
  .stBig { text-align: center; padding: 12px 8px 4px; }
  .stBig b { font-size: 22px; font-weight: 500; }
  .stBig p { margin: 2px 0 0; font-size: 11px; color: #9aa1ab; }
  .stSect { padding: 8px 12px 4px; font-size: 11.5px; color: #5f6670;
            border-top: 1px solid #eef0f3; margin-top: 6px; }
  .stRow { display: flex; align-items: center; gap: 8px; padding: 3px 12px;
           font-size: 12px; }
  .stRow .bar { height: 8px; border-radius: 4px; background: #2f6fed;
                min-width: 2px; }
</style>
</head>
<body>

<div id="side">
  <h1>Agent</h1>
  <div id="convList"></div>
  <div id="sideBtns"><button id="newBtn">＋ 新对话</button></div>
</div>

<div id="main">
  <div id="top">
    <span class="dot" id="dot"></span>
    <span id="status">连接中…</span>
    <span class="sp"></span>
    <span id="model"></span>
    <button id="taskBtn">任务</button>
    <button id="statBtn">统计</button>
    <button id="skillBtn">技能</button>
    <button id="setBtn">参数</button>
  </div>
  <div id="chat"></div>
  <div id="bar">
    <div id="attach" class="hide"></div>
    <div id="inputWrap">
      <textarea id="input" placeholder="说点什么…（Enter 发送，Shift+Enter 换行，Ctrl+V 粘截图）"></textarea>
      <button id="imgBtn">图片</button>
      <input type="file" id="file" accept="image/*" multiple hidden>
      <button id="send">发送</button>
    </div>
    <div id="hint">Ctrl+Enter 也可以发送 · 正在跑的时候按钮会变成「停止」</div>
  </div>
</div>

<div id="skillPanel">
  <div id="skHead">
    <span>技能 Skill</span>
    <span class="sp"></span>
    <label><input type="checkbox" id="skMaster"> 总开关</label>
  </div>
  <div id="skList"></div>
  <div id="skFoot"></div>
</div>

<div id="taskPanel">
  <div id="tkHead"><span>定时任务</span><span class="sp"></span></div>
  <div id="tkList"></div>
  <div class="tkForm">
    <input id="tkName" placeholder="任务名，如：每日早报">
    <textarea id="tkPrompt" rows="2" placeholder="到点让 agent 干什么，如：联网搜今天的科技新闻整理成 5 条"></textarea>
    <div class="row2">
      <select id="tkKind">
        <option value="daily">每天定点</option>
        <option value="interval">间隔循环</option>
      </select>
      <input id="tkTime" placeholder="08:30" style="display:none">
      <input id="tkEvery" placeholder="间隔分钟，如 120" style="display:none">
    </div>
    <button id="tkAdd">＋ 添加任务</button>
  </div>
</div>

<div id="statPanel">
  <div id="stHead"><span>用量统计</span></div>
  <div id="stList"></div>
</div>

<div id="setPanel">
  <div id="setHead">
    <span>设置</span>
    <span class="sp"></span>
    <span id="setModel" style="font-size:11px;font-weight:400;color:#9aa1ab"></span>
  </div>
  <div id="setBody">
    <div class="setGroup">接口</div>
    <div class="setRow">
      <label>主对话 API</label>
      <input id="setMainUrl" placeholder="https://api.deepseek.com">
      <input id="setMainKey" placeholder="API Key（留空不改）" autocomplete="off">
      <input id="setMainModel" placeholder="模型名，如 deepseek-flash">
      <div class="sub">接口地址不要带 /chat/completions。</div>
    </div>
    <details class="setBox" data-api="vis">
      <summary>识图 API（看屏幕用，可留空）</summary>
      <input id="setVisUrl" placeholder="接口地址，留空则用主 API">
      <input id="setVisKey" placeholder="API Key（留空不改）" autocomplete="off">
      <input id="setVisModel" placeholder="模型名，留空则用主模型">
    </details>
    <details class="setBox" data-api="mem">
      <summary>记忆总结 API（留空复用主模型）</summary>
      <input id="setMemUrl" placeholder="接口地址，留空则用主 API">
      <input id="setMemKey" placeholder="API Key（留空不改）" autocomplete="off">
      <input id="setMemModel" placeholder="模型名，留空则用主模型">
    </details>

    <div class="setGroup">模型参数</div>
    <div class="setRow">
      <label>温度 temperature <span class="val" id="tVal">0.7</span></label>
      <input type="range" id="tSlider" min="0" max="2" step="0.05" value="0.7">
      <div id="tPresets">
        <button data-t="0">0 死板</button>
        <button data-t="0.3">0.3 严谨</button>
        <button data-t="0.7">0.7 默认</button>
        <button data-t="1.0">1.0 发散</button>
        <button data-t="1.5">1.5 放飞</button>
      </div>
      <div class="sub">越低越稳、越高越发散。改完立刻对下一条消息生效，不用重启。</div>
    </div>

    <div class="setRow">
      <label class="setChk"><input type="checkbox" id="setThink"> 显示思考过程</label>
      <div class="sub">推理模型（deepseek-flash / reasoner）会把思考单独吐出来，
        开就显示成可折叠的灰色块，关就只留正文。</div>
    </div>

    <div class="setRow">
      <label>自定义请求参数（JSON）</label>
      <textarea id="setExtra" spellcheck="false" placeholder='{
  "top_p": 0.9,
  "frequency_penalty": 0.2,
  "max_tokens": 4096
}'></textarea>
      <div class="sub">原样合并进 /chat/completions 的 payload。<br>
        model / messages / stream / tools 是保留字段，填了也会被忽略。</div>
    </div>

    <div class="setGroup">Agent 行为</div>
    <div class="setRow">
      <label>用户名</label>
      <input id="setUser" placeholder="用户">
      <label style="margin-top:8px">交互方式</label>
      <select id="setMode">
        <option value="both">两者都用</option>
        <option value="bash">命令行 Bash</option>
        <option value="screen">截屏操作</option>
      </select>
      <div class="setPair">
        <div>
          <label>最大连续步数</label>
          <input id="setSteps" type="number" min="1" max="60" step="1">
        </div>
        <div>
          <label>记忆每 N 轮总结</label>
          <input id="setMemEvery" type="number" min="1" max="60" step="1">
        </div>
      </div>
    </div>

    <div class="setRow">
      <label>命令沙箱</label>
      <select id="setSandbox">
        <option value="dir">钉目录 + 高危拦截（快）</option>
        <option value="off">不限制（自己担风险）</option>
        <option value="wsb">Windows Sandbox 真隔离（慢，需专业版）</option>
      </select>
      <label class="setChk" style="margin-top:8px"><input type="checkbox" id="setSandboxNet"> 沙箱内允许联网</label>
      <label class="setChk"><input type="checkbox" id="setScreen"> 允许屏幕控制（点击/打字/按键）</label>
      <label class="setChk"><input type="checkbox" id="setPassthrough"> 截图直接给模型看</label>
      <label class="setChk"><input type="checkbox" id="setWebSearch"> 联网搜索（web_search / web_fetch）</label>
      <label class="setChk"><input type="checkbox" id="setSkills"> 启用技能包</label>
    </div>

    <div class="setGroup">远程访问</div>
    <div class="setRow">
      <label class="setChk"><input type="checkbox" id="setRemote"> 允许局域网/公网访问（重启生效）</label>
      <input id="setToken" placeholder="访问令牌，留空不改" autocomplete="off">
      <div class="sub">开了远程必须设令牌，否则服务拒绝启动。改完要重启才生效。</div>
    </div>

    <button id="setSave">保存</button>
    <div id="setMsg"></div>
  </div>
</div>

<div id="overlay">
  <div id="dlg">
    <h2>AI 想要执行一条高危命令</h2>
    <div id="dlgCmd"></div>
    <div id="dlgWhy"></div>
    <div id="dlgBtns">
      <button id="no">拒绝</button>
      <button id="yes">允许执行</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
// 远程模式下所有接口都要带 token（页面用 ?token=xxx 打开）
const TOKEN = new URLSearchParams(location.search).get('token') || '';
function api(p) {
  if (!TOKEN) return p;
  return p + (p.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
}
const chat = $('#chat');
let busy = false, curAi = null, curTool = null, streaming = false;
let pending = [];        // 待发送图片（服务端相对路径）

function esc(t) {
  return (t || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

function addRow(cls) {
  const row = document.createElement('div');
  row.className = 'row ' + cls;
  const b = document.createElement('div');
  b.className = 'bubble';
  row.appendChild(b);
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
  return b;
}

function scrollBottom() { chat.scrollTop = chat.scrollHeight; }

// ---------------- 思考过程（可折叠） ----------------
let curThink = null, thinkText = '';

function thinkStart() {
  if (curThink) return;
  const row = document.createElement('div');
  row.className = 'row think';
  const det = document.createElement('details');
  det.open = true;
  const sum = document.createElement('summary');
  sum.textContent = '思考中…';
  const body = document.createElement('div');
  body.className = 'thinkBody';
  det.append(sum, body);
  row.appendChild(det);
  chat.appendChild(row);
  curThink = {row, det, sum, body};
  scrollBottom();
}

function thinkAdd(t) {
  if (!t) return;
  thinkStart();
  thinkText += t;
  curThink.body.textContent = thinkText;
  scrollBottom();
}

function thinkDone() {
  if (!curThink) return;
  curThink.det.open = false;
  curThink.sum.textContent = '思考过程 · ' + thinkText.length + ' 字（点开看）';
  curThink = null;
  thinkText = '';
}

function setBusy(v) {
  busy = v;
  const btn = $('#send');
  btn.textContent = v ? '停止' : '发送';
  btn.classList.toggle('stop', v);
  $('#status').textContent = v ? '执行中…' : '空闲';
}

// ---------------- 图片 ----------------
function imgUrl(p) { return api('/img?p=' + encodeURIComponent(p)); }

function fillBubble(b, text, imgs) {
  if (imgs && imgs.length) {
    imgs.forEach(p => {
      const im = document.createElement('img');
      im.className = 'msgimg';
      im.src = imgUrl(p);
      im.onclick = () => window.open(imgUrl(p), '_blank');
      b.appendChild(im);
    });
  }
  if (text) {
    const t = document.createElement('div');
    t.textContent = text;
    b.appendChild(t);
  }
}

function renderAttach() {
  const box = $('#attach');
  box.innerHTML = '';
  if (!pending.length) { box.classList.add('hide'); return; }
  box.classList.remove('hide');
  pending.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'thumb';
    const im = document.createElement('img');
    im.src = imgUrl(p);
    const x = document.createElement('b');
    x.textContent = '×';
    x.onclick = () => { pending.splice(i, 1); renderAttach(); };
    d.appendChild(im);
    d.appendChild(x);
    box.appendChild(d);
  });
}

async function uploadBlob(blob) {
  if (!blob || !blob.size) return;
  const data = await new Promise(res => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result);
    fr.readAsDataURL(blob);
  });
  try {
    const r = await fetch(api('/api/upload'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data})
    });
    const d = await r.json();
    if (!d.ok) { toast(d.err || '图片没传上去'); return; }
    pending.push(d.path);
    renderAttach();
  } catch (e) {
    toast('图片没传上去：' + e);
  }
}

$('#imgBtn').onclick = () => $('#file').click();
$('#file').onchange = e => {
  [...e.target.files].forEach(uploadBlob);
  e.target.value = '';
};
$('#input').addEventListener('paste', e => {
  const items = (e.clipboardData || {}).items || [];
  for (const it of items) {
    if (it.type && it.type.startsWith('image/')) {
      e.preventDefault();
      uploadBlob(it.getAsFile());
      return;
    }
  }
});

// ---------------- 发送 ----------------
async function send() {
  if (busy) { await fetch(api('/api/stop'), {method: 'POST'}); return; }
  const box = $('#input');
  const text = box.value.trim();
  if (!text && !pending.length) return;
  const imgs = pending.slice();
  const r = await fetch(api('/api/send'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text, images: imgs})
  });
  const d = await r.json();
  if (!d.ok) { toast(d.err); return; }
  box.value = '';
  pending = [];
  renderAttach();
  fillBubble(addRow('user'), text, imgs);
  curAi = null; curTool = null;
  setBusy(true);
}

function toast(msg) {
  const b = addRow('ai');
  b.className = 'bubble err';
  b.textContent = msg;
  scrollBottom();
}

$('#send').onclick = send;
$('#input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

// ---------------- 会话 ----------------
async function loadConvs() {
  const r = await fetch(api('/api/conversations'));
  const d = await r.json();
  const box = $('#convList');
  box.innerHTML = '';
  if (!d.items.length) {
    box.innerHTML = '<div class="conv" style="color:#9aa1ab">还没有对话</div>';
    return;
  }
  d.items.forEach(c => {
    const el = document.createElement('div');
    el.className = 'conv' + (c.current ? ' on' : '');
    el.innerHTML = '<span style="color:inherit;font-size:12.5px;overflow:hidden">'
      + esc(c.title) + '</span><span>' + c.turns + '</span>';
    el.onclick = async () => {
      const r = await fetch(api('/api/switch'), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: c.id})
      });
      const d = await r.json();
      if (d.ok) { pending = []; renderAttach(); renderMessages(d.messages); loadConvs(); }
    };
    box.appendChild(el);
  });
}

function renderMessages(msgs) {
  chat.innerHTML = '';
  if (!msgs || !msgs.length) {
    chat.innerHTML = '<div id="welcome"><h2>HI</h2>'
      + '<div>我是跑在这台电脑上的 agent。要我干什么？</div></div>';
    return;
  }
  msgs.forEach(m => {
    if (m.role === 'tool') {
      const b = addRow('tool');
      b.innerHTML = '<b>⟩ ' + esc(m.name) + '</b>' + esc(m.text).slice(0, 4000);
    } else {
      fillBubble(addRow(m.role === 'user' ? 'user' : 'ai'), m.text, m.images);
    }
  });
  scrollBottom();
}

$('#newBtn').onclick = async () => {
  await fetch(api('/api/new'), {method: 'POST'});
  pending = [];
  renderAttach();
  renderMessages([]);
  loadConvs();
};

// ---------------- 技能 ----------------
const skEl = $('#skillPanel');

async function loadSkills() {
  let d;
  try {
    d = await (await fetch(api('/api/skills'))).json();
  } catch (e) {
    return;
  }
  $('#skMaster').checked = !!d.enabled;
  $('#skFoot').textContent = d.dir || '';
  const box = $('#skList');
  box.innerHTML = '';

  if (!d.items || !d.items.length) {
    const p = document.createElement('p');
    p.style.cssText = 'font-size:12px;color:#9aa1ab;padding:6px;margin:0';
    p.textContent = '还没有技能。在 skills/ 里建一个文件夹，放 SKILL.md。';
    box.appendChild(p);
    return;
  }

  for (const s of d.items) {
    const card = document.createElement('div');
    card.className = 'sk';

    const h = document.createElement('div');
    h.className = 'h';
    const b = document.createElement('b');
    b.textContent = s.name || s.id;
    const i = document.createElement('i');
    i.textContent = s.id;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!s.enabled;
    cb.onchange = async () => {
      await fetch(api('/api/skills/toggle'), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: s.id, enabled: cb.checked})
      });
    };
    h.append(b, i, cb);

    const p = document.createElement('p');
    p.textContent = s.desc || '（没写 description，模型看不出它干什么）';
    card.append(h, p);

    if (s.files && s.files.length) {
      const sm = document.createElement('small');
      sm.textContent = s.files.join(' · ');
      card.appendChild(sm);
    }
    box.appendChild(card);
  }
}

$('#skillBtn').onclick = async (e) => {
  e.stopPropagation();
  if (skEl.classList.toggle('on')) await loadSkills();
};

document.addEventListener('click', (e) => {
  if (!skEl.classList.contains('on')) return;
  if (skEl.contains(e.target) || e.target.id === 'skillBtn') return;
  skEl.classList.remove('on');
});

$('#skMaster').onchange = async (e) => {
  await fetch(api('/api/skills/toggle'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({master: e.target.checked})
  });
};

// ---------------- 请求参数面板 ----------------
const setEl = $('#setPanel');
const tSlider = $('#tSlider');
const tVal = $('#tVal');

function showTemp(v) {
  tVal.textContent = Number(v).toFixed(2);
}

tSlider.oninput = () => showTemp(tSlider.value);
document.querySelectorAll('#tPresets button').forEach(b => {
  b.onclick = () => { tSlider.value = b.dataset.t; showTemp(b.dataset.t); };
});

async function loadSettings() {
  let d;
  try { d = await (await fetch(api('/api/settings'))).json(); } catch (e) { return; }
  tSlider.value = d.temperature;
  showTemp(d.temperature);
  $('#setThink').checked = !!d.show_reasoning;
  const extra = d.extra_params || {};
  $('#setExtra').value = Object.keys(extra).length
    ? JSON.stringify(extra, null, 2) : '';
  $('#setModel').textContent = d.model || '';
  $('#model').textContent = d.model || '';

  const main = d.main_api || {}, vis = d.vision_api || {}, mem = d.memory_api || {};
  $('#setMainUrl').value = main.base_url || '';
  $('#setMainModel').value = main.model || '';
  $('#setMainKey').value = '';
  $('#setMainKey').placeholder = main.has_key
    ? '已设置 ' + main.key_hint + '（留空不改）' : 'API Key（未设置）';
  $('#setVisUrl').value = vis.base_url || '';
  $('#setVisModel').value = vis.model || '';
  $('#setVisKey').value = '';
  $('#setVisKey').placeholder = vis.has_key
    ? '已设置 ' + vis.key_hint + '（留空不改）' : 'API Key（留空则用主 API）';
  $('#setMemUrl').value = mem.base_url || '';
  $('#setMemModel').value = mem.model || '';
  $('#setMemKey').value = '';
  $('#setMemKey').placeholder = mem.has_key
    ? '已设置 ' + mem.key_hint + '（留空不改）' : 'API Key（留空则用主模型）';

  $('#setUser').value = d.user_name || '';
  $('#setMode').value = d.agent_mode || 'both';
  $('#setSteps').value = d.max_steps;
  $('#setMemEvery').value = d.memory_every;
  $('#setSandbox').value = d.sandbox_level || 'dir';
  $('#setSandboxNet').checked = !!d.sandbox_network;
  $('#setScreen').checked = !!d.screen_control;
  $('#setPassthrough').checked = !!d.vision_passthrough;
  $('#setWebSearch').checked = !!d.web_search;
  $('#setSkills').checked = !!d.skills_enabled;
  $('#setRemote').checked = !!d.web_remote;
  $('#setToken').value = '';
  $('#setToken').placeholder = d.token_set
    ? '已设置 ' + d.token_hint + '（留空不改）' : '访问令牌（开了远程必须设）';

  // 识图/记忆有内容时自动展开
  if (vis.base_url || vis.model) $('[data-api="vis"]').open = true;
  if (mem.base_url || mem.model) $('[data-api="mem"]').open = true;
}

function setMsg(text, bad) {
  const m = $('#setMsg');
  m.textContent = text;
  m.classList.toggle('bad', !!bad);
}

$('#setBtn').onclick = async (e) => {
  e.stopPropagation();
  if (setEl.classList.toggle('on')) {
    setMsg('');
    await loadSettings();
  }
};

document.addEventListener('click', (e) => {
  if (!setEl.classList.contains('on')) return;
  if (setEl.contains(e.target) || e.target.id === 'setBtn') return;
  setEl.classList.remove('on');
});

$('#setSave').onclick = async () => {
  const raw = $('#setExtra').value.trim();
  let extra = {};
  if (raw) {
    try { extra = JSON.parse(raw); }
    catch (err) { setMsg('不是合法 JSON：' + err.message, true); return; }
    if (typeof extra !== 'object' || Array.isArray(extra) || extra === null) {
      setMsg('自定义参数必须是 JSON 对象，比如 {"top_p": 0.9}', true);
      return;
    }
  }
  const num = (sel, dflt) => {
    const v = parseInt($(sel).value, 10);
    return Number.isFinite(v) ? v : dflt;
  };
  const body = {
    temperature: parseFloat(tSlider.value),
    show_reasoning: $('#setThink').checked,
    extra_params: extra,
    user_name: $('#setUser').value.trim() || '用户',
    agent_mode: $('#setMode').value,
    max_steps: num('#setSteps', 12),
    memory_every: num('#setMemEvery', 6),
    sandbox_level: $('#setSandbox').value,
    sandbox_network: $('#setSandboxNet').checked,
    screen_control: $('#setScreen').checked,
    vision_passthrough: $('#setPassthrough').checked,
    web_search: $('#setWebSearch').checked,
    skills_enabled: $('#setSkills').checked,
    web_remote: $('#setRemote').checked,
    main_api: {
      base_url: $('#setMainUrl').value.trim(),
      api_key: $('#setMainKey').value.trim(),
      model: $('#setMainModel').value.trim()
    },
    vision_api: {
      base_url: $('#setVisUrl').value.trim(),
      api_key: $('#setVisKey').value.trim(),
      model: $('#setVisModel').value.trim()
    },
    memory_api: {
      base_url: $('#setMemUrl').value.trim(),
      api_key: $('#setMemKey').value.trim(),
      model: $('#setMemModel').value.trim()
    }
  };
  const tok = $('#setToken').value.trim();
  if (tok) body.web_token = tok;

  setMsg('保存中…');
  const r = await fetch(api('/api/settings'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const d = await r.json();
  if (!d.ok) { setMsg(d.err || '保存失败', true); return; }
  showTemp(d.temperature);
  await loadSettings();
  setMsg('已保存 · 下一条消息生效（远程访问需重启）');
};

// ---------------- 定时任务面板 ----------------
const tkEl = $('#taskPanel');

function fmtTask(t) {
  if (t.kind === 'daily') return '每天 ' + (t.time || '??:??');
  return '每 ' + (t.interval_min || 60) + ' 分钟';
}

async function loadTasks() {
  let d;
  try { d = await (await fetch(api('/api/schedule'))).json(); } catch (e) { return; }
  const box = $('#tkList');
  box.innerHTML = '';
  if (!(d.items || []).length) {
    box.innerHTML = '<p style="font-size:12px;color:#9aa1ab;padding:6px;margin:0">'
      + '还没有定时任务。在下面填一个，或者直接跟 agent 说「每天 8 点半帮我做 XX」。</p>';
    return;
  }
  d.items.forEach(t => {
    const card = document.createElement('div');
    card.className = 'sk';
    const h = document.createElement('div');
    h.className = 'h';
    const b = document.createElement('b');
    b.textContent = t.name || t.id;
    const i = document.createElement('i');
    i.textContent = fmtTask(t);
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!t.enabled;
    cb.onchange = async () => {
      await fetch(api('/api/schedule'), {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({op: 'toggle', id: t.id, enabled: cb.checked})});
    };
    const del = document.createElement('button');
    del.className = 'del';
    del.textContent = '删除';
    del.onclick = async () => {
      await fetch(api('/api/schedule'), {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({op: 'remove', id: t.id})});
      loadTasks();
    };
    h.append(b, i, cb, del);
    const p = document.createElement('p');
    p.textContent = t.prompt || '';
    card.append(h, p);
    box.appendChild(card);
  });
}

$('#tkKind').onchange = (e) => {
  const daily = e.target.value === 'daily';
  $('#tkTime').style.display = daily ? '' : 'none';
  $('#tkEvery').style.display = daily ? 'none' : '';
};

$('#tkAdd').onclick = async () => {
  const kind = $('#tkKind').value;
  const task = {
    id: 'web_' + Date.now().toString(36),
    name: $('#tkName').value.trim() || '未命名任务',
    prompt: $('#tkPrompt').value.trim(),
    kind: kind,
    enabled: true
  };
  if (kind === 'daily') task.time = $('#tkTime').value.trim() || '08:30';
  else task.interval_min = parseInt($('#tkEvery').value) || 60;
  if (!task.prompt) { alert('任务内容不能为空'); return; }
  const r = await fetch(api('/api/schedule'), {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: 'add', task})});
  const d = await r.json();
  if (!d.ok) { alert(d.err || '添加失败'); return; }
  $('#tkName').value = ''; $('#tkPrompt').value = '';
  loadTasks();
};

$('#taskBtn').onclick = async (e) => {
  e.stopPropagation();
  statEl.classList.remove('on');
  if (tkEl.classList.toggle('on')) await loadTasks();
};

// ---------------- 用量统计面板 ----------------
const statEl = $('#statPanel');

async function loadStats() {
  let d;
  try { d = await (await fetch(api('/api/usage'))).json(); } catch (e) { return; }
  const box = $('#stList');
  box.innerHTML = '';
  const big = document.createElement('div');
  big.className = 'stBig';
  big.innerHTML = '<b>' + (d.today ? d.today.tokens.toLocaleString() : 0)
    + '</b><p>今天估算 token · ' + (d.today ? d.today.requests : 0) + ' 次请求</p>';
  box.appendChild(big);

  const days = d.by_day || [];
  const maxTk = Math.max(1, ...days.map(x => x.tokens));
  const s1 = document.createElement('div');
  s1.className = 'stSect';
  s1.textContent = '最近 7 天';
  box.appendChild(s1);
  days.slice(-7).forEach(x => {
    const row = document.createElement('div');
    row.className = 'stRow';
    const bar = document.createElement('span');
    bar.className = 'bar';
    bar.style.width = Math.max(2, Math.round(x.tokens / maxTk * 110)) + 'px';
    row.append(bar,
      Object.assign(document.createElement('span'),
        {textContent: x.day.slice(5), style: 'color:#888780'}),
      Object.assign(document.createElement('span'),
        {textContent: x.tokens.toLocaleString() + ' tok · ' + x.requests + ' 次',
         style: 'margin-left:auto;color:#444441'}));
    box.appendChild(row);
  });

  const s2 = document.createElement('div');
  s2.className = 'stSect';
  s2.textContent = '按模型（7 天内）';
  box.appendChild(s2);
  (d.by_model || []).forEach(x => {
    const row = document.createElement('div');
    row.className = 'stRow';
    row.append(Object.assign(document.createElement('span'),
      {textContent: x.model, style: 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px'}),
      Object.assign(document.createElement('span'),
        {textContent: x.tokens.toLocaleString() + ' tok',
         style: 'margin-left:auto;color:#444441'}));
    box.appendChild(row);
  });

  const note = document.createElement('p');
  note.style.cssText = 'font-size:10.5px;color:#9aa1ab;padding:8px 12px;margin:0';
  note.textContent = d.note || '';
  box.appendChild(note);
}

$('#statBtn').onclick = async (e) => {
  e.stopPropagation();
  tkEl.classList.remove('on');
  if (statEl.classList.toggle('on')) await loadStats();
};

document.addEventListener('click', (e) => {
  if (tkEl.classList.contains('on') && !tkEl.contains(e.target)
      && e.target.id !== 'taskBtn') tkEl.classList.remove('on');
  if (statEl.classList.contains('on') && !statEl.contains(e.target)
      && e.target.id !== 'statBtn') statEl.classList.remove('on');
});

// ---------------- 确认框 ----------------
async function answer(ok) {
  $('#overlay').classList.remove('on');
  await fetch(api('/api/confirm'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ok})
  });
}
$('#yes').onclick = () => answer(true);
$('#no').onclick = () => answer(false);

// ---------------- SSE ----------------
function connect() {
  const es = new EventSource(api('/api/stream'));
  es.onopen = () => { $('#dot').classList.add('live'); };
  es.onerror = () => {
    $('#dot').classList.remove('live');
    $('#status').textContent = '断线，重连中…';
  };
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    const t = d.type;

    if (t === 'thinking') {
      thinkStart();
      return;
    }
    if (t === 'reasoning') {
      thinkAdd(d.text || '');
      return;
    }
    if (t === 'stream') {
      thinkDone();
      if (!curAi) curAi = addRow('ai');
      curAi.textContent = d.text || '…';
      scrollBottom();
      return;
    }
    if (t === 'assistant') {
      thinkDone();
      if (d.text) {
        if (!curAi) curAi = addRow('ai');
        curAi.textContent = d.text;
      } else if (curAi) {
        curAi.parentElement.remove();
      }
      curAi = null;
      scrollBottom();
      return;
    }
    if (t === 'tool_start') {
      thinkDone();
      curAi = null;
      const b = addRow('tool');
      b.innerHTML = '<b>⟩ ' + esc(d.name) + '  ' + esc(d.detail) + '</b>执行中…';
      curTool = b;
      scrollBottom();
      return;
    }
    if (t === 'tool_result') {
      if (curTool) {
        let txt = d.text || '（无输出）';
        if (txt.length > 4000) txt = txt.slice(0, 4000) + '\n…（已截断）';
        curTool.innerHTML = '<b>⟩ ' + esc(d.name) + '</b>' + esc(txt);
        curTool = null;
      }
      scrollBottom();
      return;
    }
    if (t === 'ask') {
      if (d.text) addRow('ai').textContent = d.text;
      return;
    }
    if (t === 'error') {
      thinkDone();
      curAi = null;
      toast(d.msg || '未知错误');
      return;
    }
    if (t === 'cancel') {
      thinkDone();
      curAi = null;
      const b = addRow('ai');
      b.style.color = '#9aa1ab';
      b.textContent = '（已停止）';
      return;
    }
    if (t === 'confirm') {
      $('#dlgCmd').textContent = d.cmd || '';
      $('#dlgWhy').textContent = d.reason || '';
      $('#overlay').classList.add('on');
      return;
    }
    if (t === 'done' || t === 'idle') {
      thinkDone();
      setBusy(false);
      curAi = null;
      curTool = null;
      loadConvs();
      return;
    }
    if (t === 'mem_ok' || t === 'mem_fail') return;
  };
}

// ---------------- 启动 ----------------
(async () => {
  try {
    const m = await fetch(api('/api/messages'));
    const d = await m.json();
    renderMessages(d.messages);
  } catch (e) {}
  loadConvs();
  loadSettings();
  connect();
})();
</script>
</body>
</html>
"""
