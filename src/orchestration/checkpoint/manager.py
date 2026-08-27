"""Checkpoint manager and execution resume.

Two responsibilities:

:meth:`CheckpointManager.write`
    Persist a snapshot *and* the state row it corresponds to, in one
    transaction. Doing them separately would allow a crash to leave a checkpoint
    that disagrees with the live state -- which is worse than having no
    checkpoint, because resume would trust it.

:meth:`CheckpointManager.resume`
    Reconstruct an execution from its newest resumable checkpoint. This is where
    the design pays off: because :class:`ExecutionState` holds *everything*
    needed to schedule -- node statuses, attempt counts, outputs, budget
    consumption -- resume is a load plus a re-entry into the ordinary executor
    loop. There is no separate recovery code path that could drift from the
    normal one.

Deduplication is by content hash, so the common case of "nothing changed since
the last checkpoint" does not accumulate identical rows, and a retried write
after a lost acknowledgement collapses to one.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestration.domain.base import JsonDict
from orchestration.domain.checkpoint import Checkpoint
from orchestration.domain.enums import CheckpointReason, EventType, ExecutionStatus
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Workflow
from orchestration.errors import (
    ConcurrencyConflictError,
    InvalidStateTransitionError,
    NotFoundError,
)
from orchestration.events.bus import EventBus
from orchestration.observability.tracing import checkpoint_span
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import (
    CheckpointRepository,
    EventRepository,
    ExecutionRepository,
    ExecutionStateRepository,
)


@dataclass(slots=True)
class ResumeContext:
    """Everything needed to restart an execution.

    Carries the workflow as it stood at checkpoint time, not the workflow as
    currently defined: a dynamically replanned execution must resume against the
    topology it was actually running.
    """

    state: ExecutionState
    workflow: Workflow
    checkpoint: Checkpoint
    #: Highest event sequence already recorded, so a fresh bus continues rather
    #: than restarting at zero and colliding.
    event_sequence: int
    #: Whether anything was actually restored, or this is a first start.
    restored: bool


class CheckpointManager:
    """Writes checkpoints and restores executions from them."""

    def __init__(
        self,
        database: Database,
        *,
        events: EventBus | None = None,
        dedupe: bool = True,
    ) -> None:
        self._db = database
        self._events = events
        self._dedupe = dedupe
        #: Last hash written per execution, so the common "nothing changed" case
        #: is skipped without a database round trip.
        self._last_hash: dict[str, str] = {}

    # -- writing -----------------------------------------------------------

    async def write(
        self,
        state: ExecutionState,
        workflow: Workflow,
        reason: CheckpointReason,
        node_id: str | None = None,
    ) -> Checkpoint | None:
        """Persist a checkpoint and the state it reflects, atomically.

        Returns the stored checkpoint, or ``None`` when the write was skipped as
        a duplicate.
        """
        candidate = Checkpoint(
            execution_id=state.execution_id,
            sequence=0,  # replaced below, inside the transaction
            reason=reason,
            status=state.status,
            node_id=node_id,
            state=state,
            workflow=workflow,
        )
        content_hash = candidate.compute_hash()

        if self._dedupe and self._last_hash.get(state.execution_id) == content_hash:
            # Identical to the previous write for this execution. Skipping keeps
            # a long run's checkpoint table proportional to actual progress.
            return None

        with checkpoint_span(state.execution_id, reason=reason.value):
            return await self._write_traced(
                state, workflow, reason, node_id, candidate, content_hash
            )

    async def _write_traced(
        self,
        state: ExecutionState,
        workflow: Workflow,
        reason: CheckpointReason,
        node_id: str | None,
        candidate: Checkpoint,
        content_hash: str,
    ) -> Checkpoint | None:
        async with self._db.transaction() as session:
            checkpoints = CheckpointRepository(session)
            states = ExecutionStateRepository(session)
            executions = ExecutionRepository(session)

            sequence = await checkpoints.next_sequence(state.execution_id)
            stamped = candidate.model_copy(
                update={"sequence": sequence, "content_hash": content_hash}
            )
            stored = await checkpoints.append(stamped)

            # Same transaction: a checkpoint that disagrees with live state is
            # worse than none, because resume would believe it.
            await states.save(state, workflow)
            await executions.update_status(
                state.execution_id,
                state.status,
                cost_usd=state.budget_usage.cost_usd,
                total_tokens=state.budget_usage.total_tokens,
                started_at=state.started_at,
                completed_at=state.completed_at,
            )

        self._last_hash[state.execution_id] = content_hash

        if self._events is not None:
            await self._events.emit(
                EventType.CHECKPOINT_CREATED,
                execution_id=state.execution_id,
                message=f"checkpoint {stored.sequence} ({reason.value})",
                node_id=node_id,
                payload={
                    "sequence": stored.sequence,
                    "reason": reason.value,
                    "content_hash": stored.content_hash[:12],
                },
            )
        return stored

    def writer(self) -> object:
        """Return a callable matching the executor's ``CheckpointWriter``."""

        async def _write(
            state: ExecutionState,
            workflow: Workflow,
            reason: CheckpointReason,
            node_id: str | None,
        ) -> None:
            await self.write(state, workflow, reason, node_id)

        return _write

    # -- reading -----------------------------------------------------------

    async def latest(self, execution_id: str) -> Checkpoint | None:
        async with self._db.session() as session:
            return await CheckpointRepository(session).latest(execution_id)

    async def history(self, execution_id: str, *, limit: int = 100) -> list[JsonDict]:
        async with self._db.session() as session:
            return await CheckpointRepository(session).history(execution_id, limit=limit)

    # -- resume ------------------------------------------------------------

    async def resume(self, execution_id: str) -> ResumeContext:
        """Reconstruct an execution from its newest resumable checkpoint.

        Prefers the live state row over the checkpoint when both exist and the
        state row is at least as advanced. The state row is written in the same
        transaction as the checkpoint, so it is never behind -- but it can be
        *ahead* if the process died after a state write and before the next
        checkpoint, and resuming from the more advanced position avoids redoing
        completed work.

        Raises:
            NotFoundError: No persisted state or checkpoint for this execution.
            InvalidStateTransitionError: The execution already reached a terminal
                status, so there is nothing to resume.
        """
        async with self._db.session() as session:
            checkpoints = CheckpointRepository(session)
            states = ExecutionStateRepository(session)
            events = EventRepository(session)

            checkpoint = await checkpoints.latest_resumable(execution_id)
            live = await states.try_load(execution_id)
            event_sequence = await events.max_sequence(execution_id)

        if checkpoint is None and live is None:
            raise NotFoundError(
                f"execution {execution_id!r} has no persisted state to resume from",
                execution=execution_id,
            )

        if live is not None:
            state, workflow = live
            if state.status.is_terminal:
                raise InvalidStateTransitionError(
                    f"execution {execution_id!r} already finished with status "
                    f"{state.status.value}; there is nothing to resume",
                    execution=execution_id,
                    status=state.status.value,
                )
            if checkpoint is None:
                # State without a checkpoint: unusual but recoverable. Synthesise
                # one so the caller always has a checkpoint to reason about.
                checkpoint = Checkpoint(
                    execution_id=execution_id,
                    sequence=0,
                    reason=CheckpointReason.EXECUTION_STARTED,
                    status=state.status,
                    state=state,
                    workflow=workflow,
                ).with_hash()
            return ResumeContext(
                state=state,
                workflow=workflow,
                checkpoint=checkpoint,
                event_sequence=event_sequence,
                restored=True,
            )

        assert checkpoint is not None
        if checkpoint.status.is_terminal:
            raise InvalidStateTransitionError(
                f"execution {execution_id!r} already finished; nothing to resume",
                execution=execution_id,
                status=checkpoint.status.value,
            )

        return ResumeContext(
            state=checkpoint.state,
            workflow=checkpoint.workflow,
            checkpoint=checkpoint,
            event_sequence=event_sequence,
            restored=True,
        )

    async def resume_from_sequence(self, execution_id: str, sequence: int) -> ResumeContext:
        """Restore a specific checkpoint.

        Exposed for operator recovery: when the newest checkpoint is itself the
        problem, rewinding to an earlier one is the escape hatch.
        """
        async with self._db.session() as session:
            checkpoint = await CheckpointRepository(session).find_by_sequence(
                execution_id, sequence
            )
            event_sequence = await EventRepository(session).max_sequence(execution_id)

        if checkpoint is None:
            raise NotFoundError(
                f"no checkpoint {sequence} for execution {execution_id!r}",
                execution=execution_id,
                sequence=sequence,
            )
        return ResumeContext(
            state=checkpoint.state,
            workflow=checkpoint.workflow,
            checkpoint=checkpoint,
            event_sequence=event_sequence,
            restored=True,
        )

    async def claim_for_resume(self, execution_id: str) -> bool:
        """Attempt to take exclusive ownership of an execution for resume.

        Uses a transaction-scoped PostgreSQL advisory lock. Two workers both
        finding the same stranded execution is the expected case after a restart;
        this makes only one of them proceed.

        The lock is released when the transaction ends, so this returns whether
        the claim *succeeded*, and the caller must then re-verify state under
        optimistic concurrency for the actual work. Holding a lock for a whole
        multi-minute execution would be worse: a crashed worker would block the
        execution until its connection timed out.
        """
        async with self._db.session() as session:
            return await ExecutionStateRepository(session).acquire_advisory_lock(execution_id)

    async def find_resumable(self, *, limit: int = 20) -> list[str]:
        """Executions a crashed process may have left stranded."""
        async with self._db.session() as session:
            return await ExecutionRepository(session).find_resumable(limit=limit)

    # -- state persistence -------------------------------------------------

    async def save_state(
        self, state: ExecutionState, workflow: Workflow, *, check_version: bool = True
    ) -> int:
        """Persist state, optionally enforcing optimistic concurrency.

        Raises:
            ConcurrencyConflictError: When ``check_version`` is set and another
                writer advanced the execution first.
        """
        async with self._db.transaction() as session:
            return await ExecutionStateRepository(session).save(
                state, workflow, expected_version=state.version if check_version else None
            )

    async def load_state(self, execution_id: str) -> tuple[ExecutionState, Workflow]:
        async with self._db.session() as session:
            return await ExecutionStateRepository(session).load(execution_id)

    def forget(self, execution_id: str) -> None:
        """Drop the cached hash for an execution.

        Called when an execution finishes, so the cache does not grow with every
        run a long-lived process handles.
        """
        self._last_hash.pop(execution_id, None)


async def resume_execution(
    manager: CheckpointManager, execution_id: str, *, require_claim: bool = True
) -> ResumeContext:
    """Claim and restore an execution.

    The convenience entry point the CLI and API use.

    Raises:
        ConcurrencyConflictError: When the claim fails because another worker
            already owns this execution. Retryable by classification, which is
            correct: the caller should try a different execution or wait.
    """
    if require_claim and not await manager.claim_for_resume(execution_id):
        raise ConcurrencyConflictError(
            f"execution {execution_id!r} is being resumed by another worker",
            execution=execution_id,
        )
    return await manager.resume(execution_id)


async def restore_status_for_resume(state: ExecutionState) -> None:
    """Put a restored state back into a runnable status.

    A state restored from ``WAITING_FOR_APPROVAL`` or a stranded ``RUNNING``
    needs to re-enter ``RUNNING``, and the transition table permits both. Done
    here rather than in the executor so the executor's own entry conditions stay
    simple.
    """
    if (
        state.status is ExecutionStatus.WAITING_FOR_APPROVAL
        or state.status is ExecutionStatus.PENDING
    ):
        state.transition_to(ExecutionStatus.RUNNING)
