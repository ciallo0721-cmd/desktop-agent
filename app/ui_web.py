# -*- coding: utf-8 -*-
"""Web 模式：浏览器访问 http://127.0.0.1:16800

只用标准库的 http.server，不依赖 Flask —— 少一个要装的包。
SSE 长连接，流式表现跟 GUI 一致；只监听本机回环，外人扫不到。
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import agent_core, config, images
from .session import Session

HOST = "127.0.0.1"
PORT = 16800


def _sse(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


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

        # ---------------- GET ----------------
        def do_GET(self):
            path = self.path.split("?")[0]

            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
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


def run(cfg: dict | None = None) -> int:
    sess = Session(cfg)
    hub = SessionHub(sess)

    conf = sess.cfg
    model = (conf.get("main_api") or {}).get("model") or "未配置"
    print("=" * 46)
    print("  Desktop Agent · Web")
    print(f"  http://{HOST}:{PORT}")
    print(f"  模型 {model}  ·  方式 {conf.get('agent_mode', 'both')}")
    print("  Ctrl+C 关闭服务")
    print("=" * 46)

    if not config.is_configured(conf):
        print("！没有配置主 API Key，发消息会失败。先跑 first.bat。")

    try:
        httpd = ThreadingHTTPServer((HOST, PORT), make_handler(hub, sess))
    except OSError as e:
        print(f"端口 {PORT} 起不来：{e}")
        print("可能已经开着一个了，先关掉再试。")
        return 1

    httpd.daemon_threads = True
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
<title>Desktop Agent</title>
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

function setBusy(v) {
  busy = v;
  const btn = $('#send');
  btn.textContent = v ? '停止' : '发送';
  btn.classList.toggle('stop', v);
  $('#status').textContent = v ? '执行中…' : '空闲';
}

// ---------------- 图片 ----------------
function imgUrl(p) { return '/img?p=' + encodeURIComponent(p); }

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
    const r = await fetch('/api/upload', {
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
  if (busy) { await fetch('/api/stop', {method: 'POST'}); return; }
  const box = $('#input');
  const text = box.value.trim();
  if (!text && !pending.length) return;
  const imgs = pending.slice();
  const r = await fetch('/api/send', {
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
  const r = await fetch('/api/conversations');
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
      const r = await fetch('/api/switch', {
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
  await fetch('/api/new', {method: 'POST'});
  pending = [];
  renderAttach();
  renderMessages([]);
  loadConvs();
};

// ---------------- 确认框 ----------------
async function answer(ok) {
  $('#overlay').classList.remove('on');
  await fetch('/api/confirm', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ok})
  });
}
$('#yes').onclick = () => answer(true);
$('#no').onclick = () => answer(false);

// ---------------- SSE ----------------
function connect() {
  const es = new EventSource('/api/stream');
  es.onopen = () => { $('#dot').classList.add('live'); };
  es.onerror = () => {
    $('#dot').classList.remove('live');
    $('#status').textContent = '断线，重连中…';
  };
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    const t = d.type;

    if (t === 'thinking') {
      if (!curAi) { curAi = addRow('ai'); curAi.textContent = '正在思考…'; }
      return;
    }
    if (t === 'stream') {
      if (!curAi) curAi = addRow('ai');
      curAi.textContent = d.text || '…';
      scrollBottom();
      return;
    }
    if (t === 'assistant') {
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
      curAi = null;
      toast(d.msg || '未知错误');
      return;
    }
    if (t === 'cancel') {
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
    const m = await fetch('/api/messages');
    const d = await m.json();
    renderMessages(d.messages);
  } catch (e) {}
  loadConvs();
  connect();
})();
</script>
</body>
</html>
"""
