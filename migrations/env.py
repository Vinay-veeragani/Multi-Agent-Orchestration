"""Alembic environment.

Two things are deliberate here:

**The DSN comes from settings, never from ``alembic.ini``.** A committed ini
file with a connection string is one accidental commit away from leaking a
credential; ``alembic.ini`` here has no ``sqlalchemy.url`` at all, and this
module reads ``ORCH_PG_DSN`` (or ``ORCH_PG_MIGRATIONS_DSN`` to point migrations
at a different database than the running application) through the same
``Settings`` object the rest of the engine uses, so there is exactly one place
DSNs are configured.

**Target metadata is the real application schema.** ``target_metadata`` is
``orchestration.persistence.tables.Base.metadata`` -- the same declarative base
the engine reads and writes through at runtime. Autogenerate diffs against
what the ORM actually declares, not a hand-maintained parallel description of
the schema that could drift from it.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# The package is installed editable (`pip install -e .`), so this import works
# without any sys.path manipulation.
from orchestration.config import get_settings
from orchestration.persistence.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the migration target DSN.

    ``ORCH_PG_MIGRATIONS_DSN`` takes precedence when set, so CI or an operator
    can point migrations at a database other than the one the running
    application uses (a fresh schema-verification database, say) without
    touching the application's own configuration.
    """
    override = os.environ.get("ORCH_PG_MIGRATIONS_DSN")
    if override:
        return override
    return get_settings().pg_dsn


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Column type changes (e.g. widening a String) show up in autogenerate
        # diffs; without this Alembic only compares column presence.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
