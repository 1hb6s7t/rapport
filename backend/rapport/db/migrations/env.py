"""Alembic environment. The app itself uses `create_all` at startup for v0.1; this env exists so that
schema changes after v0.1 ship as migrations (`uv run alembic revision --autogenerate -m "..."`).
Set RAPPORT_DATA_DIR to point at the database you want to migrate."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from rapport.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
data_dir = os.environ.get("RAPPORT_DATA_DIR")
if data_dir:
    config.set_main_option("sqlalchemy.url", f"sqlite:///{Path(data_dir) / 'app.db'}")
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
