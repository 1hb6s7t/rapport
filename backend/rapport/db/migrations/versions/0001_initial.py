"""initial schema (v0.1)

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-14
"""

from __future__ import annotations

from alembic import op

from rapport.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
