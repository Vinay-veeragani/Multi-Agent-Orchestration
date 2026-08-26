"""Shared Pydantic base classes, identifier helpers, and time utilities.

Two conventions are enforced here rather than repeated in every model:

1. **Timezone-aware timestamps only.** Naive datetimes compare incorrectly
   against database values and silently corrupt duration arithmetic, so
   :func:`utc_now` is the single source of "now" and the ruff ``DTZ`` rules ban
   naive alternatives.
2. **Prefixed identifiers.** IDs carry a short type prefix (``exec_``, ``ckpt_``)
   so that a value appearing in a log line, trace, or error message is
   self-describing without a schema lookup.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Coerce a datetime to UTC, treating a naive value as already-UTC.

    Naive datetimes reach us from drivers and JSON payloads that dropped the
    offset; assuming UTC is correct here because every timestamp the engine
    writes is UTC by construction.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def duration_since(start: datetime) -> float:
    """Seconds elapsed from ``start`` until now."""
    return (utc_now() - ensure_utc(start)).total_seconds()


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

ID_PREFIXES: Final[dict[str, str]] = {
    "agent": "agt",
    "tool": "tol",
    "task": "tsk",
    "workflow": "wkf",
    "node": "nod",
    "edge": "edg",
    "execution": "exec",
    "checkpoint": "ckpt",
    "approval": "appr",
    "event": "evt",
    "agent_invocation": "ainv",
    "tool_invocation": "tinv",
    "artifact": "art",
    "evaluation": "eval",
    "trace": "trc",
}


def new_id(kind: str) -> str:
    """Generate a prefixed, sortable-enough unique identifier.

    Uses uuid4 hex rather than a monotonic scheme: identifiers are generated on
    multiple workers, and ordering is carried explicitly by ``sequence`` columns
    where it matters (checkpoints, events) instead of being inferred from an ID.
    """
    prefix = ID_PREFIXES.get(kind, kind[:4])
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def id_factory(kind: str) -> Callable[[], str]:
    """Return a zero-argument factory suitable for ``Field(default_factory=...)``."""

    def _factory() -> str:
        return new_id(kind)

    return _factory


#: A non-empty, trimmed identifier-like string.
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]

#: A slug used for registry keys: lowercase letters, digits, underscore, hyphen.
Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
]

#: Free-text bounded so a runaway LLM response cannot become an unbounded row.
BoundedText = Annotated[str, StringConstraints(max_length=200_000)]

#: A probability / confidence score.
Score = Annotated[float, Field(ge=0.0, le=1.0)]

#: An arbitrary JSON object payload.
JsonDict = dict[str, Any]


# ---------------------------------------------------------------------------
# Base models
# ---------------------------------------------------------------------------


class DomainModel(BaseModel):
    """Base class for domain objects.

    Configuration choices worth stating:

    ``extra="forbid"``
        An unexpected key is a bug or a hallucinated field, not something to
        silently accept. This is what makes LLM-produced payloads fail loudly.
    ``validate_assignment=True``
        Mutating a field re-runs validation, so an invalid state cannot be
        reached by assignment after construction.
    ``use_enum_values=False``
        Enum members are kept as members in Python; serialisation to strings
        happens at the JSON boundary via ``StrEnum``.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        str_strip_whitespace=True,
        frozen=False,
        populate_by_name=True,
        ser_json_timedelta="float",
    )

    def to_json_dict(self) -> JsonDict:
        """Serialise to a JSON-safe dict suitable for a JSONB column."""
        return self.model_dump(mode="json")

    def merged(self, **changes: Any) -> Self:
        """Return a validated copy with ``changes`` applied.

        Unlike ``model_copy(update=...)``, this re-validates, so an invalid
        update raises instead of producing a malformed object.
        """
        return type(self).model_validate({**self.model_dump(), **changes})


class FrozenModel(DomainModel):
    """Immutable domain object.

    Used for values that must not drift after creation -- recorded events,
    checkpoints, completed invocations. Immutability is what lets these be
    shared across concurrent tasks without defensive copying.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class TimestampedModel(DomainModel):
    """Domain object carrying creation and update times."""

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def touch(self) -> None:
        """Mark the object as modified now."""
        self.updated_at = utc_now()
