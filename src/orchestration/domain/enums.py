"""Enumerations shared across the domain model.

``StrEnum`` is used throughout so that values serialise to plain strings in JSON
and JSONB columns without custom encoders, and remain readable in the database.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionStatus(StrEnum):
    """Lifecycle of a single workflow execution.

    The transition table lives in :mod:`orchestration.domain.execution`; this
    enum only names the states.
    """

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        """Whether no further work will occur for this execution."""
        return self in _TERMINAL_EXECUTION_STATUSES

    @property
    def is_resumable(self) -> bool:
        """Whether ``resume_execution`` may pick this execution back up.

        ``RUNNING`` is resumable because a process crash leaves executions
        stranded in that state; resume re-acquires the lock and continues from
        the newest checkpoint.
        """
        return self in _RESUMABLE_EXECUTION_STATUSES


_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.BUDGET_EXCEEDED,
        ExecutionStatus.TIMED_OUT,
    }
)

_RESUMABLE_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.PENDING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.WAITING_FOR_APPROVAL,
    }
)


class NodeStatus(StrEnum):
    """Lifecycle of one node within an execution."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    WAITING_FOR_APPROVAL = "waiting_for_approval"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_NODE_STATUSES

    @property
    def is_complete(self) -> bool:
        """Terminal *and* not requiring downstream nodes to block.

        ``SKIPPED`` counts as complete: a conditional branch that was not taken
        must not stall a downstream join forever.
        """
        return self in _COMPLETE_NODE_STATUSES


_TERMINAL_NODE_STATUSES = frozenset(
    {
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.SKIPPED,
        NodeStatus.CANCELLED,
    }
)

_COMPLETE_NODE_STATUSES = frozenset(
    {
        NodeStatus.SUCCEEDED,
        NodeStatus.SKIPPED,
    }
)


class NodeKind(StrEnum):
    """What a workflow node does when executed.

    Keeping node behaviour in an enum -- rather than in a subclass hierarchy --
    means a node round-trips through JSONB and back without polymorphic
    deserialisation, which matters for checkpoint/resume.
    """

    AGENT = "agent"
    """Invoke a registered agent."""

    TOOL = "tool"
    """Invoke a tool directly, without an agent in the loop."""

    SUPERVISOR = "supervisor"
    """Ask the supervisor for a routing decision, possibly extending the graph."""

    BRANCH = "branch"
    """Evaluate conditions and select which outgoing edges activate."""

    JOIN = "join"
    """Fan-in barrier: waits for upstream nodes per its join policy."""

    APPROVAL = "approval"
    """Human-in-the-loop gate. Suspends the execution durably."""

    TERMINAL = "terminal"
    """End of a path. Carries the final output when reached."""


class JoinPolicy(StrEnum):
    """How a :attr:`NodeKind.JOIN` node treats upstream results."""

    ALL = "all"
    """Every upstream node must complete successfully."""

    ANY = "any"
    """Proceed as soon as one upstream node succeeds; cancel the rest."""

    QUORUM = "quorum"
    """Proceed when ``quorum`` upstream nodes succeed."""

    ALL_SETTLED = "all_settled"
    """Wait for every upstream node, tolerating failures (partial results)."""


class SupervisorAction(StrEnum):
    """The closed set of actions a supervisor may return.

    Closed by design: the engine dispatches on this enum, so a hallucinated
    action fails schema validation instead of reaching the executor.
    """

    RESPOND_DIRECTLY = "respond_directly"
    DELEGATE = "delegate"
    PARALLEL_DELEGATE = "parallel_delegate"
    RETRY = "retry"
    REPLAN = "replan"
    REQUEST_HUMAN_APPROVAL = "request_human_approval"
    FINALIZE = "finalize"
    FAIL = "fail"


class RiskLevel(StrEnum):
    """How dangerous a tool invocation is, used by the policy engine."""

    SAFE = "safe"
    """Read-only, no external side effects (calculator, read_file)."""

    LOW = "low"
    """Outbound reads (web search, HTTP GET)."""

    MEDIUM = "medium"
    """Local writes, code execution in a constrained context."""

    HIGH = "high"
    """Irreversible or externally visible effects (send_email, DB write)."""

    CRITICAL = "critical"
    """Arbitrary code/command execution, production data mutation."""


