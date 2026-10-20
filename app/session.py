# -*- coding: utf-8 -*-
"""四个前端（GUI / CLI / TUI / Web）共用的会话服务。

前端只干两件事：
  1. 把用户输入交给 send()
  2. 把 poll() 吐出来的事件画到屏幕上

线程管理、取消、高危命令确认、记忆总结都在这里，别在前端里重写一遍。

事件类型（和 AgentRunner.emit 一致）：
    step / thinking / delta / assistant_end / tool_start / tool_result
    ask / done / cancel / error
本层额外加的：
    confirm  高危命令要用户点头，字段 cmd / reason
    mem_ok / mem_fail   记忆总结的结果
    _idle    这一轮跑完了（前端据此解除"运行中"状态）
"""
from __future__ import annotations

import queue
import threading

from . import agent_core, config, memory


class Session:
    def __init__(self, cfg: dict | None = None):
        self.cfg = dict(cfg or config.load_config())
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False

        self._thread: threading.Thread | None = None
        self._rounds = 0
        self._mem_busy = False
        self._confirm: tuple[threading.Event, dict] | None = None

        self.conv = self._pick_or_new()

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------
    def _pick_or_new(self) -> agent_core.Conversation:
        convs = agent_core.list_conversations()
        return convs[0] if convs else agent_core.Conversation()

    def conversations(self) -> list:
        return agent_core.list_conversations()

    def new(self) -> agent_core.Conversation:
        self.stop()
        self.conv = agent_core.Conversation()
        self._rounds = 0
        return self.conv

    def switch(self, conv) -> None:
        if not conv:
            return
        self.stop()
        self.conv = conv
        self._rounds = 0

    def switch_by_index(self, idx: int) -> bool:
        convs = self.conversations()
        if not (0 <= idx < len(convs)):
            return False
        self.switch(convs[idx])
        return True

    def delete(self, conv) -> None:
        conv.delete()
        if self.conv and self.conv.id == conv.id:
            self.conv = self._pick_or_new()

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send(self, text: str, images: list[str] | None = None) -> tuple[bool, str]:
        text = (text or "").strip()
        images = [p for p in (images or []) if p]
        if not text and not images:
            return False, "空输入"
        if self.busy:
            return False, "正在执行，先停下这一轮"
        if not config.is_configured(self.cfg):
            return False, "没有配置主 API Key，先跑 first.bat 或改 data/config.json"

        conv = self.conv
        cid = conv.id
        runner = agent_core.AgentRunner(dict(self.cfg), conv)
        self.cancel = threading.Event()
        cancel = self.cancel
        self.busy = True

        def emit(evt: dict) -> None:
            evt = dict(evt)
            evt["_cid"] = cid
            self.q.put(evt)

        def work() -> None:
            try:
                runner.run(text, emit, cancel, self._ask_confirm, images=images)
            except Exception as e:                      # 兜底，别让线程静默死掉
                self.q.put({"type": "error", "_cid": cid,
                            "msg": f"内部错误 {type(e).__name__}: {e}"})
            finally:
                self.busy = False
                self.q.put({"type": "_idle", "_cid": cid})
                self._maybe_memory()

        # 先把用户消息落到对话里，前端的"运行中"才不会抢在 add_user 之前
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        return True, ""

    def stop(self) -> None:
        if self.busy:
            self.cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        """等这一轮彻底结束。CLI 这类同步前端用。"""
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def poll(self, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        for _ in range(limit):
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out

    # ------------------------------------------------------------------
    # 高危命令确认（跑在工作线程里，必须阻塞）
    # ------------------------------------------------------------------
    def _ask_confirm(self, cmd: str, reason: str) -> bool:
        ev = threading.Event()
        box = {"ok": False}
        self._confirm = (ev, box)
        self.q.put({"type": "confirm", "cmd": cmd, "reason": reason,
                    "_cid": self.conv.id})
        ev.wait(timeout=300)
        self._confirm = None
        return bool(box.get("ok"))

    def answer_confirm(self, ok: bool) -> None:
        cur = self._confirm
        if cur:
            ev, box = cur
            box["ok"] = bool(ok)
            ev.set()

    # ------------------------------------------------------------------
    # 记忆总结
    # ------------------------------------------------------------------
    def count_round(self, n: int = 1) -> None:
        self._rounds += n

    def _maybe_memory(self) -> None:
        if self._mem_busy:
            return
        every = max(1, int(self.cfg.get("memory_every", 6)))
        if self._rounds < every:
            return
        if not self.conv or self.conv.user_turns() < 1:
            return

        self._rounds = 0
        self._mem_busy = True
        cfg = dict(self.cfg)
        msgs = list(self.conv.messages)

        def work() -> None:
            try:
                memory.summarize(cfg, msgs)
                self.q.put({"type": "mem_ok"})
            except Exception as e:
                self.q.put({"type": "mem_fail", "msg": str(e)})
            finally:
                self._mem_busy = False

        threading.Thread(target=work, daemon=True).start()
