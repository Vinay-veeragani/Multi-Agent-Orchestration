"""Async database engine and session management.

One engine per process, one session per unit of work. Both are explicit rather
than global, so tests can point at a different database without monkeypatching a
module-level singleton.

Two PostgreSQL specifics are configured here rather than assumed:

**A statement timeout.**
    Without one, a pathological query holds a connection from the pool forever.
    Set per-connection so it applies to every session.

**Error translation.**
    ``asyncpg`` and SQLAlchemy exceptions are mapped onto the engine's error
    taxonomy at this boundary, so the retry layer sees ``StorageTransientError``
    for a lost connection and a terminal error for a constraint violation --
    without anything above this module importing a driver.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from orchestration.config import Settings, get_settings
from orchestration.errors import (
    ConcurrencyConflictError,
    DuplicateError,
    OrchestrationError,
    StorageTransientError,
)

#: Type of the session factory the repositories depend on.
SessionFactory = async_sessionmaker[AsyncSession]


def build_engine(
    dsn: str | None = None,
    *,
    settings: Settings | None = None,
    echo: bool | None = None,
    use_null_pool: bool = False,
) -> AsyncEngine:
    """Create an async engine.

    Args:
        dsn: Connection string. Defaults to the configured application DSN.
        settings: Configuration source.
        echo: Log SQL. Defaults to the configured value.
        use_null_pool: Disable pooling. Used by tests, where a pooled connection
            outliving an event loop causes confusing cross-test failures.
    """
    config = settings or get_settings()
    target = dsn or config.pg_dsn

    kwargs: dict[str, object] = {
        "echo": config.pg_echo if echo is None else echo,
        # A statement timeout is set per connection rather than per session: it
        # must apply to every query, including ones issued by migrations.
        "connect_args": {
            "server_settings": {
                "statement_timeout": str(config.pg_statement_timeout_ms),
                "application_name": config.service_name,
            }
        },
    }
    if use_null_pool:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = config.pg_pool_size
        kwargs["max_overflow"] = config.pg_max_overflow
        kwargs["pool_pre_ping"] = True  # a stale connection is retried, not raised

    return create_async_engine(target, **kwargs)


def build_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Create a session factory.

    ``expire_on_commit=False`` because the engine reads attributes from returned
    objects after commit; leaving it on would trigger lazy reloads against a
    closed session.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False, class_=AsyncSession)


class Database:
    """Owns the engine and hands out sessions.

    Args:
        dsn: Connection string; defaults to the configured application DSN.
        settings: Configuration source.
        use_null_pool: Disable pooling (tests).
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        settings: Settings | None = None,
        use_null_pool: bool = False,
    ) -> None:
        self._settings = settings or get_settings()
        self._dsn = dsn or self._settings.pg_dsn
        self._engine = build_engine(self._dsn, settings=self._settings, use_null_pool=use_null_pool)
        self._session_factory = build_session_factory(self._engine)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> SessionFactory:
        return self._session_factory

    @property
    def dsn_safe(self) -> str:
        """The DSN with its password removed -- the only form safe to log."""
        from orchestration.config import _redact_dsn

        return _redact_dsn(self._dsn)

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure.

        Error translation happens here so every repository inherits it.
        """
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise translate_error(exc) from exc
            except Exception:
                await session.rollback()
                raise

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A session inside an explicit transaction block.

        Used where several writes must be atomic -- notably persisting a
        checkpoint alongside the state update it corresponds to.
        """
        async with self._session_factory() as session, session.begin():
            try:
                yield session
            except SQLAlchemyError as exc:
                raise translate_error(exc) from exc

    async def ping(self) -> bool:
        """Whether the database is reachable. Used by the health endpoint."""
        from sqlalchemy import text

        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    async def pgvector_version(self) -> str | None:
        """Installed pgvector version, or ``None`` if the extension is absent.

        Surfaced by the health endpoint: an install missing the extension will
        fail only when evidence retrieval is first used, which is a bad time to
        find out.
        """
        from sqlalchemy import text

        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
                row = result.scalar_one_or_none()
            return str(row) if row is not None else None
        except SQLAlchemyError:
            return None

    async def create_all(self) -> None:
        """Create every table. For tests; production uses Alembic."""
        from sqlalchemy import text

        from orchestration.persistence.tables import Base

        async with self._engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        """Drop every table. For tests."""
        from orchestration.persistence.tables import Base

        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def truncate_all(self) -> None:
        """Empty every table, preserving the schema.

        Faster than drop/create between tests, and it exercises the same schema
        the migrations produced rather than a freshly built one.
        """
        from sqlalchemy import text

        from orchestration.persistence.tables import ALL_TABLES

        async with self._engine.begin() as connection:
            await connection.execute(
                text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            )

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def __aenter__(self) -> Database:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


