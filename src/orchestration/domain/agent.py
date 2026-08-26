"""Agent definitions and invocation records.

An :class:`AgentDefinition` is inert, serialisable configuration: identity,
capabilities, tool allowlist, model preference, limits. The behaviour that acts
on it lives in :mod:`orchestration.agents`. This separation is what allows an
agent to be registered at runtime over the HTTP API -- there is no Python class
to import, only data to validate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from orchestration.domain.base import (
    BoundedText,
    DomainModel,
    FrozenModel,
    JsonDict,
    ModelKey,
    Score,
    Slug,
    TimestampedModel,
    id_factory,
    utc_now,
)
from orchestration.domain.budget import Budget
from orchestration.domain.enums import InvocationStatus, ModelCapability
from orchestration.domain.model import RoutingCriteria
from orchestration.domain.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from orchestration.domain.tool import AgentCapability, ToolPermission


class AgentDefinition(TimestampedModel):
    """Registry entry describing one agent.

    Attributes:
        id: Stable registry key, e.g. ``"research_agent"``.
        kind: Which runtime implementation drives this agent. Multiple
            definitions may share a kind -- ``pricing_agent`` and
            ``feature_agent`` are both ``research`` agents with different prompts
            and tools, which is how the reference workflow fans out without
            adding classes.
        allowed_tools: The complete allowlist. Deny-by-default: a tool absent
            from this list cannot be called, whatever the model asks for.
        max_iterations: Cap on the agent's internal reason/act loop, so a
            confused agent cannot spin until the budget dies.
        confidence_floor: Below this, the runtime marks the output low-confidence
            so the supervisor can order more work instead of accepting it.
    """

    id: Slug
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2_000)
    kind: Slug = "generic"
    capabilities: tuple[AgentCapability, ...] = ()
    allowed_tools: tuple[ToolPermission, ...] = ()
    system_prompt: BoundedText = Field(min_length=1)
    routing_criteria: RoutingCriteria = Field(default_factory=RoutingCriteria)
    #: Pinned model key; when set it overrides the router entirely.
    model_key: ModelKey | None = None
    timeout_seconds: float = Field(default=120.0, gt=0, le=3_600.0)
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY
    budget: Budget | None = None
    max_iterations: int = Field(default=6, ge=1, le=50)
    confidence_floor: Score = 0.5
    enabled: bool = True
    #: Arbitrary configuration consumed by the specific agent runtime.
    config: JsonDict = Field(default_factory=dict)
    version: str = Field(default="1.0.0", max_length=32)
    tags: frozenset[str] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def _unique_tool_permissions(self) -> Self:
        names = [p.tool for p in self.allowed_tools]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"agent {self.id!r} lists duplicate tool permissions: {sorted(duplicates)}"
            )
        return self

    @model_validator(mode="after")
    def _unique_capabilities(self) -> Self:
        names = [c.name for c in self.capabilities]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"agent {self.id!r} lists duplicate capabilities: {sorted(duplicates)}"
            )
        return self

    # -- queries -----------------------------------------------------------

    @property
    def tool_names(self) -> frozenset[str]:
        """Names of every tool this agent may attempt."""
        return frozenset(p.tool for p in self.allowed_tools)

    def permission_for(self, tool: str) -> ToolPermission | None:
        """The permission entry for ``tool``, or ``None`` if not allowlisted."""
        return next((p for p in self.allowed_tools if p.tool == tool), None)

    def may_attempt(self, tool: str) -> bool:
        """Whether the tool appears in the allowlist at all.

        This is a cheap pre-check for prompt construction. It is *not* the
        authorisation decision -- that is the policy engine's job, which also
        evaluates argument constraints and risk.
        """
        return tool in self.tool_names

    def capability_score(self, text: str) -> float:
        """How well this agent matches ``text``, for deterministic shortlisting.

        Weighted by proficiency so a highly capable agent outranks a marginal
        one on the same keyword hit. Returns 0.0 when nothing matches, which the
        heuristic router treats as "not a candidate".
        """
        if not self.capabilities:
            return 0.0
        total = sum(cap.matches(text) * cap.proficiency for cap in self.capabilities)
        return round(total, 6)

    def required_model_capabilities(self) -> frozenset[ModelCapability]:
        """Model capabilities implied by this agent's configuration."""
        required = set(self.routing_criteria.required_capabilities)
        required.add(ModelCapability.CHAT)
        if self.allowed_tools:
            required.add(ModelCapability.TOOL_USE)
        return frozenset(required)

    def summary_for_supervisor(self) -> JsonDict:
        """Compact description handed to the supervisor prompt.

        Deliberately excludes the system prompt and config: the supervisor
        chooses *between* agents and does not need their internals, and keeping
        this small directly reduces routing token cost.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "capabilities": [
                {"name": c.name, "description": c.description} for c in self.capabilities
            ],
            "tools": sorted(self.tool_names),
        }


class AgentOutput(FrozenModel):
    """Structured result of one agent run.

    ``confidence`` and ``evidence`` exist so the supervisor can make a
    low-confidence routing decision from data rather than by re-reading prose.
    """

    content: BoundedText
    confidence: Score = 0.8
    #: Short claims the agent asserts, each ideally backed by an evidence item.
    claims: tuple[str, ...] = ()
    #: Source references, URLs, or file paths supporting the content.
    evidence: tuple[str, ...] = ()
    #: Facts the agent could not establish -- drives follow-up research.
    gaps: tuple[str, ...] = ()
    #: Structured payload for downstream nodes (tables, metrics, file paths).
    data: JsonDict = Field(default_factory=dict)
    #: Artifact identifiers produced by this run (charts, reports).
    artifacts: tuple[str, ...] = ()

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < 0.5

    @property
    def unsupported_claim_count(self) -> int:
        """Claims present with no evidence at all.

        A blunt signal, intentionally: it lets the critic and the supervisor
        detect an unsupported answer without another LLM call.
        """
        return len(self.claims) if not self.evidence else 0

    def as_context_text(self, *, max_chars: int = 4_000) -> str:
        """Rendering used when this output becomes another agent's input."""
        body = self.content[:max_chars]
        parts = [body]
        if self.evidence:
            parts.append("Evidence: " + "; ".join(self.evidence[:10]))
        if self.gaps:
            parts.append("Open gaps: " + "; ".join(self.gaps[:10]))
        return "\n".join(parts)


class AgentInvocation(DomainModel):
    """Record of one agent execution attempt."""

    id: str = Field(default_factory=id_factory("agent_invocation"))
    execution_id: str
    node_id: str | None = None
    agent_id: Slug
    attempt: int = Field(default=1, ge=1)
    status: InvocationStatus = InvocationStatus.RUNNING
    task_input: BoundedText = ""
    output: AgentOutput | None = None
    error: JsonDict | None = None
    model_key: ModelKey | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    iterations: int = Field(default=0, ge=0)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    trace_id: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is InvocationStatus.SUCCEEDED

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
