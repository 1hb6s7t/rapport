"""Serial desktop worker adapted from DSH SenderQueue (MIT, Copyright 2025).

Source: dsh-wechat-bot 42d672ea8aa49925637973fe11194f768bc92af6,
infra/wechat/gui.py; see LICENSE-dsh-wechat-bot.txt.
Changes: returns the actual operation result; never truncates, retries or sends
on its own. Entire operations (including receipt checks) stay on one thread.
"""
import queue
import sys
import threading
from concurrent.futures import Future
from contextlib import contextmanager

from rapport.errors import RapportError


@contextmanager
def desktop_context():
    if sys.platform != "win32":
        yield
        return
    from rapport.desktop.wechat_native import automation

    with automation():
        yield


class DesktopWorker:
    def __init__(self, context_factory=desktop_context, max_pending=4):
        self.context_factory = context_factory
        self.tasks = queue.Queue(maxsize=max_pending)
        self.thread = None
        self.lock = threading.Lock()
        self.stopped = False

    def run(self, function, *args, **kwargs):
        if threading.current_thread() is self.thread:
            return function(*args, **kwargs)
        future = Future()
        with self.lock:
            if self.stopped:
                raise RapportError("desktop_stopped", "桌面服务正在停止，请稍后重试。", status=409)
            if self.thread is None:
                self.thread = threading.Thread(target=self._worker, name="rapport-dsh-desktop", daemon=True)
                self.thread.start()
            try:
                self.tasks.put_nowait((future, function, args, kwargs))
            except queue.Full as exc:
                raise RapportError("wechat_busy", "桌面操作队列已满，请等待当前操作完成。", status=409) from exc
        return future.result()

    def _worker(self):
        try:
            with self.context_factory():
                while True:
                    item = self.tasks.get()
                    if item is None:
                        return
                    future, function, args, kwargs = item
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        future.set_result(function(*args, **kwargs))
                    except BaseException as exc:
                        future.set_exception(exc)
        except BaseException as exc:
            with self.lock:
                self.stopped = True
                while not self.tasks.empty():
                    item = self.tasks.get_nowait()
                    if item is not None:
                        item[0].set_exception(exc)

    def close(self):
        with self.lock:
            self.stopped = True
            while not self.tasks.empty():
                item = self.tasks.get_nowait()
                if item is not None:
                    item[0].set_exception(RapportError("desktop_stopped", "桌面操作尚未执行，服务已停止。", status=409))
            self.tasks.put_nowait(None)
        if self.thread and threading.current_thread() is not self.thread:
            self.thread.join(timeout=5)