#: PostgreSQL SQLSTATE codes that indicate a transient condition.
_TRANSIENT_SQLSTATES: frozenset[str] = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "53300",  # too_many_connections
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
        "08000",  # connection_exception
        "08003",  # connection_does_not_exist
        "08006",  # connection_failure
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "55P03",  # lock_not_available
    }
)


def translate_error(exc: SQLAlchemyError) -> OrchestrationError:
    """Map a SQLAlchemy error onto the engine's taxonomy.

    Classifying by SQLSTATE rather than by exception class where possible: the
    codes are stable and standardised, whereas which SQLAlchemy class wraps a
    given failure varies by driver and version.
    """
    sqlstate = _sqlstate_of(exc)

    if sqlstate in _TRANSIENT_SQLSTATES:
        if sqlstate in {"40001", "40P01"}:
            return ConcurrencyConflictError(
                "database serialisation conflict; the operation may be retried",
                sqlstate=sqlstate,
            )
        return StorageTransientError(
            "database connection problem", sqlstate=sqlstate, detail=type(exc).__name__
        )

    if isinstance(exc, IntegrityError):
        # 23505 unique_violation is the common case and is genuinely terminal:
        # inserting the same key again will fail identically.
        return DuplicateError(
            "database rejected the write as a constraint violation",
            sqlstate=sqlstate,
            detail=_first_line(str(exc.orig) if exc.orig else str(exc)),
        )

    if isinstance(exc, OperationalError | InterfaceError):
        return StorageTransientError(
            "database operation failed transiently",
            sqlstate=sqlstate,
            detail=type(exc).__name__,
        )

    return StorageTransientError("database error", sqlstate=sqlstate, detail=type(exc).__name__)


def _sqlstate_of(exc: SQLAlchemyError) -> str | None:
    """Extract the SQLSTATE code, tolerating drivers that expose it differently."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(exc.orig, attribute, None)
            if value:
                return str(value)
    return None


def _first_line(text: str, *, limit: int = 300) -> str:
    return text.strip().splitlines()[0][:limit] if text.strip() else ""


def is_unique_violation(exc: BaseException) -> bool:
    """Whether an error is specifically a unique-constraint violation.

    Used by the idempotent write paths: a duplicate checkpoint or tool
    invocation is not a failure, it is the concurrency guard doing its job.
    """
    if isinstance(exc, DuplicateError):
        return True
    if isinstance(exc, IntegrityError):
        return _sqlstate_of(exc) == "23505"
    return False


#: Lazily-built process-wide database, for the API and CLI.
_database: Database | None = None


def get_database(*, settings: Settings | None = None) -> Database:
    """Return the process-wide database, creating it on first use."""
    global _database
    if _database is None:
        _database = Database(settings=settings)
    return _database


async def reset_database() -> None:
    """Dispose the process-wide database. Used at shutdown and between tests."""
    global _database
    if _database is not None:
        await _database.aclose()
        _database = None


def session_dependency(database: Database) -> Callable[[], AsyncIterator[AsyncSession]]:
    """Build a FastAPI dependency yielding a session."""

    async def _dependency() -> AsyncIterator[AsyncSession]:
        async with database.session() as session:
            yield session

    return _dependency
