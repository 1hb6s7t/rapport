"""Serialized model residency with bounded idle time and explicit GPU handoff."""
import threading
import time
from contextlib import contextmanager


class ResidentBusy(Exception):
    pass


class ResidentResource:
    def __init__(self, load, dispose, *, idle_seconds=300, clock=time.monotonic):
        self.load, self.dispose = load, dispose
        self.idle_seconds, self.clock = idle_seconds, clock
        self.lock = threading.Lock()
        self.value = None
        self.last_used = 0.0

    @contextmanager
    def use(self):
        if not self.lock.acquire(blocking=False):
            raise ResidentBusy()
        try:
            reused = self.value is not None
            if not reused:
                self.value = self.load()
            yield self.value, reused
        finally:
            self.last_used = self.clock()
            try:
                if self.idle_seconds == 0:
                    self._dispose()
            finally:
                self.lock.release()

    def _dispose(self):
        value, self.value = self.value, None
        if value is not None:
            self.dispose(value)

    def release(self, *, only_if_idle=False):
        if not self.lock.acquire(blocking=False):
            raise ResidentBusy()
        try:
            if only_if_idle and self.clock() - self.last_used < self.idle_seconds:
                return False
            existed = self.value is not None
            self._dispose()
            return existed
        finally:
            self.lock.release()

    def info(self):
        return {"resident": self.value is not None, "busy": self.lock.locked(),
                "idle_timeout_s": self.idle_seconds}
