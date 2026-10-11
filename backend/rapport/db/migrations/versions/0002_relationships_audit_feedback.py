"""relationships, llm_calls, feedback; relationship_id + ts_precision columns (v0.4)

Revision ID: 0002_relationships
Revises: 0001_initial
Create Date: 2026-09-16
"""

from __future__ import annotations

from alembic import op

from rapport.db.migrate import ensure_schema

revision = "0002_relationships"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive only: new tables are created, missing columns are added with their defaults.
    ensure_schema(op.get_bind().engine)


def downgrade() -> None:
    for table in ("feedback", "llm_calls", "relationships"):
        op.drop_table(table)
    # SQLite cannot drop columns portably before 3.35; the added columns are left in place.
