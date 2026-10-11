"""Cross-process GPU exclusion. OS releases the lock if a worker crashes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class GPUBusy(RuntimeError):
    pass


class GPULease:
    def __init__(self, data_dir: Path, owner: str = "learning"):
        self.root = Path(data_dir) / "learning"
        self.owner = owner
        self._file = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        f = (self.root / "gpu.lock").open("a+b")
        try:
            f.seek(0, 2)
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            raise GPUBusy("GPU 正被另一个本地任务使用，请稍后重试") from e
        self._file = f
        (self.root / "gpu-owner.json").write_text(
            json.dumps({"owner": self.owner, "pid": os.getpid(), "since": time.time()}), encoding="utf-8"
        )
        return self

    def __exit__(self, *_):
        if self._file:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
