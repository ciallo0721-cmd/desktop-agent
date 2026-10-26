# -*- coding: utf-8 -*-
"""定时任务：到点自动把一段 prompt 发给 agent 跑。

任务存在 data/schedule.json（改完保存即生效，不用重启）：

[
  {
    "id": "morning-news",            // 唯一 id
    "name": "早报",                   // 显示名
    "prompt": "联网搜今天的科技新闻，整理成 5 条摘要",   // 到点发给 agent 的话
    "kind": "daily",                 // "daily" = 每天定点 | "interval" = 间隔循环
    "time": "08:30",                 // daily 用：每天几点（24h 制）
    "interval_min": 120,             // interval 用：每多少分钟一次
    "enabled": true
  }
]

执行结果追加写到 data/schedule_log.jsonl，一行一条：
{"time": "...", "task": "morning-news", "ok": true, "summary": "...", "elapsed": 95.2}

安全约定：定时任务跑在无人值守环境，高危命令没有弹窗确认，
会按「无交互环境」直接拒绝 —— 想跑高危命令请人在旁边盯着。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime

from . import config

CHECK_INTERVAL = 20          # 每 20s 查一次任务表
RUN_TIMEOUT = 900            # 单个任务最多跑 15 分钟

_thread: threading.Thread | None = None
_busy_lock = threading.Lock()


def schedule_path():
    return config.DATA_DIR / "schedule.json"


def log_path():
    return config.DATA_DIR / "schedule_log.jsonl"


def load_tasks() -> list[dict]:
    try:
        raw = json.loads(schedule_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _save_tasks(tasks: list[dict]) -> None:
    schedule_path().write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# 任务管理 API（agent 的 schedule 工具和 Web 管理页共用）
# --------------------------------------------------------------------------

def _clean_task(t: dict) -> tuple[bool, str | dict]:
    """校验 + 规范化一个任务。返回 (ok, 规范化后的 dict 或错误信息)。"""
    tid = str(t.get("id") or "").strip() or ("task_" + uuid.uuid4().hex[:6])
    prompt = str(t.get("prompt") or "").strip()
    if not prompt:
        return False, "prompt 不能为空。"
    kind = str(t.get("kind") or "interval").strip().lower()
    if kind not in ("daily", "interval"):
        return False, f"kind 只能是 daily / interval，收到 {kind!r}"
    task = {
        "id": tid,
        "name": str(t.get("name") or tid).strip()[:40],
        "prompt": prompt,
        "kind": kind,
        "enabled": bool(t.get("enabled", True)),
        "last_run": float(t.get("last_run") or 0),
    }
    if kind == "daily":
        tm = str(t.get("time") or "").strip()
        try:
            hh, mm = tm.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
            task["time"] = f"{int(hh):02d}:{int(mm):02d}"
        except (ValueError, TypeError):
            return False, f"daily 任务需要合法的 time（HH:MM），收到 {tm!r}"
    else:
        try:
            task["interval_min"] = max(1, int(t.get("interval_min") or 60))
        except (TypeError, ValueError):
            return False, "interval_min 必须是分钟数。"
    return True, task


def add_task(t: dict) -> tuple[bool, str]:
    ok, task = _clean_task(t)
    if not ok:
        return False, str(task)
    tasks = load_tasks()
    if any(x.get("id") == task["id"] for x in tasks):
        return False, f"id {task['id']!r} 已经存在，换个 id 或先删掉旧的。"
    tasks.append(task)
    _save_tasks(tasks)
    return True, f"已创建定时任务 {task['name']}（id={task['id']}，{task['kind']}）"


def remove_task(tid: str) -> tuple[bool, str]:
    tasks = load_tasks()
    left = [x for x in tasks if x.get("id") != tid]
    if len(left) == len(tasks):
        return False, f"没有 id 为 {tid!r} 的任务。"
    _save_tasks(left)
    return True, f"已删除任务 {tid}。"


def toggle_task(tid: str, enabled: bool) -> tuple[bool, str]:
    tasks = load_tasks()
    for x in tasks:
        if x.get("id") == tid:
            x["enabled"] = bool(enabled)
            _save_tasks(tasks)
            state = "启用" if enabled else "停用"
            return True, f"任务 {tid} 已{state}。"
    return False, f"没有 id 为 {tid!r} 的任务。"


def list_tasks_text() -> str:
    tasks = load_tasks()
    if not tasks:
        return "【定时任务】当前没有任何任务。schedule.json 里可以手写，"
    rows = []
    for t in tasks:
        if t.get("kind") == "daily":
            sched = f"每天 {t.get('time')}"
        else:
            sched = f"每 {t.get('interval_min', 60)} 分钟"
        state = "启用" if t.get("enabled", True) else "停用"
        last = t.get("last_run") or 0
        last_s = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "从未"
        rows.append(f"- {t.get('id')} · {t.get('name')} · {sched} · {state} · 上次 {last_s}\n"
                    f"  prompt: {str(t.get('prompt'))[:80]}")
    return "【定时任务】\n" + "\n".join(rows)


def _log(task: dict, ok: bool, summary: str, elapsed: float) -> None:
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task": task.get("id") or task.get("name") or "?",
        "ok": ok,
        "elapsed": round(elapsed, 1),
        "summary": (summary or "")[:800],
    }
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _due(task: dict, now: datetime, last: float) -> bool:
    if not task.get("enabled", True) or not str(task.get("prompt") or "").strip():
        return False
    kind = str(task.get("kind") or "interval").lower()
    try:
        if kind == "daily":
            hh, mm = str(task.get("time") or "").split(":")
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            return now >= target and last < target.timestamp()
        interval = max(1, int(task.get("interval_min") or 60))
        return now.timestamp() - last >= interval * 60
    except (ValueError, TypeError):
        return False


def _run_task(task: dict) -> None:
    """起一个独立 Session 跑任务，等它结束并写日志。"""
    from .session import Session

    with _busy_lock:
        try:
            sess = Session()
            sess.cfg = dict(config.load_config())
            if not config.is_configured(sess.cfg):
                _log(task, False, "没有配置主 API Key，任务跳过", 0.0)
                return

            ok = sess.send(str(task["prompt"]))
            if not ok:
                _log(task, False, "任务没能启动（agent 正忙或输入被拒）", 0.0)
                return

            start = time.time()
            summary = ""
            while time.time() - start < RUN_TIMEOUT:
                for evt in sess.poll():
                    t = evt.get("type")
                    if t == "assistant_end":
                        summary = evt.get("text", "")
                    elif t == "error":
                        summary = "错误: " + str(evt.get("msg", ""))
                    elif t in ("done", "_idle"):
                        _log(task, True, summary, time.time() - start)
                        try:
                            from . import notify

                            notify.push_task_done(
                                sess.cfg, task.get("name") or task.get("id"),
                                True, summary,
                            )
                        except Exception:
                            pass
                        return
                time.sleep(1.0)
            sess.stop()
            _log(task, False, f"任务超过 {RUN_TIMEOUT}s 未完成，已强制停止", time.time() - start)
            try:
                from . import notify

                notify.push_task_done(
                    sess.cfg, task.get("name") or task.get("id"), False, "超时被强制停止",
                )
            except Exception:
                pass
        except Exception as e:
            _log(task, False, f"调度器异常 {type(e).__name__}: {e}", 0.0)


def _loop() -> None:
    while True:
        try:
            now = datetime.now()
            for task in load_tasks():
                last = float(task.get("last_run") or 0)
                if _due(task, now, last):
                    task["last_run"] = now.timestamp()
                    _save_last_run(task)
                    threading.Thread(
                        target=_run_task, args=(dict(task),), daemon=True
                    ).start()
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL)


def _save_last_run(task: dict) -> None:
    """把 last_run 写回 schedule.json，防止重启后重复触发。"""
    try:
        tasks = load_tasks()
        for t in tasks:
            if t.get("id") == task.get("id"):
                t["last_run"] = task.get("last_run")
        schedule_path().write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def start() -> None:
    """启动调度线程（进程里只起一个）。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not load_tasks():
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
