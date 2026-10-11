"""Idempotent, additive schema upgrades for existing SQLite files.

`Base.metadata.create_all` creates tables that do not exist but never adds columns to tables
that do. Every release so far has only *added* tables and columns, so the upgrade path is:

1. `create_all` for new tables;
2. for each known table, `ALTER TABLE ... ADD COLUMN` for every model column the file lacks,
   with the model's scalar default baked in so old rows read consistently.

Both steps are safe to run on every start. Anything destructive (renames, type changes) must be
an explicit Alembic revision — this module refuses to guess.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from rapport.db.models import Base

log = logging.getLogger("rapport.db")


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict | list):
        # JSON columns: an empty object / list literal keeps old rows readable.
        return "'{}'" if isinstance(value, dict) else "'[]'"
    return "'" + str(value).replace("'", "''") + "'"


def _column_default(column) -> str | None:
    """Best-effort literal default for ADD COLUMN; None means no DEFAULT clause."""
    default = column.default
    if default is None:
        return None
    if getattr(default, "is_scalar", False):
        return _sql_literal(default.arg)
    if getattr(default, "is_callable", False):
        # datetime.now / uuid factories: old rows get NULL, which every reader tolerates.
        return None
    return None


def ensure_schema(engine: Engine) -> dict[str, list[str]]:
    """Create missing tables and add missing columns. Returns {table: [added columns]}."""
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: dict[str, list[str]] = {}
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                default = _column_default(column)
                if default is not None:
                    clause += f" DEFAULT {default}"
                conn.execute(text(clause))
                added.setdefault(table.name, []).append(column.name)
                if column.index:
                    conn.execute(
                        text(f'CREATE INDEX IF NOT EXISTS "ix_{table.name}_{column.name}" ON "{table.name}" ("{column.name}")')
                    )
    if added:
        log.info("schema upgraded: %s", added)
    return added
