"""Engine/session factory. SQLite with WAL; short transactions only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from rapport.db.migrate import ensure_schema


def make_engine(db_path: Path | str):
    url = f"sqlite:///{db_path}" if str(db_path) != ":memory:" else "sqlite://"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


class Database:
    def __init__(self, db_path: Path | str):
        self.engine = make_engine(db_path)
        self._sm = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)
        self.schema_changes = ensure_schema(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sm()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
