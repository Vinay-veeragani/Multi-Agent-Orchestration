"""Redis coordination: locks, concurrency semaphores, and event streams.

PostgreSQL is the durable source of truth. Redis handles the things a relational
database is a poor fit for: short-lived mutual exclusion, cross-process counters,
and a tailable event stream for live consumers.

Every primitive here is built to survive the holder dying, because that is the
failure mode that matters:

**Locks carry a fencing token and a TTL.**
    ``SET key token NX PX ttl`` means a crashed holder's lock expires rather than
    wedging an execution forever. Release is a Lua compare-and-delete against the
    token, so a process whose lock already expired cannot delete the *new*
    holder's lock -- the classic bug in naive Redis locking.

**Semaphores are counters with expiry, decremented atomically.**
    A leaked slot expires instead of permanently reducing capacity.

**Streams are capped.**
    ``XADD MAXLEN ~`` bounds memory. The durable copy is in PostgreSQL, so
    trimming the stream loses nothing that matters.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from orchestration.config import Settings, get_settings
from orchestration.domain.events import ExecutionEvent
from orchestration.errors import (
    ConcurrencyConflictError,
    StorageTransientError,
)
from orchestration.events.bus import EventSink

#: Release a lock only if we still hold it. Compare-and-delete must be atomic, or
#: a process whose lock expired mid-operation can delete the next holder's lock.
_RELEASE_SCRIPT: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

#: Extend a lock we still hold. Same reasoning as release.
_EXTEND_SCRIPT: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

#: Acquire a semaphore slot: increment, and refuse (rolling back) past the limit.
#: A single script because check-then-increment from the client is a race.
_ACQUIRE_SLOT_SCRIPT: Final[str] = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if current >= limit then
    return -1
end
local value = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return value
"""

#: Release a slot without going negative, which a bare DECR would allow after a
#: double release and would then over-admit work.
_RELEASE_SLOT_SCRIPT: Final[str] = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current <= 0 then
    return 0
end
return redis.call('DECR', KEYS[1])
"""


def _as_text(value: object) -> str:
    """Coerce a Redis reply to ``str``.

    The client is built with ``decode_responses=True``, so replies really are
    strings at runtime -- but that is a constructor flag the type stubs cannot
    see, so every read is typed ``bytes | str``. Coercing at this one boundary
    keeps the rest of the module honestly typed instead of scattering casts.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _as_fields(value: object) -> dict[str, str]:
    """Coerce a Redis hash/stream entry to ``dict[str, str]``."""
    if not isinstance(value, dict):
        return {}
    return {_as_text(k): _as_text(v) for k, v in value.items()}


@dataclass(slots=True)
class LockHandle:
    """A held lock and the token proving ownership."""

    key: str
    token: str
    ttl_seconds: float


