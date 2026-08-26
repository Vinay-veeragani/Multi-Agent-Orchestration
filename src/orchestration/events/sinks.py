"""Durable event sink backed by PostgreSQL.

Kept out of :mod:`orchestration.events.bus` so the bus itself stays free of any
persistence dependency -- which is what lets the unit suite use it without a
database.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestration.domain.events import ExecutionEvent
from orchestration.events.bus import EventSink
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import EventRepository


class PostgresEventSink(EventSink):
    """Writes events to the durable event log.

    Batches on ``emit_many`` so a step that produces a dozen events costs one
    transaction rather than a dozen.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def emit(self, event: ExecutionEvent) -> None:
        async with self._db.session() as session:
            await EventRepository(session).append(event)

    async def emit_many(self, events: Sequence[ExecutionEvent]) -> None:
        if not events:
            return
        async with self._db.session() as session:
            await EventRepository(session).append_many(events)


__all__ = ["PostgresEventSink"]
