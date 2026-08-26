"""Domain model: the Pydantic types every other subsystem speaks.

This package has no dependencies on the runtime, persistence, or providers -- the
dependency arrow points inward. That is what allows the same objects to be used
by the API layer, the execution engine, the database mappers, and the benchmark
harness without any of them importing each other.

Layout:

``base``
    Shared model configuration, identifier generation, UTC time helpers.
``enums``
    Every closed vocabulary in the system.
``retry`` / ``budget`` / ``model``
    Value objects for limits and the LLM contract.
``tool`` / ``agent``
    Registry definitions and invocation records.
``workflow`` / ``execution`` / ``checkpoint`` / ``approval``
    Graph definition and durable runtime state.
``routing`` / ``events`` / ``evaluation``
    Supervisor decisions, observability records, and benchmark types.
"""

from __future__ import annotations

from orchestration.domain.agent import AgentDefinition, AgentInvocation, AgentOutput
from orchestration.domain.approval import ApprovalRequest
from orchestration.domain.base import (
    BoundedText,
    DomainModel,
    FrozenModel,
    Identifier,
    JsonDict,
    ModelKey,
    Score,
    Slug,
    TimestampedModel,
    duration_since,
    ensure_utc,
    id_factory,
    new_id,
    utc_now,
)
from orchestration.domain.budget import (
    DEFAULT_BUDGET,
    TIGHT_BUDGET,
    UNLIMITED_BUDGET,
    Budget,
    BudgetSnapshot,
    BudgetStatus,
    BudgetUsage,
)
from orchestration.domain.checkpoint import Checkpoint
from orchestration.domain.enums import (
    ApprovalStatus,
    BudgetDimension,
    CheckpointReason,
    EventSeverity,
    EventType,
    ExecutionStatus,
    InvocationStatus,
    JoinPolicy,
    MessageRole,
    ModelCapability,
    NodeKind,
    NodeStatus,
    PolicyEffect,
    Provider,
    RiskLevel,
    SupervisorAction,
    TaskComplexity,
)
from orchestration.domain.evaluation import (
    ArmMetrics,
    BenchmarkReport,
    BenchmarkScenario,
    EvaluationResult,
    ScenarioExpectation,
    ScenarioResult,
    percentile,
    summarise_arm,
)
from orchestration.domain.events import EventFilter, ExecutionEvent
from orchestration.domain.execution import (
    EXECUTION_TRANSITIONS,
    Artifact,
    Execution,
    ExecutionError,
    ExecutionState,
    NodeState,
    can_transition,
)
from orchestration.domain.model import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMRequest,
    LLMResponse,
    Message,
    ModelConfig,
    ModelSelection,
    RoutingCriteria,
    TokenUsage,
    ToolCallRequest,
)
from orchestration.domain.retry import (
    DEFAULT_RETRY_POLICY,
    NETWORK_RETRY_POLICY,
    NO_RETRY_POLICY,
    RATE_LIMIT_RETRY_POLICY,
    RetryPolicy,
)
from orchestration.domain.routing import (
    DelegationTarget,
    RoutingAttempt,
    RoutingDecision,
    RoutingOutcome,
)
from orchestration.domain.tool import (
    DEFAULT_APPROVAL_RISK_LEVELS,
    SENSITIVE_ARGUMENT_KEYS,
    AgentCapability,
    ToolInvocation,
    ToolPermission,
    ToolResult,
    ToolSpec,
)
from orchestration.domain.workflow import (
    Condition,
    DynamicPlan,
    NodeCondition,
    Task,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)

__all__ = [
    "DEFAULT_APPROVAL_RISK_LEVELS",
    "DEFAULT_BUDGET",
    "DEFAULT_RETRY_POLICY",
    "EXECUTION_TRANSITIONS",
    "NETWORK_RETRY_POLICY",
    "NO_RETRY_POLICY",
    "RATE_LIMIT_RETRY_POLICY",
    "SENSITIVE_ARGUMENT_KEYS",
    "TIGHT_BUDGET",
    "UNLIMITED_BUDGET",
    "AgentCapability",
    "AgentDefinition",
    "AgentInvocation",
    "AgentOutput",
    "ApprovalRequest",
    "ApprovalStatus",
    "ArmMetrics",
    "Artifact",
    "BenchmarkReport",
    "BenchmarkScenario",
    "BoundedText",
    "Budget",
    "BudgetDimension",
    "BudgetSnapshot",
    "BudgetStatus",
    "BudgetUsage",
    "Checkpoint",
    "CheckpointReason",
    "Condition",
    "DelegationTarget",
    "DomainModel",
    "DynamicPlan",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "EvaluationResult",
    "EventFilter",
    "EventSeverity",
    "EventType",
    "Execution",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionState",
    "ExecutionStatus",
    "FrozenModel",
    "Identifier",
    "InvocationStatus",
    "JoinPolicy",
    "JsonDict",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MessageRole",
    "ModelCapability",
    "ModelConfig",
    "ModelKey",
    "ModelSelection",
    "NodeCondition",
    "NodeKind",
    "NodeState",
    "NodeStatus",
    "PolicyEffect",
    "Provider",
    "RetryPolicy",
    "RiskLevel",
    "RoutingAttempt",
    "RoutingCriteria",
    "RoutingDecision",
    "RoutingOutcome",
    "ScenarioExpectation",
    "ScenarioResult",
    "Score",
    "Slug",
    "SupervisorAction",
    "Task",
    "TaskComplexity",
    "TimestampedModel",
    "TokenUsage",
    "ToolCallRequest",
    "ToolInvocation",
    "ToolPermission",
    "ToolResult",
    "ToolSpec",
    "Workflow",
    "WorkflowEdge",
    "WorkflowNode",
    "can_transition",
    "duration_since",
    "ensure_utc",
    "id_factory",
    "new_id",
    "percentile",
    "summarise_arm",
    "utc_now",
]
