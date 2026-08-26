"""Fixtures for tests that require real PostgreSQL and Redis.

These are the only tests that touch a network socket. They skip cleanly when the
services are unreachable, so ``pytest tests/unit`` on a bare checkout still runs.

The schema is created once per session and truncated between tests. Truncating
rather than recreating means every test runs against the *same* schema object,
which is what makes an index or constraint problem show up here instead of in
production.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from orchestration.coordination.redis import RedisCoordinator
from orchestration.persistence.database import Database

#: Test database and Redis db, both distinct from the application's.
TEST_PG_DSN = os.environ.get(
    "ORCH_PG_TEST_DSN",
    "postgresql+asyncpg://orchestrator:orch_local_dev_only@127.0.0.1:5432/orchestration_test",
)
TEST_REDIS_URL = os.environ.get("ORCH_REDIS_TEST_URL", "redis://127.0.0.1:6379/15")


async def _postgres_available(dsn: str) -> bool:
    database = Database(dsn, use_null_pool=True)
    try:
        return await database.ping()
    finally:
        await database.aclose()


async def _redis_available(url: str) -> bool:
    coordinator = RedisCoordinator(url, namespace="orch_test")
    try:
        return await coordinator.ping()
    finally:
        await coordinator.aclose()


#: Memoised so the schema is built once per test session. A session-scoped async
#: fixture would need a session-scoped event loop, which conflicts with the
#: function-scoped loop the rest of the suite uses; a module flag is simpler than
#: reconciling the two.
_SCHEMA_READY = False
_PG_REACHABLE: bool | None = None


@pytest_asyncio.fixture
async def schema() -> AsyncIterator[None]:
    """Ensure the schema exists, building it on first use.

    Uses ``create_all`` rather than Alembic so a schema problem is isolated from
    a migration problem; the migrations are verified by their own test.
    """
    global _SCHEMA_READY, _PG_REACHABLE

    if _PG_REACHABLE is None:
        _PG_REACHABLE = await _postgres_available(TEST_PG_DSN)
    if not _PG_REACHABLE:
        pytest.skip("PostgreSQL is not reachable")

    if not _SCHEMA_READY:
        database = Database(TEST_PG_DSN, use_null_pool=True)
        try:
            await database.drop_all()
            await database.create_all()
        finally:
            await database.aclose()
        _SCHEMA_READY = True
    yield


@pytest_asyncio.fixture
async def database(schema: None) -> AsyncIterator[Database]:
    """A database with every table empty.

    ``NullPool`` because a pooled connection surviving between tests with
    different event loops produces confusing cross-test failures.
    """
    db = Database(TEST_PG_DSN, use_null_pool=True)
    await db.truncate_all()
    try:
        yield db
    finally:
        await db.aclose()


@pytest_asyncio.fixture
async def redis_coordinator() -> AsyncIterator[RedisCoordinator]:
    """A Redis coordinator on the test database, namespaced and flushed."""
    coordinator = RedisCoordinator(TEST_REDIS_URL, namespace="orch_test")
    if not await coordinator.ping():
        await coordinator.aclose()
        pytest.skip("Redis is not reachable")
    await coordinator.flush_namespace()
    try:
        yield coordinator
    finally:
        await coordinator.flush_namespace()
        await coordinator.aclose()
