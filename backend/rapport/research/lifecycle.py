"""Research cancellation and generation fencing, including full-data erasure.

Control records deliberately live outside the private research tree so erasure cannot
remove the fence while an old process is still exiting. They contain IDs/status only.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from rapport.errors import RapportError
from rapport.learning.service import _alive


class ResearchCancelled(RuntimeError):
    pass


@contextmanager
def control(data_dir):
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "research-control.db", timeout=30)
    try:
        db.executescript("CREATE TABLE IF NOT EXISTS generation (id INTEGER PRIMARY KEY, value INTEGER NOT NULL);"
                         "INSERT OR IGNORE INTO generation VALUES(1,1);"
                         "CREATE TABLE IF NOT EXISTS flags(id TEXT PRIMARY KEY,value INTEGER);"
                         "INSERT OR IGNORE INTO flags VALUES('wiping',0);"
                         "CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,pid INTEGER,generation INTEGER,status TEXT,heartbeat REAL);")
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class ResearchRun:
    def __init__(self, data_dir, ident, dataset_id):
        self.data_dir = Path(data_dir).resolve()
        self.ident = ident
        self.dataset_id = dataset_id
        with control(self.data_dir) as db:
            if db.execute("SELECT value FROM flags WHERE id='wiping'").fetchone()[0]:
                raise RapportError("research_stopping", "正在清空研究资料，暂不能开始新研究任务", status=409)
            self.generation = db.execute("SELECT value FROM generation WHERE id=1").fetchone()[0]
            db.execute("INSERT OR REPLACE INTO workers VALUES(?,?,?,?,?)", (ident, os.getpid(), self.generation, "running", time.time()))

    def check(self):
        with control(self.data_dir) as db:
            current = db.execute("SELECT value FROM generation WHERE id=1").fetchone()[0]
            worker = db.execute("SELECT status FROM workers WHERE id=?", (self.ident,)).fetchone()
            if current != self.generation or not worker or worker[0] != "running":
                raise ResearchCancelled("研究任务已取消或资料正在清除")
            if self.dataset_id is not None and not (self.data_dir / "datasets" / self.dataset_id / "manifest.json").is_file():
                raise ResearchCancelled("研究来源快照已删除")
            db.execute("UPDATE workers SET heartbeat=? WHERE id=?", (time.time(), self.ident))

    def finish(self, status):
        with control(self.data_dir) as db:
            db.execute("UPDATE workers SET status=?,heartbeat=? WHERE id=?", (status, time.time(), self.ident))


def safe_wipe(data_dir):
    """Fence writers, refuse until live workers acknowledge, then remove private artifacts.

    Caller may retry a 409 once workers have stopped. No unrelated processes are killed.
    """
    base = Path(data_dir).resolve()
    root = base / "research"
    if root.is_symlink() or root.is_junction() or not root.resolve().is_relative_to(base) or root.resolve() == base:
        raise RapportError("research_path_unsafe", "研究目录路径不符合清理条件", status=409)
    with control(base) as db:
        db.execute("UPDATE generation SET value=value+1 WHERE id=1")
        db.execute("UPDATE flags SET value=1 WHERE id='wiping'")
        workers = db.execute("SELECT id,pid,status FROM workers WHERE status IN ('running','cancelling')").fetchall()
        live = [(ident, pid) for ident, pid, _ in workers if _alive(pid)]
        db.execute("UPDATE workers SET status='cancelling' WHERE status='running'")
    if live:
        raise RapportError("research_stopping", "研究任务正在停止，请稍后再次清空资料", status=409)
    with control(base) as db:
        # Hold the writer lock through erase: new workers cannot interleave registration.
        if root.exists():
            for path in root.rglob("*"):
                if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root.resolve()):
                    raise RapportError("research_path_unsafe", "研究目录含外部链接，未执行清理", status=409)
            shutil.rmtree(root)
        db.execute("UPDATE workers SET status='erased' WHERE status IN ('running','cancelling')")
        db.execute("UPDATE flags SET value=0 WHERE id='wiping'")
    return {"erased": True}


def reconcile_reports(data_dir):
    """Mark dead registered workers interrupted; never pretend abandoned work completed."""
    root = Path(data_dir).resolve() / "research" / "runs"
    with control(data_dir) as db:
        workers = db.execute("SELECT id,pid,status FROM workers WHERE status IN ('running','cancelling')").fetchall()
        for ident, pid, _ in workers:
            if _alive(pid):
                continue
            db.execute("UPDATE workers SET status='interrupted' WHERE id=?", (ident,))
            report_path = root / ident / "report.json"
            if report_path.is_file():
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if report.get("status") == "running":
                    report["status"] = "interrupted"
                    report["error_type"] = "WorkerExited"
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
