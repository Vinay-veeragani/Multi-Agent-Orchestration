"""Repositories: the only code that reads and writes the database.

Each repository takes an :class:`AsyncSession` rather than owning one, so the
caller controls transaction boundaries. That matters for the checkpoint path,
where the snapshot and the state update must land atomically.

Three behaviours here carry real weight:

:meth:`ExecutionStateRepository.save`
    Optimistic concurrency. The writer supplies the version it read; the update
    matches on it. Two processes resuming the same execution cannot both advance
    it -- the loser gets a :class:`ConcurrencyConflictError` and backs off.

:meth:`CheckpointRepository.append`
    Idempotent. A duplicate ``(execution_id, sequence)`` or a repeat of the same
    ``content_hash`` returns the existing row instead of raising. This is what
    makes a process killed between the write and the acknowledgement safe.

:meth:`ToolInvocationRepository.claim`
    The side-effect guard. The key is written *before* the tool runs; if it is
    already present and completed, the call is not repeated on resume.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestration.domain.agent import AgentDefinition, AgentInvocation
from orchestration.domain.approval import ApprovalRequest
from orchestration.domain.base import JsonDict, utc_now
from orchestration.domain.checkpoint import Checkpoint
from orchestration.domain.enums import ApprovalStatus, ExecutionStatus
from orchestration.domain.events import EventFilter, ExecutionEvent
from orchestration.domain.execution import ExecutionState
from orchestration.domain.tool import ToolInvocation, ToolSpec
from orchestration.domain.workflow import Workflow
from orchestration.errors import ConcurrencyConflictError, NotFoundError
from orchestration.persistence.database import is_unique_violation
from orchestration.persistence.tables import (
    AgentInvocationRow,
    AgentRow,
    ApprovalRow,
    CheckpointRow,
    ExecutionEventRow,
    ExecutionRow,
    ExecutionStateRow,
    ToolInvocationRow,
    ToolRow,
    WorkflowEdgeRow,
    WorkflowNodeRow,
    WorkflowRow,
)


def _affected_rows(result: Any) -> int:
    """Rows touched by a DELETE/UPDATE.

    ``Result`` is the declared return type of ``execute``, but DML actually
    returns a ``CursorResult`` which carries ``rowcount``. Reading it through one
    helper keeps that narrowing in a single place rather than scattering casts.
    """
    return int(getattr(result, "rowcount", 0) or 0)


class AgentRepository:
    """Persistence for agent definitions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, definition: AgentDefinition) -> AgentDefinition:
        """Insert or replace a definition.

        An upsert rather than insert-or-fail: registering an agent over the API
        is naturally idempotent, and a client retrying a request should not get a
        conflict for sending the same definition twice.
        """
        payload = definition.model_dump(mode="json")
        statement = (
            pg_insert(AgentRow)
            .values(
                id=definition.id,
                name=definition.name,
                kind=definition.kind,
                enabled=definition.enabled,
                version=definition.version,
                definition=payload,
            )
            .on_conflict_do_update(
                index_elements=[AgentRow.id],
                set_={
                    "name": definition.name,
                    "kind": definition.kind,
                    "enabled": definition.enabled,
                    "version": definition.version,
                    "definition": payload,
                    "updated_at": utc_now(),
                },
            )
        )
        await self._session.execute(statement)
        return definition

    async def get(self, agent_id: str) -> AgentDefinition:
        row = await self._session.get(AgentRow, agent_id)
        if row is None:
            raise NotFoundError(f"agent {agent_id!r} is not persisted", agent=agent_id)
        return AgentDefinition.model_validate(row.definition)

    async def try_get(self, agent_id: str) -> AgentDefinition | None:
        row = await self._session.get(AgentRow, agent_id)
        return AgentDefinition.model_validate(row.definition) if row else None

    async def list_all(self, *, include_disabled: bool = False) -> list[AgentDefinition]:
        statement = select(AgentRow).order_by(AgentRow.id)
        if not include_disabled:
            statement = statement.where(AgentRow.enabled.is_(True))
        rows = (await self._session.execute(statement)).scalars().all()
        return [AgentDefinition.model_validate(r.definition) for r in rows]

    async def delete(self, agent_id: str) -> None:
        result = await self._session.execute(delete(AgentRow).where(AgentRow.id == agent_id))
        if _affected_rows(result) == 0:
            raise NotFoundError(f"agent {agent_id!r} is not persisted", agent=agent_id)


