"""Migration integration tests against real PostgreSQL.

These exist because Phase 8's ``create_all()`` and the Alembic migrations are
two independent ways to produce the same schema, and nothing structurally
guarantees they stay in agreement -- a column added to a table in
:mod:`orchestration.persistence.tables` without a corresponding migration would
pass every repository test (which runs against ``create_all()``) while leaving
production deployments, which run migrations, permanently out of date.

What is verified here, against the actual database on this machine:

* ``alembic upgrade head`` on an empty database produces every table
  ``create_all()`` would, including the ``vector`` extension and the pgvector
  column -- not approximately, but with zero drift, checked the same way
  ``alembic check`` checks it.
* ``alembic downgrade base`` removes every application table cleanly.
* The upgrade is idempotent in the sense that matters: running it against a
  database that already has the extension installed does not fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text

from orchestration.persistence.database import Database
from orchestration.persistence.tables import ALL_TABLES, Base

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: `alembic.command.upgrade`/`downgrade` are synchronous entry points, but
#: `migrations/env.py` drives its own async engine internally via
#: `asyncio.run()`. That cannot nest inside the event loop pytest-asyncio is
#: already running for these tests, so every call is dispatched through
#: `asyncio.to_thread`, which gives it a plain thread with no loop to nest in.


async def _reset_schema(database: Database) -> None:
    """Drop every application table *and* Alembic's own version-tracking table.

    ``Database.drop_all()`` only drops what ``Base.metadata`` declares, which
    does not include ``alembic_version`` -- Alembic creates and owns that table
    itself. Leaving it behind after a manual drop means the next
    ``upgrade("head")`` sees the database already stamped at head and treats it
    as a no-op, silently leaving the application tables missing. Dropping both
    is what makes each test a genuine fresh-database upgrade.
    """
    await database.drop_all()
    async with database.session() as session:
        await session.execute(text("DROP TABLE IF EXISTS alembic_version"))


def _alembic_config(dsn: str) -> Config:
    """An Alembic config pointed at ``dsn`` via the override env var.

    Uses ``ORCH_PG_MIGRATIONS_DSN`` -- the same override
    ``migrations/env.py`` already supports -- rather than duplicating DSN
    resolution logic here.
    """
    import os

    os.environ["ORCH_PG_MIGRATIONS_DSN"] = dsn
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


class TestMigrationsApplyCleanly:
    @pytest.fixture(autouse=True)
    async def _restore_schema_after(self, database: Database) -> AsyncIterator[None]:
        """Guarantee the shared test database is back at head after each test.

        Several tests here deliberately leave the schema downgraded to
        inspect that state. The `database` fixture is function-scoped but
        points at the same physical test database across the whole module, and
        its own setup only truncates rows -- it assumes the tables already
        exist. Without this, a downgrade test leaves the next test's fixture
        setup failing on a table that no longer exists, which is exactly the
        kind of cross-test pollution a database-backed suite has to guard
        against explicitly.
        """
        yield
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await asyncio.to_thread(command.upgrade, config, "head")

    async def test_upgrade_head_produces_every_table(self, database: Database) -> None:
        """Mirrors what a fresh production deployment actually runs."""
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await _reset_schema(database)

        await asyncio.to_thread(command.upgrade, config, "head")

        async with database.session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                            "ORDER BY tablename"
                        )
                    )
                )
                .scalars()
                .all()
            )

        migrated = set(rows) - {"alembic_version"}
        assert migrated == set(ALL_TABLES)

    async def test_upgrade_installs_pgvector(self, database: Database) -> None:
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await _reset_schema(database)
        await asyncio.to_thread(command.upgrade, config, "head")

        version = await database.pgvector_version()
        assert version is not None

    async def test_evidence_chunks_has_a_real_vector_column(self, database: Database) -> None:
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await _reset_schema(database)
        await asyncio.to_thread(command.upgrade, config, "head")

        async with database.session() as session:
            udt = (
                await session.execute(
                    text(
                        "SELECT udt_name FROM information_schema.columns "
                        "WHERE table_name = 'evidence_chunks' AND column_name = 'embedding'"
                    )
                )
            ).scalar_one()
        assert udt == "vector"

    async def test_migrated_schema_matches_the_orm_exactly(self, database: Database) -> None:
        """The same zero-drift check `alembic check` performs, run in-process.

        This is the test that would fail if a table or column were added to
        the ORM without a corresponding migration: `compare_metadata` diffs the
        live, migrated schema against `Base.metadata` and must find nothing.

        `compare_metadata` needs a sync `Connection`. Rather than adding a sync
        driver dependency just for this one comparison, the async engine's own
        `run_sync` bridges a sync callable onto the async connection it already
        holds -- the same pattern `Database.create_all()` uses.
        """
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await _reset_schema(database)
        await asyncio.to_thread(command.upgrade, config, "head")

        def _diff(sync_connection: Connection) -> list[object]:
            context = MigrationContext.configure(sync_connection)
            return list(compare_metadata(context, Base.metadata))

        async with database.engine.connect() as connection:
            diff = await connection.run_sync(_diff)

        # Sequence-ownership detection noise (SERIAL columns) is the only thing
        # Alembic itself also treats as expected during `check`; anything else
        # is a real drift between the ORM and the migration.
        assert diff == [], f"schema drift between ORM and migration: {diff}"

    async def test_downgrade_removes_every_application_table(self, database: Database) -> None:
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await _reset_schema(database)
        await asyncio.to_thread(command.upgrade, config, "head")

        await asyncio.to_thread(command.downgrade, config, "base")

        async with database.session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                            "ORDER BY tablename"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert set(rows) == {"alembic_version"}

    async def test_upgrade_is_safe_when_the_extension_already_exists(
        self, database: Database
    ) -> None:
        """CREATE EXTENSION IF NOT EXISTS must not fail on a pre-provisioned db."""
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await _reset_schema(database)
        async with database.session() as session:
            await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        await asyncio.to_thread(command.upgrade, config, "head")  # must not raise

        async with database.session() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one()
        assert count == 1

    async def test_downgrade_does_not_drop_the_extension(self, database: Database) -> None:
        """Other schemas or a manual install may depend on it."""
        from tests.integration.conftest import TEST_PG_DSN

        config = _alembic_config(TEST_PG_DSN)
        await _reset_schema(database)
        await asyncio.to_thread(command.upgrade, config, "head")
        await asyncio.to_thread(command.downgrade, config, "base")

        version = await database.pgvector_version()
        assert version is not None, "downgrade must not remove the vector extension"
