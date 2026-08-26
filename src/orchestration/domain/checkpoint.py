"""Checkpoint model.

A checkpoint is an immutable snapshot of :class:`ExecutionState` plus the
workflow graph as it stood at that moment. Both halves are needed: a dynamically
replanned execution has a different graph than the one it started with, so
restoring only the state would resume against the wrong topology.

Idempotency is built in via :attr:`Checkpoint.content_hash`. Writing the same
logical checkpoint twice -- which happens when a process is killed between the
database write and the acknowledgement -- collapses to one row.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import Field

from orchestration.domain.base import FrozenModel, JsonDict, Slug, id_factory, utc_now
from orchestration.domain.enums import CheckpointReason, ExecutionStatus
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Workflow


class Checkpoint(FrozenModel):
    """An immutable, restorable snapshot of an execution.

    Attributes:
        sequence: Monotonic per-execution counter. Resume selects the highest
            sequence, and ``UNIQUE(execution_id, sequence)`` in the database is
            what prevents two workers from both appending a "next" checkpoint.
        content_hash: Digest of the meaningful payload, used to deduplicate
            identical consecutive writes.
        node_id: The node this checkpoint brackets, when applicable.
    """

    id: str = Field(default_factory=id_factory("checkpoint"))
    execution_id: str
    sequence: int = Field(ge=0)
    reason: CheckpointReason
    status: ExecutionStatus
    node_id: Slug | None = None
    state: ExecutionState
    #: The graph in force at snapshot time. Stored because replanning mutates it.
    workflow: Workflow
    content_hash: str = Field(default="", max_length=64)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: JsonDict = Field(default_factory=dict)

    def with_hash(self) -> Checkpoint:
        """Return a copy with :attr:`content_hash` computed."""
        return self.model_copy(update={"content_hash": self.compute_hash()})

    def compute_hash(self) -> str:
        """Stable digest over the payload that defines this checkpoint.

        Excludes the checkpoint's own id and timestamp -- otherwise every write
        would be unique by construction and deduplication could never fire. The
        state's ``updated_at`` and ``version`` are excluded for the same reason.
        """
        state = self.state.model_dump(mode="json")
        # Excluded because they are storage bookkeeping, not part of the logical
        # snapshot. `version` in particular is bumped by the *write itself*, so
        # including it would make every checkpoint hash unique and deduplication
        # could never fire.
        for bookkeeping in ("updated_at", "version"):
            state.pop(bookkeeping, None)
        payload = {
            "execution_id": self.execution_id,
            "sequence": self.sequence,
            "reason": self.reason.value,
            "status": self.status.value,
            "node_id": self.node_id,
            "state": state,
            "workflow": self.workflow.model_dump(mode="json"),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def is_equivalent_to(self, other: Checkpoint) -> bool:
        """Whether two checkpoints capture the same logical moment."""
        return self.compute_hash() == other.compute_hash()

    @property
    def is_resumable(self) -> bool:
        """Whether an execution can be restarted from this snapshot."""
        return self.status.is_resumable

    def summary(self) -> JsonDict:
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "sequence": self.sequence,
            "reason": self.reason.value,
            "status": self.status.value,
            "node_id": self.node_id,
            "created_at": self.created_at.isoformat(),
            "resumable": self.is_resumable,
            "content_hash": self.content_hash[:12],
        }
