"""Execution and execution state.

:class:`ExecutionState` is the durable heart of the engine. Everything needed to
resume an interrupted run is here and nothing else is: given a state object and
the registries, the executor can rebuild its scheduling position exactly. That
constraint is what keeps checkpoint/resume honest -- if a piece of progress were
tracked only in a local variable inside the executor, resume would silently lose
it.

State transitions are validated against an explicit table rather than being
implicit in the code that assigns them, so an illegal move (say, ``SUCCEEDED``
back to ``RUNNING``) raises instead of corrupting history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import Field, model_validator

from orchestration.domain.base import (
    BoundedText,
    DomainModel,
    FrozenModel,
    JsonDict,
    Score,
    Slug,
    id_factory,
    utc_now,
)
from orchestration.domain.budget import Budget, BudgetUsage
from orchestration.domain.enums import ExecutionStatus, NodeStatus
from orchestration.domain.model import Message
from orchestration.domain.workflow import Task
from orchestration.errors import InvalidStateTransitionError

# ---------------------------------------------------------------------------
# Legal execution status transitions
# ---------------------------------------------------------------------------

#: Explicit transition table. A move absent from here is rejected.
EXECUTION_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.PENDING: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.RUNNING,  # re-entrant: resume re-asserts RUNNING
            ExecutionStatus.WAITING_FOR_APPROVAL,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.BUDGET_EXCEEDED,
            ExecutionStatus.TIMED_OUT,
        }
    ),
    ExecutionStatus.WAITING_FOR_APPROVAL: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMED_OUT,
        }
    ),
    # Terminal states permit no outbound transitions.
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.BUDGET_EXCEEDED: frozenset(),
    ExecutionStatus.TIMED_OUT: frozenset(),
}


def can_transition(source: ExecutionStatus, target: ExecutionStatus) -> bool:
    """Whether moving from ``source`` to ``target`` is legal."""
    return target in EXECUTION_TRANSITIONS.get(source, frozenset())


class NodeState(DomainModel):
    """Per-node progress within an execution.

    Attempt counting lives here rather than in the retry loop so that a resumed
    execution knows how many attempts a node has already consumed and does not
    silently reset its retry allowance.
    """

    node_id: Slug
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    output_key: str | None = None
    error: JsonDict | None = None
    confidence: Score | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    #: Set for join nodes: which upstream nodes had completed when it fired.
    satisfied_by: tuple[Slug, ...] = ()
    #: Approval gating this node, when applicable.
    approval_id: str | None = None
    #: Idempotency keys already committed for this node, so a resumed attempt
    #: does not repeat a side effect that already happened.
    committed_keys: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return self.status.is_complete

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def mark_running(self) -> None:
        self.status = NodeStatus.RUNNING
        self.attempts += 1
        if self.started_at is None:
            self.started_at = utc_now()

    def mark_succeeded(self, *, confidence: Score | None = None) -> None:
        self.status = NodeStatus.SUCCEEDED
        self.confidence = confidence
        self.completed_at = utc_now()
        self._set_duration()

    def mark_failed(self, error: JsonDict) -> None:
        self.status = NodeStatus.FAILED
        self.error = error
        self.completed_at = utc_now()
        self._set_duration()

    def mark_skipped(self, reason: str) -> None:
        self.status = NodeStatus.SKIPPED
        self.error = {"code": "skipped", "message": reason}
        self.completed_at = utc_now()
        self._set_duration()

    def _set_duration(self) -> None:
        if self.started_at and self.completed_at:
            self.duration_seconds = max(0.0, (self.completed_at - self.started_at).total_seconds())


class ExecutionError(FrozenModel):
    """A failure recorded against an execution, retained even after recovery.

    Kept as history rather than a single ``last_error`` field: a run that
    recovered from two transient failures and then succeeded is materially
    different from one that succeeded first time, and that difference is exactly
    what the recovery metrics measure.
    """

    node_id: Slug | None = None
    agent_id: Slug | None = None
    tool: Slug | None = None
    code: str
    message: BoundedText
    retryable: bool = False
    attempt: int = Field(default=1, ge=1)
    recovered: bool = False
    occurred_at: datetime = Field(default_factory=utc_now)
    context: JsonDict = Field(default_factory=dict)


class Artifact(FrozenModel):
    """A file or blob produced during execution (chart, report, dataset)."""

    id: str = Field(default_factory=id_factory("artifact"))
    name: str = Field(min_length=1, max_length=256)
    kind: str = Field(default="file", max_length=32)
    media_type: str = Field(default="application/octet-stream", max_length=128)
    #: Path relative to the configured artifact sandbox root.
    path: str = Field(min_length=1, max_length=1_024)
    size_bytes: int = Field(default=0, ge=0)
    produced_by: Slug | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: JsonDict = Field(default_factory=dict)


class ExecutionState(DomainModel):
    """Complete, serialisable state of one execution.

    This object is what gets checkpointed. It holds no coroutines, no open
    connections, and no registry references -- only data -- which is precisely
    what makes ``resume_execution`` able to reconstitute a run in a fresh
    process.
    """

    execution_id: str
    workflow_id: str
    task: Task
    status: ExecutionStatus = ExecutionStatus.PENDING

    #: Nodes currently executing. A list, not a scalar, because parallel
    #: branches mean several nodes are legitimately in flight at once.
    current_nodes: tuple[Slug, ...] = ()
    node_states: dict[str, NodeState] = Field(default_factory=dict)

    #: Conversation history for supervisor context.
    messages: tuple[Message, ...] = ()
    #: Final structured output of each completed agent node, keyed by node id.
    agent_outputs: dict[str, JsonDict] = Field(default_factory=dict)
    #: Tool results keyed by invocation id.
    tool_outputs: dict[str, JsonDict] = Field(default_factory=dict)
    #: Named values written by nodes and read by templates and conditions.
    variables: JsonDict = Field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()

    errors: tuple[ExecutionError, ...] = ()
    #: Retry counts keyed by node id.
    retries: dict[str, int] = Field(default_factory=dict)
    #: Approval ids raised during this execution.
    approvals: tuple[str, ...] = ()
    #: Approval currently blocking progress, if any.
    pending_approval_id: str | None = None

    budget: Budget = Field(default_factory=Budget)
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)

    #: Arbitrary counters and timings for observability.
    metrics: dict[str, float] = Field(default_factory=dict)

    #: The final answer, set on successful completion.
    final_output: BoundedText | None = None
    failure_reason: BoundedText | None = None

    trace_id: str | None = None
    #: Number of supervisor replans performed; bounded to prevent plan churn.
    replan_count: int = Field(default=0, ge=0)
    #: Monotonic version, incremented on every persisted mutation. Used for
    #: optimistic concurrency control so two workers cannot both advance a run.
    version: int = Field(default=0, ge=0)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # -- transitions -------------------------------------------------------

    def transition_to(self, target: ExecutionStatus, *, reason: str | None = None) -> None:
        """Move to ``target``, enforcing the transition table.

        Raises:
            InvalidStateTransitionError: If the move is not permitted.
        """
        if not can_transition(self.status, target):
            raise InvalidStateTransitionError(
                f"cannot transition execution from {self.status.value} to {target.value}",
                execution_id=self.execution_id,
                source=self.status.value,
                target=target.value,
            )
        self.status = target
        if target is ExecutionStatus.RUNNING and self.started_at is None:
            self.started_at = utc_now()
        if target.is_terminal:
            self.completed_at = utc_now()
            self.current_nodes = ()
        if reason:
            self.failure_reason = reason
        self.updated_at = utc_now()

    # -- node bookkeeping --------------------------------------------------

    def node_state(self, node_id: str) -> NodeState:
        """Get or create the state record for ``node_id``."""
        state = self.node_states.get(node_id)
        if state is None:
            state = NodeState(node_id=node_id)
            self.node_states[node_id] = state
        return state

    def completed_node_ids(self) -> frozenset[str]:
        """Nodes that will not run again and do not block downstream work."""
        return frozenset(n for n, s in self.node_states.items() if s.is_complete)

    def succeeded_node_ids(self) -> frozenset[str]:
        return frozenset(n for n, s in self.node_states.items() if s.status is NodeStatus.SUCCEEDED)

    def failed_node_ids(self) -> frozenset[str]:
        return frozenset(n for n, s in self.node_states.items() if s.status is NodeStatus.FAILED)

    def attempts_for(self, node_id: str) -> int:
        state = self.node_states.get(node_id)
        return state.attempts if state else 0

    # -- recording ---------------------------------------------------------

    def record_error(self, error: ExecutionError) -> None:
        self.errors = (*self.errors, error)
        if error.node_id:
            self.retries[error.node_id] = self.retries.get(error.node_id, 0)
        self.updated_at = utc_now()

    def mark_last_error_recovered(self, node_id: str) -> None:
        """Flag the most recent error for ``node_id`` as recovered.

        Recovery rate is a headline metric for this engine, so it is recorded
        explicitly at the moment a retry succeeds rather than being inferred
        later by correlating error and success timestamps.
        """
        for i in range(len(self.errors) - 1, -1, -1):
            if self.errors[i].node_id == node_id and not self.errors[i].recovered:
                updated = list(self.errors)
                updated[i] = self.errors[i].model_copy(update={"recovered": True})
                self.errors = tuple(updated)
                return

    def record_retry(self, node_id: str) -> int:
        self.retries[node_id] = self.retries.get(node_id, 0) + 1
        self.budget_usage.retries += 1
        self.updated_at = utc_now()
        return self.retries[node_id]

    def add_artifact(self, artifact: Artifact) -> None:
        self.artifacts = (*self.artifacts, artifact)
        self.updated_at = utc_now()

    def add_message(self, message: Message) -> None:
        self.messages = (*self.messages, message)
        self.updated_at = utc_now()

    def set_variable(self, key: str, value: Any) -> None:
        self.variables[key] = value
        self.updated_at = utc_now()

    def record_agent_output(
        self, node_id: str, output: JsonDict, *, output_key: str | None
    ) -> None:
        """Store an agent result, and mirror it into variables when requested."""
        self.agent_outputs[node_id] = output
        if output_key:
            self.variables[output_key] = output
        self.updated_at = utc_now()

    # -- derived -----------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock consumed so far.

        Derived from timestamps rather than accumulated, so a resumed execution
        correctly accounts for time already spent instead of restarting the
        duration budget at zero.
        """
        if self.started_at is None:
            return 0.0
        end = self.completed_at or utc_now()
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def total_retries(self) -> int:
        return sum(self.retries.values())

    @property
    def recovered_error_count(self) -> int:
        return sum(1 for e in self.errors if e.recovered)

    @property
    def is_waiting_for_approval(self) -> bool:
        return self.status is ExecutionStatus.WAITING_FOR_APPROVAL

    def evaluation_context(self) -> JsonDict:
        """The read-only context that conditions and templates resolve against.

        Exposes a deliberately narrow surface: outputs, variables, and a few
        scalars. Conditions cannot reach into the whole state object, which keeps
        workflow definitions from depending on engine internals.
        """
        return {
            "task": {"description": self.task.description, "inputs": self.task.inputs},
            "outputs": dict(self.agent_outputs),
            "variables": dict(self.variables),
            "status": self.status.value,
            "retries": dict(self.retries),
            "total_retries": self.total_retries,
            "elapsed_seconds": self.elapsed_seconds,
            "errors": [
                {"code": e.code, "node_id": e.node_id, "retryable": e.retryable}
                for e in self.errors
            ],
            "node_status": {n: s.status.value for n, s in self.node_states.items()},
            "confidence": {
                n: s.confidence for n, s in self.node_states.items() if s.confidence is not None
            },
        }

    def summary(self) -> JsonDict:
        """Compact status view for the API and CLI."""
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "current_nodes": list(self.current_nodes),
            "nodes_total": len(self.node_states),
            "nodes_succeeded": len(self.succeeded_node_ids()),
            "nodes_failed": len(self.failed_node_ids()),
            "total_retries": self.total_retries,
            "errors": len(self.errors),
            "errors_recovered": self.recovered_error_count,
            "cost_usd": round(self.budget_usage.cost_usd, 6),
            "total_tokens": self.budget_usage.total_tokens,
            "agent_steps": self.budget_usage.agent_steps,
            "tool_calls": self.budget_usage.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "pending_approval_id": self.pending_approval_id,
            "version": self.version,
        }


class Execution(DomainModel):
    """Header record for an execution.

    Split from :class:`ExecutionState` because the header is queried constantly
    (list views, status polls) while the state blob is large; keeping them apart
    avoids reading a full state document to answer "is it done yet".
    """

    id: str = Field(default_factory=id_factory("execution"))
    workflow_id: str
    task_description: BoundedText
    status: ExecutionStatus = ExecutionStatus.PENDING
    trace_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cost_usd: float = Field(default=0.0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    metadata: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _terminal_has_completion_time(self) -> Self:
        if self.status.is_terminal and self.completed_at is None:
            raise ValueError(
                f"execution {self.id} is in terminal status {self.status.value} "
                "but has no completed_at timestamp"
            )
        return self
