"""Typed execution events.

Events are the observability substrate: they are persisted to PostgreSQL for
durable history, published to a Redis stream for live consumers, and mirrored as
span events in traces. One event type carries a JSONB payload rather than a
subclass per event, because the alternative -- forty Pydantic subclasses -- makes
querying and deserialising history far harder without buying real type safety at
the persistence boundary.

Payload *construction* is still typed: the factory helpers below are the only
sanctioned way to build events, so payload keys stay consistent.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from orchestration.domain.base import (
    BoundedText,
    FrozenModel,
    JsonDict,
    Slug,
    id_factory,
    utc_now,
)
from orchestration.domain.enums import EventSeverity, EventType

#: Which severity each event type carries, so callers never pass it by hand.
_EVENT_SEVERITY: dict[EventType, EventSeverity] = {
    EventType.EXECUTION_FAILED: EventSeverity.ERROR,
    EventType.NODE_FAILED: EventSeverity.ERROR,
    EventType.AGENT_FAILED: EventSeverity.ERROR,
    EventType.TOOL_FAILED: EventSeverity.ERROR,
    EventType.RETRY_EXHAUSTED: EventSeverity.ERROR,
    EventType.BUDGET_EXCEEDED: EventSeverity.ERROR,
    EventType.EXECUTION_CANCELLED: EventSeverity.WARNING,
    EventType.TOOL_DENIED: EventSeverity.WARNING,
    EventType.POLICY_DENIED: EventSeverity.WARNING,
    EventType.APPROVAL_REJECTED: EventSeverity.WARNING,
    EventType.APPROVAL_EXPIRED: EventSeverity.WARNING,
    EventType.BUDGET_WARNING: EventSeverity.WARNING,
    EventType.RETRY_STARTED: EventSeverity.WARNING,
    EventType.ROUTING_DEGRADED: EventSeverity.WARNING,
    EventType.NODE_SKIPPED: EventSeverity.DEBUG,
    EventType.CHECKPOINT_CREATED: EventSeverity.DEBUG,
    EventType.CHECKPOINT_RESTORED: EventSeverity.DEBUG,
    EventType.LLM_CALL_STARTED: EventSeverity.DEBUG,
    EventType.LLM_CALL_COMPLETED: EventSeverity.DEBUG,
}


def event_severity(event_type: EventType) -> EventSeverity:
    """Severity implied by an event type.

    Exposed so the bus can build an event directly without going through the
    factory, while still deriving severity from one table.
    """
    return _EVENT_SEVERITY.get(event_type, EventSeverity.INFO)


class ExecutionEvent(FrozenModel):
    """One immutable record of something that happened during an execution.

    Attributes:
        sequence: Monotonic per-execution ordering. Timestamps alone are
            insufficient: events emitted from concurrent parallel branches can
            share a microsecond, and replaying history requires a total order.
    """

    id: str = Field(default_factory=id_factory("event"))
    execution_id: str
    sequence: int = Field(default=0, ge=0)
    type: EventType
    severity: EventSeverity = EventSeverity.INFO
    node_id: Slug | None = None
    agent_id: Slug | None = None
    tool: Slug | None = None
    message: BoundedText = ""
    payload: JsonDict = Field(default_factory=dict)
    trace_id: str | None = None
    span_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def make(
        cls,
        type_: EventType,
        *,
        execution_id: str,
        sequence: int = 0,
        message: str = "",
        node_id: str | None = None,
        agent_id: str | None = None,
        tool: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        **payload: object,
    ) -> ExecutionEvent:
        """Build an event with the severity implied by its type."""
        return cls(
            execution_id=execution_id,
            sequence=sequence,
            type=type_,
            severity=_EVENT_SEVERITY.get(type_, EventSeverity.INFO),
            node_id=node_id,
            agent_id=agent_id,
            tool=tool,
            message=message,
            payload=dict(payload),
            trace_id=trace_id,
            span_id=span_id,
        )

    @property
    def is_failure(self) -> bool:
        return self.severity is EventSeverity.ERROR

    def to_stream_fields(self) -> dict[str, str]:
        """Flatten for a Redis stream, which stores only string field values."""
        import json

        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "sequence": str(self.sequence),
            "type": self.type.value,
            "severity": self.severity.value,
            "node_id": self.node_id or "",
            "agent_id": self.agent_id or "",
            "tool": self.tool or "",
            "message": self.message[:1_000],
            "payload": json.dumps(self.payload, default=str)[:8_000],
            "created_at": self.created_at.isoformat(),
        }

    def describe(self) -> str:
        """Single-line human rendering, used by the CLI event log."""
        where = self.node_id or self.agent_id or self.tool or "-"
        return f"[{self.sequence:04d}] {self.type.value:<24} {where:<20} {self.message}"


class EventFilter(FrozenModel):
    """Query filter for reading persisted events."""

    types: frozenset[EventType] = Field(default_factory=frozenset)
    severities: frozenset[EventSeverity] = Field(default_factory=frozenset)
    node_id: Slug | None = None
    agent_id: Slug | None = None
    after_sequence: int | None = Field(default=None, ge=0)
    limit: int = Field(default=200, ge=1, le=10_000)

    def matches(self, event: ExecutionEvent) -> bool:
        """In-memory application of the filter, for the non-persistent event bus."""
        if self.types and event.type not in self.types:
            return False
        if self.severities and event.severity not in self.severities:
            return False
        if self.node_id and event.node_id != self.node_id:
            return False
        if self.agent_id and event.agent_id != self.agent_id:
            return False
        return not (self.after_sequence is not None and event.sequence <= self.after_sequence)