class PolicyEffect(StrEnum):
    """Outcome of a policy evaluation for a tool invocation."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalStatus(StrEnum):
    """State of a human approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_decided(self) -> bool:
        return self is not ApprovalStatus.PENDING


class InvocationStatus(StrEnum):
    """Outcome of a single agent or tool invocation attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    DENIED = "denied"
    CANCELLED = "cancelled"


class CheckpointReason(StrEnum):
    """Why a checkpoint was taken.

    Mirrors the checkpoint trigger points required by the design: before and
    after each node, around approvals, and before finalisation.
    """

    EXECUTION_STARTED = "execution_started"
    BEFORE_NODE = "before_node"
    AFTER_NODE_SUCCESS = "after_node_success"
    AFTER_NODE_FAILURE = "after_node_failure"
    BEFORE_APPROVAL = "before_approval"
    AFTER_APPROVAL = "after_approval"
    BEFORE_FINALIZATION = "before_finalization"
    #: Written after the terminal transition, so the persisted status matches
    #: reality. Without it a crash at completion leaves a finished run looking
    #: resumable.
    EXECUTION_FINALIZED = "execution_finalized"
    AFTER_REPLAN = "after_replan"
    ON_CANCELLATION = "on_cancellation"
    ON_BUDGET_EXCEEDED = "on_budget_exceeded"
    #: A dynamic orchestration round drained (its delegated nodes all finished)
    #: without the overall execution actually concluding. Written to correct a
    #: checkpoint :class:`~orchestration.workflow.executor.WorkflowExecutor`
    #: wrote when it mistook "nothing left ready in this round's subgraph" for
    #: "the execution is over" -- true only for a static, fully-declared graph.
    ROUND_COMPLETED = "round_completed"


class EventType(StrEnum):
    """Typed execution events persisted for observability and debugging."""

    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_CANCELLED = "execution_cancelled"
    EXECUTION_RESUMED = "execution_resumed"

    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    NODE_SKIPPED = "node_skipped"

    AGENT_INVOKED = "agent_invoked"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"

    TOOL_INVOKED = "tool_invoked"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    TOOL_DENIED = "tool_denied"

    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_COMPLETED = "llm_call_completed"

    SUPERVISOR_DECIDED = "supervisor_decided"
    ROUTING_DEGRADED = "routing_degraded"
    REPLANNED = "replanned"

    RETRY_STARTED = "retry_started"
    RETRY_EXHAUSTED = "retry_exhausted"

    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_RESTORED = "checkpoint_restored"

    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"

    BUDGET_WARNING = "budget_warning"
    BUDGET_EXCEEDED = "budget_exceeded"

    POLICY_DENIED = "policy_denied"


class EventSeverity(StrEnum):
    """Severity attached to an event, so a UI can filter without a lookup table."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class BudgetDimension(StrEnum):
    """The metered dimensions of an execution budget."""

    COST_USD = "cost_usd"
    TOKENS = "tokens"
    DURATION_SECONDS = "duration_seconds"
    AGENT_STEPS = "agent_steps"
    TOOL_CALLS = "tool_calls"
    RETRIES = "retries"


class Provider(StrEnum):
    """Supported LLM provider families."""

    MOCK = "mock"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    GROQ = "groq"


class ModelCapability(StrEnum):
    """Capability tags used by the model router to filter candidates."""

    CHAT = "chat"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_USE = "tool_use"
    LONG_CONTEXT = "long_context"
    VISION = "vision"
    REASONING = "reasoning"
    FAST = "fast"
    CHEAP = "cheap"
    LOCAL = "local"
    EMBEDDING = "embedding"


class TaskComplexity(StrEnum):
    """Coarse complexity signal driving model selection."""

    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class MessageRole(StrEnum):
    """Roles in an LLM conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