class ToolRepository:
    """Persistence for tool specifications."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, spec: ToolSpec, *, enabled: bool) -> ToolSpec:
        payload = spec.model_dump(mode="json")
        statement = (
            pg_insert(ToolRow)
            .values(
                name=spec.name,
                risk=spec.risk.value,
                enabled=enabled,
                requires_approval=spec.requires_approval,
                version=spec.version,
                spec=payload,
            )
            .on_conflict_do_update(
                index_elements=[ToolRow.name],
                set_={
                    "risk": spec.risk.value,
                    "enabled": enabled,
                    "requires_approval": spec.requires_approval,
                    "version": spec.version,
                    "spec": payload,
                },
            )
        )
        await self._session.execute(statement)
        return spec

    async def list_all(self) -> list[ToolSpec]:
        rows = (await self._session.execute(select(ToolRow).order_by(ToolRow.name))).scalars().all()
        return [ToolSpec.model_validate(r.spec) for r in rows]


class WorkflowRepository:
    """Persistence for workflow definitions.

    Writes both the JSONB document (what executes) and the relational node/edge
    rows (what is queryable). The duplication is deliberate and documented in
    :mod:`orchestration.persistence.tables`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, workflow: Workflow) -> Workflow:
        payload = workflow.model_dump(mode="json")
        statement = (
            pg_insert(WorkflowRow)
            .values(
                id=workflow.id,
                name=workflow.name,
                version=workflow.version,
                dynamic=workflow.dynamic,
                definition=payload,
            )
            .on_conflict_do_update(
                index_elements=[WorkflowRow.id],
                set_={
                    "name": workflow.name,
                    "version": workflow.version,
                    "dynamic": workflow.dynamic,
                    "definition": payload,
                    "updated_at": utc_now(),
                },
            )
        )
        await self._session.execute(statement)

        # Replace the projected rows wholesale: a workflow's graph changes as a
        # unit, and diffing nodes and edges would be more code for no benefit.
        await self._session.execute(
            delete(WorkflowNodeRow).where(WorkflowNodeRow.workflow_id == workflow.id)
        )
        await self._session.execute(
            delete(WorkflowEdgeRow).where(WorkflowEdgeRow.workflow_id == workflow.id)
        )
        for node in workflow.nodes:
            self._session.add(
                WorkflowNodeRow(
                    workflow_id=workflow.id,
                    node_id=node.id,
                    kind=node.kind.value,
                    agent_id=node.agent_id,
                    tool=node.tool,
                    definition=node.model_dump(mode="json"),
                )
            )
        for edge in workflow.edges:
            self._session.add(
                WorkflowEdgeRow(
                    workflow_id=workflow.id,
                    edge_id=edge.id,
                    source=edge.source,
                    target=edge.target,
                    conditional=edge.is_conditional,
                    definition=edge.model_dump(mode="json"),
                )
            )
        return workflow

    async def get(self, workflow_id: str) -> Workflow:
        row = await self._session.get(WorkflowRow, workflow_id)
        if row is None:
            raise NotFoundError(f"workflow {workflow_id!r} is not persisted", workflow=workflow_id)
        return Workflow.model_validate(row.definition)

    async def list_all(self, *, limit: int = 100) -> list[Workflow]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowRow).order_by(WorkflowRow.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [Workflow.model_validate(r.definition) for r in rows]

    async def using_agent(self, agent_id: str) -> list[str]:
        """Workflow ids referencing ``agent_id``.

        The reason the relational projection exists: answering this from JSONB
        alone would mean scanning every definition.
        """
        rows = (
            (
                await self._session.execute(
                    select(WorkflowNodeRow.workflow_id)
                    .where(WorkflowNodeRow.agent_id == agent_id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


class ExecutionRepository:
    """Persistence for execution headers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        execution_id: str,
        workflow_id: str,
        task_description: str,
        status: ExecutionStatus = ExecutionStatus.PENDING,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: JsonDict | None = None,
    ) -> str:
        """Create a header, returning the execution id that is now authoritative.

        When ``idempotency_key`` collides with an existing execution, the
        *existing* id is returned rather than raising. That is what makes a
        retried ``POST /executions`` safe: the caller gets the run it already
        started instead of a second one.
        """
        self._session.add(
            ExecutionRow(
                id=execution_id,
                workflow_id=workflow_id,
                status=status.value,
                task_description=task_description,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                metadata_json=metadata or {},
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if not (idempotency_key and is_unique_violation(exc)):
                raise
            await self._session.rollback()
            existing = await self.find_by_idempotency_key(idempotency_key)
            if existing is None:  # pragma: no cover - the row must exist
                raise
            return existing
        return execution_id

    async def find_by_idempotency_key(self, key: str) -> str | None:
        result = await self._session.execute(
            select(ExecutionRow.id).where(ExecutionRow.idempotency_key == key)
        )
        row = result.scalar_one_or_none()
        return str(row) if row else None

    async def update_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        *,
        cost_usd: float | None = None,
        total_tokens: int | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status.value, "updated_at": utc_now()}
        if cost_usd is not None:
            values["cost_usd"] = cost_usd
        if total_tokens is not None:
            values["total_tokens"] = total_tokens
        if started_at is not None:
            values["started_at"] = started_at
        if completed_at is not None:
            values["completed_at"] = completed_at
        await self._session.execute(
            update(ExecutionRow).where(ExecutionRow.id == execution_id).values(**values)
        )

    async def get_status(self, execution_id: str) -> ExecutionStatus:
        result = await self._session.execute(
            select(ExecutionRow.status).where(ExecutionRow.id == execution_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"execution {execution_id!r} not found", execution=execution_id)
        return ExecutionStatus(row)

    async def list_recent(
        self, *, limit: int = 50, status: ExecutionStatus | None = None
    ) -> list[JsonDict]:
        statement = select(ExecutionRow).order_by(ExecutionRow.created_at.desc()).limit(limit)
        if status is not None:
            statement = statement.where(ExecutionRow.status == status.value)
        rows = (await self._session.execute(statement)).scalars().all()
        return [
            {
                "id": r.id,
                "workflow_id": r.workflow_id,
                "status": r.status,
                "task": r.task_description[:200],
                "cost_usd": r.cost_usd,
                "total_tokens": r.total_tokens,
                "created_at": r.created_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in rows
        ]

    async def find_resumable(self, *, limit: int = 20) -> list[str]:
        """Executions a crashed process may have left stranded.

        Selected by status rather than by a heartbeat: an execution left
        ``RUNNING`` with no live worker is exactly the case resume exists for.
        """
        resumable = [
            ExecutionStatus.PENDING.value,
            ExecutionStatus.RUNNING.value,
            ExecutionStatus.WAITING_FOR_APPROVAL.value,
        ]
        rows = (
            (
                await self._session.execute(
                    select(ExecutionRow.id)
                    .where(ExecutionRow.status.in_(resumable))
                    .order_by(ExecutionRow.updated_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


class ExecutionStateRepository:
    """Persistence for the durable state document."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self, state: ExecutionState, workflow: Workflow, *, expected_version: int | None = None
    ) -> int:
        """Insert or update state, enforcing optimistic concurrency.

        Args:
            expected_version: The version the caller read. When supplied and the
                stored version differs, the write is refused.

        Returns:
            The new version.

        Raises:
            ConcurrencyConflictError: If another writer advanced the state first.
                Retryable by classification: the caller should re-read and retry,
                which is what the resume path does.
        """
        payload = state.model_dump(mode="json")
        snapshot = workflow.model_dump(mode="json")
        next_version = (expected_version if expected_version is not None else state.version) + 1

        if expected_version is None:
            statement = (
                pg_insert(ExecutionStateRow)
                .values(
                    execution_id=state.execution_id,
                    version=next_version,
                    status=state.status.value,
                    state=payload,
                    workflow_snapshot=snapshot,
                )
                .on_conflict_do_update(
                    index_elements=[ExecutionStateRow.execution_id],
                    set_={
                        "version": ExecutionStateRow.version + 1,
                        "status": state.status.value,
                        "state": payload,
                        "workflow_snapshot": snapshot,
                        "updated_at": utc_now(),
                    },
                )
                .returning(ExecutionStateRow.version)
            )
            result = await self._session.execute(statement)
            version = int(result.scalar_one())
            state.version = version
            return version

        result = await self._session.execute(
            update(ExecutionStateRow)
            .where(
                ExecutionStateRow.execution_id == state.execution_id,
                ExecutionStateRow.version == expected_version,
            )
            .values(
                version=next_version,
                status=state.status.value,
                state=payload,
                workflow_snapshot=snapshot,
                updated_at=utc_now(),
            )
            .returning(ExecutionStateRow.version)
        )
        version_row = result.scalar_one_or_none()
        if version_row is None:
            current = await self.current_version(state.execution_id)
            raise ConcurrencyConflictError(
                "execution state was modified by another writer",
                execution=state.execution_id,
                expected_version=expected_version,
                actual_version=current,
            )
        state.version = int(version_row)
        return state.version

    async def load(self, execution_id: str) -> tuple[ExecutionState, Workflow]:
        row = await self._session.get(ExecutionStateRow, execution_id)
        if row is None:
            raise NotFoundError(
                f"no persisted state for execution {execution_id!r}", execution=execution_id
            )
        state = ExecutionState.model_validate(row.state)
        state.version = row.version
        return state, Workflow.model_validate(row.workflow_snapshot)

    async def try_load(self, execution_id: str) -> tuple[ExecutionState, Workflow] | None:
        row = await self._session.get(ExecutionStateRow, execution_id)
        if row is None:
            return None
        state = ExecutionState.model_validate(row.state)
        state.version = row.version
        return state, Workflow.model_validate(row.workflow_snapshot)

    async def current_version(self, execution_id: str) -> int | None:
        result = await self._session.execute(
            select(ExecutionStateRow.version).where(ExecutionStateRow.execution_id == execution_id)
        )
        row = result.scalar_one_or_none()
        return int(row) if row is not None else None

    async def acquire_advisory_lock(self, execution_id: str) -> bool:
        """Take a transaction-scoped advisory lock on this execution.

        Belt and braces alongside optimistic concurrency: the lock stops two
        workers *starting* concurrent work on the same execution, while the
        version check catches anything that slips past. Released automatically
        when the transaction ends, so a crashed worker cannot hold it.
        """
        result = await self._session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
            {"key": f"execution:{execution_id}"},
        )
        return bool(result.scalar_one())


class CheckpointRepository:
    """Append-only checkpoint storage with idempotent writes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, checkpoint: Checkpoint) -> Checkpoint:
        """Persist a checkpoint idempotently.

        A repeat of the same ``(execution_id, sequence)`` or the same
        ``content_hash`` returns the stored row rather than raising. This is what
        makes a process killed between the database write and the acknowledgement
        safe to retry.
        """
        stamped = checkpoint if checkpoint.content_hash else checkpoint.with_hash()

        existing = await self.find_by_hash(stamped.execution_id, stamped.content_hash)
        if existing is not None:
            return existing

        self._session.add(
            CheckpointRow(
                id=stamped.id,
                execution_id=stamped.execution_id,
                sequence=stamped.sequence,
                reason=stamped.reason.value,
                status=stamped.status.value,
                node_id=stamped.node_id,
                content_hash=stamped.content_hash,
                state=stamped.state.model_dump(mode="json"),
                workflow_snapshot=stamped.workflow.model_dump(mode="json"),
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if not is_unique_violation(exc):
                raise
            await self._session.rollback()
            # Lost a race on (execution_id, sequence). The other writer's row is
            # equally valid, so adopt it rather than failing the execution.
            duplicate = await self.find_by_sequence(stamped.execution_id, stamped.sequence)
            if duplicate is None:  # pragma: no cover - the row must exist
                raise
            return duplicate
        return stamped

    async def next_sequence(self, execution_id: str) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(CheckpointRow.sequence), -1)).where(
                CheckpointRow.execution_id == execution_id
            )
        )
        return int(result.scalar_one()) + 1

    async def find_by_hash(self, execution_id: str, content_hash: str) -> Checkpoint | None:
        result = await self._session.execute(
            select(CheckpointRow).where(
                CheckpointRow.execution_id == execution_id,
                CheckpointRow.content_hash == content_hash,
            )
        )
        row = result.scalars().first()
        return self._to_domain(row) if row else None

    async def find_by_sequence(self, execution_id: str, sequence: int) -> Checkpoint | None:
        result = await self._session.execute(
            select(CheckpointRow).where(
                CheckpointRow.execution_id == execution_id,
                CheckpointRow.sequence == sequence,
            )
        )
        row = result.scalars().first()
        return self._to_domain(row) if row else None

    async def latest(self, execution_id: str) -> Checkpoint | None:
        result = await self._session.execute(
            select(CheckpointRow)
            .where(CheckpointRow.execution_id == execution_id)
            .order_by(CheckpointRow.sequence.desc())
            .limit(1)
        )
        row = result.scalars().first()
        return self._to_domain(row) if row else None

    async def latest_resumable(self, execution_id: str) -> Checkpoint | None:
        """The newest checkpoint an execution can actually restart from.

        A checkpoint taken in a terminal status is history, not a restart point,
        so resume walks back to the newest resumable one instead of taking the
        newest outright.
        """
        resumable = [
            ExecutionStatus.PENDING.value,
            ExecutionStatus.RUNNING.value,
            ExecutionStatus.WAITING_FOR_APPROVAL.value,
        ]
        result = await self._session.execute(
            select(CheckpointRow)
            .where(
                CheckpointRow.execution_id == execution_id,
                CheckpointRow.status.in_(resumable),
            )
            .order_by(CheckpointRow.sequence.desc())
            .limit(1)
        )
        row = result.scalars().first()
        return self._to_domain(row) if row else None

    async def history(self, execution_id: str, *, limit: int = 100) -> list[JsonDict]:
        """Checkpoint summaries, without the state blobs.

        Summaries only: a full history with every state document attached would
        be megabytes for a long run, and the API rarely needs the payloads.
        """
        rows = (
            (
                await self._session.execute(
                    select(
                        CheckpointRow.id,
                        CheckpointRow.sequence,
                        CheckpointRow.reason,
                        CheckpointRow.status,
                        CheckpointRow.node_id,
                        CheckpointRow.content_hash,
                        CheckpointRow.created_at,
                    )
                    .where(CheckpointRow.execution_id == execution_id)
                    .order_by(CheckpointRow.sequence.asc())
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "id": r["id"],
                "sequence": r["sequence"],
                "reason": r["reason"],
                "status": r["status"],
                "node_id": r["node_id"],
                "content_hash": r["content_hash"][:12],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    @staticmethod
    def _to_domain(row: CheckpointRow) -> Checkpoint:
        return Checkpoint(
            id=row.id,
            execution_id=row.execution_id,
            sequence=row.sequence,
            reason=row.reason,  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            node_id=row.node_id,
            content_hash=row.content_hash,
            state=ExecutionState.model_validate(row.state),
            workflow=Workflow.model_validate(row.workflow_snapshot),
            created_at=row.created_at,
        )


class EventRepository:
    """Append-only event storage."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: ExecutionEvent) -> None:
        self._session.add(
            ExecutionEventRow(
                id=event.id,
                execution_id=event.execution_id,
                sequence=event.sequence,
                type=event.type.value,
                severity=event.severity.value,
                node_id=event.node_id,
                agent_id=event.agent_id,
                tool=event.tool,
                message=event.message,
                payload=event.payload,
                trace_id=event.trace_id,
                span_id=event.span_id,
                created_at=event.created_at,
            )
        )

    async def append_many(self, events: Sequence[ExecutionEvent]) -> None:
        for event in events:
            await self.append(event)

    async def query(self, execution_id: str, event_filter: EventFilter) -> list[ExecutionEvent]:
        statement = select(ExecutionEventRow).where(ExecutionEventRow.execution_id == execution_id)
        if event_filter.types:
            statement = statement.where(
                ExecutionEventRow.type.in_([t.value for t in event_filter.types])
            )
        if event_filter.severities:
            statement = statement.where(
                ExecutionEventRow.severity.in_([s.value for s in event_filter.severities])
            )
        if event_filter.node_id:
            statement = statement.where(ExecutionEventRow.node_id == event_filter.node_id)
        if event_filter.agent_id:
            statement = statement.where(ExecutionEventRow.agent_id == event_filter.agent_id)
        if event_filter.after_sequence is not None:
            statement = statement.where(ExecutionEventRow.sequence > event_filter.after_sequence)
        statement = statement.order_by(ExecutionEventRow.sequence.asc()).limit(event_filter.limit)

        rows = (await self._session.execute(statement)).scalars().all()
        return [
            ExecutionEvent(
                id=r.id,
                execution_id=r.execution_id,
                sequence=r.sequence,
                type=r.type,  # type: ignore[arg-type]
                severity=r.severity,  # type: ignore[arg-type]
                node_id=r.node_id,
                agent_id=r.agent_id,
                tool=r.tool,
                message=r.message,
                payload=r.payload,
                trace_id=r.trace_id,
                span_id=r.span_id,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def max_sequence(self, execution_id: str) -> int:
        """Highest sequence recorded, so a resumed bus continues the numbering."""
        result = await self._session.execute(
            select(func.coalesce(func.max(ExecutionEventRow.sequence), 0)).where(
                ExecutionEventRow.execution_id == execution_id
            )
        )
        return int(result.scalar_one())

    async def count_by_type(self, execution_id: str) -> dict[str, int]:
        rows = (
            (
                await self._session.execute(
                    select(ExecutionEventRow.type, func.count())
                    .where(ExecutionEventRow.execution_id == execution_id)
                    .group_by(ExecutionEventRow.type)
                )
            )
            .tuples()
            .all()
        )
        return {str(name): int(count) for name, count in rows}


class InvocationRepository:
    """Persistence for agent and tool invocation records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_agent(self, invocation: AgentInvocation) -> None:
        self._session.add(
            AgentInvocationRow(
                id=invocation.id,
                execution_id=invocation.execution_id,
                node_id=invocation.node_id,
                agent_id=invocation.agent_id,
                attempt=invocation.attempt,
                status=invocation.status.value,
                model_key=invocation.model_key,
                input_tokens=invocation.input_tokens,
                output_tokens=invocation.output_tokens,
                cost_usd=invocation.cost_usd,
                tool_calls=invocation.tool_calls,
                iterations=invocation.iterations,
                duration_seconds=invocation.duration_seconds,
                confidence=invocation.output.confidence if invocation.output else None,
                payload={
                    "error": invocation.error,
                    "trace_id": invocation.trace_id,
                },
            )
        )

    async def claim_tool(self, invocation: ToolInvocation) -> bool:
        """Reserve an idempotency key before a tool runs.

        Returns ``True`` if this caller owns the invocation, ``False`` if the key
        already exists -- meaning the side effect already happened and must not be
        repeated. Written before the call, not after, which is the whole point.
        """
        self._session.add(
            ToolInvocationRow(
                id=invocation.id,
                execution_id=invocation.execution_id,
                node_id=invocation.node_id,
                agent_id=invocation.agent_id,
                tool=invocation.tool,
                attempt=invocation.attempt,
                status=invocation.status.value,
                policy_effect=invocation.policy_effect.value,
                approval_id=invocation.approval_id,
                idempotency_key=invocation.idempotency_key,
                arguments=invocation.arguments,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if not is_unique_violation(exc):
                raise
            await self._session.rollback()
            return False
        return True

    async def complete_tool(
        self,
        idempotency_key: str,
        *,
        status: str,
        result: JsonDict | None = None,
        error: JsonDict | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        await self._session.execute(
            update(ToolInvocationRow)
            .where(ToolInvocationRow.idempotency_key == idempotency_key)
            .values(
                status=status,
                result=result,
                error=error,
                duration_seconds=duration_seconds,
                completed_at=utc_now(),
            )
        )

    async def find_completed_tool(self, idempotency_key: str) -> JsonDict | None:
        """The recorded result of an already-completed invocation, if any.

        Consulted on resume: a tool whose side effect already happened returns
        its stored result instead of running again.
        """
        result = await self._session.execute(
            select(ToolInvocationRow).where(
                ToolInvocationRow.idempotency_key == idempotency_key,
                ToolInvocationRow.status == "succeeded",
            )
        )
        row = result.scalars().first()
        return dict(row.result or {}) if row else None

    async def agent_invocations(self, execution_id: str) -> list[JsonDict]:
        rows = (
            (
                await self._session.execute(
                    select(AgentInvocationRow)
                    .where(AgentInvocationRow.execution_id == execution_id)
                    .order_by(AgentInvocationRow.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "node_id": r.node_id,
                "agent_id": r.agent_id,
                "attempt": r.attempt,
                "status": r.status,
                "model_key": r.model_key,
                "tokens": r.input_tokens + r.output_tokens,
                "cost_usd": r.cost_usd,
                "tool_calls": r.tool_calls,
                "duration_seconds": r.duration_seconds,
                "confidence": r.confidence,
            }
            for r in rows
        ]

    async def tool_invocations(self, execution_id: str) -> list[JsonDict]:
        rows = (
            (
                await self._session.execute(
                    select(ToolInvocationRow)
                    .where(ToolInvocationRow.execution_id == execution_id)
                    .order_by(ToolInvocationRow.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "node_id": r.node_id,
                "agent_id": r.agent_id,
                "tool": r.tool,
                "attempt": r.attempt,
                "status": r.status,
                "policy_effect": r.policy_effect,
                "duration_seconds": r.duration_seconds,
            }
            for r in rows
        ]


class ApprovalRepository:
    """Persistence for approval requests."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        self._session.add(
            ApprovalRow(
                id=request.id,
                execution_id=request.execution_id,
                node_id=request.node_id,
                action=request.action,
                agent_id=request.agent_id,
                tool=request.tool,
                risk_level=request.risk_level.value,
                risk_reason=request.risk_reason,
                status=request.status.value,
                parameters=request.parameters,
                requested_at=request.requested_at,
                expires_at=request.expires_at,
            )
        )
        await self._session.flush()
        return request

    async def get(self, approval_id: str) -> ApprovalRequest:
        row = await self._session.get(ApprovalRow, approval_id)
        if row is None:
            raise NotFoundError(f"approval {approval_id!r} not found", approval=approval_id)
        return self._to_domain(row)

    async def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        decided_by: str,
        note: str | None = None,
        modified: JsonDict | None = None,
    ) -> ApprovalRequest:
        """Record a decision, refusing to overwrite a conflicting one.

        The ``status == PENDING`` predicate in the WHERE clause is what makes this
        safe under concurrency: two reviewers clicking approve and reject at the
        same instant cannot both win.
        """
        result = await self._session.execute(
            update(ApprovalRow)
            .where(
                ApprovalRow.id == approval_id,
                ApprovalRow.status == ApprovalStatus.PENDING.value,
            )
            .values(
                status=status.value,
                decided_by=decided_by,
                decision_note=note,
                modified_parameters=modified,
                decided_at=utc_now(),
            )
            .returning(ApprovalRow.id)
        )
        if result.scalar_one_or_none() is None:
            existing = await self.get(approval_id)
            if existing.status is status:
                # Same decision applied twice: idempotent, not an error.
                return existing
            from orchestration.errors import InvalidStateTransitionError

            raise InvalidStateTransitionError(
                f"approval {approval_id} is already {existing.status.value}",
                approval_id=approval_id,
                current=existing.status.value,
                requested=status.value,
            )
        return await self.get(approval_id)

    async def pending_for(self, execution_id: str) -> list[ApprovalRequest]:
        rows = (
            (
                await self._session.execute(
                    select(ApprovalRow)
                    .where(
                        ApprovalRow.execution_id == execution_id,
                        ApprovalRow.status == ApprovalStatus.PENDING.value,
                    )
                    .order_by(ApprovalRow.requested_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [self._to_domain(r) for r in rows]

    async def expire_overdue(self) -> int:
        """Mark expired pending approvals, returning how many were changed."""
        result = await self._session.execute(
            update(ApprovalRow)
            .where(
                ApprovalRow.status == ApprovalStatus.PENDING.value,
                ApprovalRow.expires_at.is_not(None),
                ApprovalRow.expires_at < utc_now(),
            )
            .values(status=ApprovalStatus.EXPIRED.value, decided_at=utc_now())
        )
        return _affected_rows(result)

    @staticmethod
    def _to_domain(row: ApprovalRow) -> ApprovalRequest:
        return ApprovalRequest(
            id=row.id,
            execution_id=row.execution_id,
            node_id=row.node_id,
            action=row.action,
            agent_id=row.agent_id,
            tool=row.tool,
            risk_level=row.risk_level,  # type: ignore[arg-type]
            risk_reason=row.risk_reason,
            status=row.status,  # type: ignore[arg-type]
            parameters=row.parameters,
            modified_parameters=row.modified_parameters,
            decided_by=row.decided_by,
            decision_note=row.decision_note,
            requested_at=row.requested_at,
            decided_at=row.decided_at,
            expires_at=row.expires_at,
        )