class RedisCoordinator:
    """Locks, semaphores, and streams over Redis.

    Args:
        url: Redis URL. Defaults to the configured application URL, which the
            settings validator forbids from being db 0 so a shared local Redis
            is never clobbered.
        namespace: Key prefix.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        namespace: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        config = settings or get_settings()
        self._url = url or config.redis_url
        self._namespace = namespace or config.redis_namespace
        self._client: aioredis.Redis | None = None

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    def key(self, *parts: str) -> str:
        return ":".join((self._namespace, *parts))

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except RedisError:
            return False

    async def info(self) -> dict[str, Any]:
        """Server info, for the health endpoint.

        Reports the Redis version rather than assuming: this project is developed
        against Memurai on Windows, which speaks the Redis protocol but is a
        different implementation, and knowing which one answered is useful.
        """
        try:
            raw = await self.client.info("server")
        except RedisError as exc:
            raise StorageTransientError("redis is unreachable", detail=str(exc)) from exc
        fields = _as_fields(raw)
        return {
            "redis_version": fields.get("redis_version"),
            "mode": fields.get("redis_mode"),
            "os": fields.get("os"),
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- locks -------------------------------------------------------------

    async def acquire_lock(
        self, name: str, *, ttl_seconds: float = 30.0, token: str | None = None
    ) -> LockHandle | None:
        """Try to take a lock. Returns ``None`` if it is already held."""
        key = self.key("lock", name)
        value = token or uuid.uuid4().hex
        try:
            acquired = await self.client.set(key, value, nx=True, px=int(ttl_seconds * 1000))
        except RedisError as exc:
            raise StorageTransientError("redis lock acquisition failed", key=name) from exc
        if not acquired:
            return None
        return LockHandle(key=key, token=value, ttl_seconds=ttl_seconds)

    async def release_lock(self, handle: LockHandle) -> bool:
        """Release a lock, only if still held by this token."""
        try:
            result = await self.client.eval(_RELEASE_SCRIPT, 1, handle.key, handle.token)
        except RedisError as exc:
            raise StorageTransientError("redis lock release failed", key=handle.key) from exc
        return bool(result)

    async def extend_lock(self, handle: LockHandle, *, ttl_seconds: float | None = None) -> bool:
        """Extend a held lock.

        Needed by long executions: a 30s lock on a five-minute run would expire
        mid-flight, so the owner renews rather than taking a lock long enough to
        cover the worst case (which would wedge the execution if it crashed).
        """
        ttl = ttl_seconds or handle.ttl_seconds
        try:
            result = await self.client.eval(
                _EXTEND_SCRIPT, 1, handle.key, handle.token, int(ttl * 1000)
            )
        except RedisError as exc:
            raise StorageTransientError("redis lock extension failed", key=handle.key) from exc
        return bool(result)

    @contextlib.asynccontextmanager
    async def lock(
        self, name: str, *, ttl_seconds: float = 30.0, raise_on_conflict: bool = True
    ) -> AsyncIterator[LockHandle | None]:
        """Hold a lock for the duration of a block."""
        handle = await self.acquire_lock(name, ttl_seconds=ttl_seconds)
        if handle is None:
            if raise_on_conflict:
                raise ConcurrencyConflictError(
                    f"lock {name!r} is held by another worker", lock=name
                )
            yield None
            return
        try:
            yield handle
        finally:
            await self.release_lock(handle)

    async def is_locked(self, name: str) -> bool:
        return bool(await self.client.exists(self.key("lock", name)))

    # -- semaphores --------------------------------------------------------

    async def acquire_slot(self, name: str, *, limit: int, ttl_seconds: float = 300.0) -> bool:
        """Take one slot from a named counter, if capacity allows."""
        key = self.key("sem", name)
        try:
            result = await self.client.eval(
                _ACQUIRE_SLOT_SCRIPT, 1, key, str(limit), str(int(ttl_seconds))
            )
        except RedisError as exc:
            raise StorageTransientError("redis semaphore failed", key=name) from exc
        return int(result) >= 0

    async def release_slot(self, name: str) -> int:
        key = self.key("sem", name)
        try:
            result = await self.client.eval(_RELEASE_SLOT_SCRIPT, 1, key)
        except RedisError as exc:
            raise StorageTransientError("redis semaphore release failed", key=name) from exc
        return int(result)

    async def slots_in_use(self, name: str) -> int:
        value = await self.client.get(self.key("sem", name))
        return int(_as_text(value)) if value else 0

    @contextlib.asynccontextmanager
    async def slot(
        self, name: str, *, limit: int, ttl_seconds: float = 300.0
    ) -> AsyncIterator[bool]:
        """Hold a semaphore slot for the duration of a block."""
        acquired = await self.acquire_slot(name, limit=limit, ttl_seconds=ttl_seconds)
        try:
            yield acquired
        finally:
            if acquired:
                await self.release_slot(name)

    # -- cancellation ------------------------------------------------------

    async def request_cancellation(self, execution_id: str, reason: str) -> None:
        """Signal cancellation for an execution running in another process.

        Cross-process cancellation needs a shared signal: the HTTP request that
        cancels may reach a different worker than the one executing. Given a TTL
        so a stale flag cannot silently cancel a later execution reusing the id.
        """
        await self.client.set(self.key("cancel", execution_id), reason, ex=86_400)

    async def cancellation_requested(self, execution_id: str) -> str | None:
        raw = await self.client.get(self.key("cancel", execution_id))
        return _as_text(raw) if raw is not None else None

    async def clear_cancellation(self, execution_id: str) -> None:
        await self.client.delete(self.key("cancel", execution_id))

    # -- streams -----------------------------------------------------------

    async def publish_event(self, event: ExecutionEvent, *, max_len: int = 10_000) -> str:
        """Append an event to this execution's stream.

        Capped with ``MAXLEN ~`` (approximate trimming, which is much cheaper).
        The durable copy lives in PostgreSQL, so trimming loses nothing.
        """
        stream = self.key("events", event.execution_id)
        try:
            # The stub's field-mapping type is wider than dict[str, str]; the
            # runtime accepts plain strings, so widen rather than fight it.
            fields: dict[Any, Any] = dict(event.to_stream_fields())
            message_id = await self.client.xadd(stream, fields, maxlen=max_len, approximate=True)
        except RedisError as exc:
            raise StorageTransientError(
                "redis event publish failed", execution=event.execution_id
            ) from exc
        return _as_text(message_id)

    async def read_events(
        self, execution_id: str, *, after: str = "-", count: int = 100
    ) -> list[dict[str, str]]:
        """Read events from the stream, oldest first."""
        stream = self.key("events", execution_id)
        entries: Any = await self.client.xrange(stream, min=after, max="+", count=count)
        return [
            {"_id": _as_text(entry_id), **_as_fields(fields)} for entry_id, fields in entries or []
        ]

    async def tail_events(
        self, execution_id: str, *, block_ms: int = 5_000, last_id: str = "$"
    ) -> AsyncIterator[dict[str, str]]:
        """Follow an execution's stream, yielding events as they arrive.

        Backs live progress views. Uses blocking ``XREAD`` rather than polling, so
        an idle execution costs one open connection instead of continuous queries.
        """
        stream = self.key("events", execution_id)
        cursor = last_id
        while True:
            try:
                response = await self.client.xread({stream: cursor}, count=50, block=block_ms)
            except RedisError as exc:
                raise StorageTransientError(
                    "redis stream read failed", execution=execution_id
                ) from exc
            if not response:
                continue
            # XREAD replies as [(stream, [(id, fields), ...]), ...]. The stub types
            # this loosely, so it is normalised through an Any-typed local instead
            # of being destructured against a union that does not describe it.
            streams: Any = response
            for _stream_name, entries in streams:
                for entry_id, fields in entries or []:
                    cursor = _as_text(entry_id)
                    yield {"_id": cursor, **_as_fields(fields)}

    async def stream_length(self, execution_id: str) -> int:
        return int(await self.client.xlen(self.key("events", execution_id)))

    # -- maintenance -------------------------------------------------------

    async def flush_namespace(self) -> int:
        """Delete every key in this namespace.

        Scoped to the namespace and iterated with ``SCAN``: a ``FLUSHDB`` would
        destroy anything else sharing the database, and this project explicitly
        promises not to do that.
        """
        deleted = 0
        pattern = f"{self._namespace}:*"
        async for key in self.client.scan_iter(match=pattern, count=500):
            await self.client.delete(key)
            deleted += 1
        return deleted


class RedisEventSink(EventSink):
    """Publishes events to a Redis stream.

    Registered alongside the PostgreSQL sink. If Redis is down, the bus records
    the sink failure and the execution continues -- the durable copy is in
    PostgreSQL, so a live-view outage is not an execution outage.
    """

    def __init__(self, coordinator: RedisCoordinator, *, max_len: int = 10_000) -> None:
        self._coordinator = coordinator
        self._max_len = max_len

    async def emit(self, event: ExecutionEvent) -> None:
        await self._coordinator.publish_event(event, max_len=self._max_len)

    async def emit_many(self, events: Sequence[ExecutionEvent]) -> None:
        for event in events:
            await self.emit(event)


class ConcurrencyLimiter:
    """Enforces deployment-wide concurrency caps across processes.

    An in-process semaphore bounds one worker; these bound the deployment. Both
    exist because they answer different questions: "how much work may this
    process do at once" and "how much work may exist at once".
    """

    def __init__(
        self,
        coordinator: RedisCoordinator,
        *,
        max_executions: int,
        max_agents: int,
        max_tools: int,
    ) -> None:
        self._coordinator = coordinator
        self._limits = {
            "executions": max_executions,
            "agents": max_agents,
            "tools": max_tools,
        }

    @contextlib.asynccontextmanager
    async def execution_slot(self) -> AsyncIterator[bool]:
        async with self._coordinator.slot(
            "executions", limit=self._limits["executions"], ttl_seconds=3_600
        ) as acquired:
            yield acquired

    @contextlib.asynccontextmanager
    async def agent_slot(self) -> AsyncIterator[bool]:
        async with self._coordinator.slot(
            "agents", limit=self._limits["agents"], ttl_seconds=600
        ) as acquired:
            yield acquired

    @contextlib.asynccontextmanager
    async def tool_slot(self) -> AsyncIterator[bool]:
        async with self._coordinator.slot(
            "tools", limit=self._limits["tools"], ttl_seconds=300
        ) as acquired:
            yield acquired

    async def usage(self) -> dict[str, dict[str, int]]:
        """Current occupancy, for the metrics endpoint."""
        return {
            name: {
                "in_use": await self._coordinator.slots_in_use(name),
                "limit": limit,
            }
            for name, limit in self._limits.items()
        }
