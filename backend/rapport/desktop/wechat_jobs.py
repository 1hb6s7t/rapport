"""Bounded, cancellable in-memory desktop jobs, scoped to browser and relationship."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from rapport.errors import RapportError


@dataclass
class HistoryJob:
    id: str
    owner: str
    relationship_id: str
    target: int
    status: str = "running"
    progress: dict = field(default_factory=dict)
    result: dict | None = None
    error: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    touched: float = field(default_factory=time.monotonic)


class HistoryJobs:
    def __init__(self):
        self.items = {}
        self.lock = threading.RLock()

    def start(self, owner, rid, target, work):
        with self.lock:
            if any(j.status == "running" for j in self.items.values()):
                raise RapportError("wechat_busy", "已有历史读取任务正在进行，请先停止或等待完成。", status=409)
            self.items = {k: j for k, j in self.items.items() if time.monotonic() - j.touched < 1800}
            while len(self.items) >= 4:
                self.items.pop(min(self.items, key=lambda k: self.items[k].touched))
            job = HistoryJob(secrets.token_urlsafe(24), owner, rid, target)
            self.items[job.id] = job

        def report(**progress):
            with self.lock:
                job.progress = progress

        def run():
            try:
                result = work(job.cancel, report)
                with self.lock:
                    if self.items.get(job.id) is job:
                        job.result = result
                        job.status = "completed" if result else "cancelled"
            except RapportError as error:
                with self.lock:
                    job.error, job.status = error.as_detail(), "failed"
            except Exception:
                with self.lock:
                    job.error = {"code": "wechat_history_failed", "msg": "历史读取中断，请重试。", "hint": "未自动导入或发送任何消息。"}
                    job.status = "failed"
            finally:
                job.touched = time.monotonic()

        threading.Thread(target=run, name="wechat-history", daemon=True).start()
        return job

    def get(self, key, owner):
        with self.lock:
            job = self.items.get(key)
            if not job or (job.status != "running" and time.monotonic() - job.touched > 1800):
                self.items.pop(key, None)
                raise RapportError("wechat_job_expired", "读取任务已失效，请重新开始。", status=404)
            if job.owner != owner:
                raise RapportError("wechat_job_scope", "该读取任务不属于当前浏览器会话。", status=403)
            return job

    def clear(self, rid=None):
        with self.lock:
            for job in self.items.values():
                if rid is None or job.relationship_id == rid:
                    job.cancel.set()
            self.items = {k: j for k, j in self.items.items() if rid is not None and j.relationship_id != rid}
