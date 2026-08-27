"""Event bus.

Events are emitted by the executor and fan out to any number of sinks: an
in-memory buffer for tests, PostgreSQL for durable history, a Redis stream for
live consumers, a span-event recorder for traces.

Two design choices worth stating:

**Sequence numbers are assigned by the bus, not the emitter.**
    Concurrent parallel branches emit events within the same microsecond, so
    timestamps alone cannot order them. A monotonic per-execution counter can,
    and replaying history requires a total order.

**A failing sink cannot fail the execution.**
    Observability is not allowed to take down the thing it observes. Sink errors
    are captured and counted, and the executor carries on. That is a deliberate
    trade: a dropped event is better than a dead run.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from orchestration.domain.base import JsonDict
from orchestration.domain.enums import EventType
from orchestration.domain.events import EventFilter, ExecutionEvent, event_severity


class EventSink(abc.ABC):
    """A destination for execution events."""

    @abc.abstractmethod
    async def emit(self, event: ExecutionEvent) -> None:
        """Record one event. Must not raise for a recoverable problem."""

    async def emit_many(self, events: Sequence[ExecutionEvent]) -> None:
        """Record several events. Overridden by sinks that can batch."""
        for event in events:
            await self.emit(event)

    async def aclose(self) -> None:  # noqa: B027 - intentional no-op default
        """Flush and release resources. Idempotent.

        Not abstract: the in-memory and callback sinks hold nothing to close, and
        forcing them to write an empty override adds noise without safety.
        """


class InMemoryEventSink(EventSink):
    """Buffers events in a list. The sink tests assert against."""

    def __init__(self, *, max_events: int = 100_000) -> None:
        self._events: list[ExecutionEvent] = []
        self._max = max_events

    async def emit(self, event: ExecutionEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max:
            # Drop oldest rather than grow without bound: this sink is used by
            # long-running demos as well as tests.
            del self._events[: len(self._events) - self._max]

    @property
    def events(self) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events)

    def filtered(self, event_filter: EventFilter) -> tuple[ExecutionEvent, ...]:
        return tuple(e for e in self._events if event_filter.matches(e))

    def of_type(self, *types: EventType) -> tuple[ExecutionEvent, ...]:
        wanted = set(types)
        return tuple(e for e in self._events if e.type in wanted)

    def for_execution(self, execution_id: str) -> tuple[ExecutionEvent, ...]:
        return tuple(e for e in self._events if e.execution_id == execution_id)

    def count(self, event_type: EventType) -> int:
        return sum(1 for e in self._events if e.type is event_type)

    def clear(self) -> None:
        self._events.clear()


class CallbackEventSink(EventSink):
    """Forwards events to a coroutine. For ad-hoc subscribers and tests."""

    def __init__(self, callback: Callable[[ExecutionEvent], Awaitable[None]]) -> None:
        self._callback = callback

    async def emit(self, event: ExecutionEvent) -> None:
        await self._callback(event)


@dataclass(slots=True)
class SinkFailure:
    """Record of a sink that raised, kept so drops are visible not silent."""

    sink: str
    event_type: str
    error: str
    count: int = 1


class EventBus:
    """Assigns sequence numbers and fans events out to sinks.

    Args:
        sinks: Destinations.
        start_sequence: Where to resume numbering. Set when reconstructing a bus
            for an execution that already has history, so a resumed run does not
            restart at zero and collide with existing events.
    """

    def __init__(self, sinks: Sequence[EventSink] = (), *, start_sequence: int = 0) -> None:
        self._sinks = list(sinks)
        self._sequence = start_sequence
        self._lock = asyncio.Lock()
        self._failures: dict[tuple[str, str], SinkFailure] = {}

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    @property
    def sequence(self) -> int:
        """The last assigned sequence number."""
        return self._sequence

    @property
    def failures(self) -> tuple[SinkFailure, ...]:
        """Sink failures observed, so a dropped event is auditable."""
        return tuple(self._failures.values())

    async def publish(self, event: ExecutionEvent) -> ExecutionEvent:
        """Assign a sequence number and deliver to every sink.

        Returns the numbered event, which the caller may want to persist or
        inspect.
        """
        async with self._lock:
            self._sequence += 1
            numbered = event.model_copy(update={"sequence": self._sequence})

        # Sinks run concurrently: a slow database write should not delay delivery
        # to the in-memory sink a test or a live view is reading.
        results = await asyncio.gather(
            *(sink.emit(numbered) for sink in self._sinks), return_exceptions=True
        )
        for sink, outcome in zip(self._sinks, results, strict=True):
            if isinstance(outcome, BaseException):
                self._record_failure(sink, numbered, outcome)
        return numbered

    async def emit(
        self,
        event_type: EventType,
        *,
        execution_id: str,
        message: str = "",
        node_id: str | None = None,
        agent_id: str | None = None,
        tool: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        payload: JsonDict | None = None,
        **extra: object,
    ) -> ExecutionEvent:
        """Build and publish an event in one call.

        Payload may be supplied either as a dict (``payload=``) or as loose
        keywords; they are merged, with the explicit dict taking precedence. Both
        forms exist because call sites in the executor read better with keywords
        while the recorder forwards an already-assembled dict.
        """
        merged: JsonDict = {**extra, **(payload or {})}
        event = ExecutionEvent(
            execution_id=execution_id,
            type=event_type,
            severity=event_severity(event_type),
            node_id=node_id,
            agent_id=agent_id,
            tool=tool,
            message=message,
            payload=merged,
            trace_id=trace_id,
            span_id=span_id,
        )
        return await self.publish(event)

    async def aclose(self) -> None:
        """Close every sink, recording rather than raising any failure.

        A sink that cannot flush has probably lost events, so the failure is
        recorded in :attr:`failures`. It is not raised: shutting down must not
        fail because observability did.
        """
        for sink in self._sinks:
            try:
                await sink.aclose()
            except Exception as exc:
                key = (type(sink).__name__, type(exc).__name__)
                existing = self._failures.get(key)
                if existing is None:
                    self._failures[key] = SinkFailure(
                        sink=type(sink).__name__,
                        event_type="<close>",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    existing.count += 1

    def _record_failure(self, sink: EventSink, event: ExecutionEvent, error: BaseException) -> None:
        """Count a sink failure without propagating it.

        Aggregated by (sink, error type) so a persistently broken sink produces
        one growing record rather than thousands of identical ones.
        """
        key = (type(sink).__name__, type(error).__name__)
        existing = self._failures.get(key)
        if existing is None:
            self._failures[key] = SinkFailure(
                sink=type(sink).__name__,
                event_type=event.type.value,
                error=f"{type(error).__name__}: {error}",
            )
        else:
            existing.count += 1


@dataclass(slots=True)
class ExecutionEventRecorder:
    """Convenience wrapper binding a bus to one execution.

    Saves threading ``execution_id`` and ``trace_id`` through every call site in
    the executor, which is where most of the emission happens.
    """

    bus: EventBus
    execution_id: str
    trace_id: str | None = None
    #: Events emitted through this recorder, for assertions in tests.
    emitted: list[ExecutionEvent] = field(default_factory=list)

    async def emit(
        self,
        event_type: EventType,
        *,
        message: str = "",
        node_id: str | None = None,
        agent_id: str | None = None,
        tool: str | None = None,
        payload: JsonDict | None = None,
        **extra: object,
    ) -> ExecutionEvent:
        """Record one event.

        As with :meth:`EventBus.emit`, the payload may be supplied as a dict
        (``payload=``) or as loose keywords -- without an explicit ``payload``
        parameter here, ``payload=`` would itself have been swallowed by the
        loose-keyword catch-all as a single nested key, rather than becoming
        the event's payload.
        """
        event = await self.bus.emit(
            event_type,
            execution_id=self.execution_id,
            message=message,
            node_id=node_id,
            agent_id=agent_id,
            tool=tool,
            trace_id=self.trace_id,
            payload={**extra, **(payload or {})},
        )
        self.emitted.append(event)
        return event

    def types_emitted(self) -> tuple[str, ...]:
        return tuple(e.type.value for e in self.emitted)


def build_event_bus(*, in_memory: bool = True) -> tuple[EventBus, InMemoryEventSink | None]:
    """Construct a bus, returning the in-memory sink when one was attached."""
    sink = InMemoryEventSink() if in_memory else None
    bus = EventBus([sink] if sink else [])
    return bus, sink
