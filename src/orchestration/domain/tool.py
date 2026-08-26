"""Tool specifications, permissions, and invocation records.

A :class:`ToolSpec` is *data about* a tool -- name, schemas, risk, limits. The
callable implementation lives in :mod:`orchestration.tools` and is registered
alongside its spec. Splitting them keeps the domain layer serialisable: a spec
can be persisted, sent over the API, and shown to an LLM, while the callable
stays in the runtime where it belongs.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Self

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
from orchestration.domain.enums import InvocationStatus, PolicyEffect, RiskLevel
from orchestration.domain.retry import DEFAULT_RETRY_POLICY, NO_RETRY_POLICY, RetryPolicy


class ToolSpec(FrozenModel):
    """Declarative description of a tool.

    Attributes:
        name: Registry key and the name exposed to models.
        description: Shown to the LLM; this is prompt surface, so it should read
            as instructions to a caller, not as internal documentation.
        input_schema: JSON Schema validated *before* the tool runs. This is the
            enforcement point for argument validity -- not the model's goodwill.
        output_schema: Optional JSON Schema describing the result.
        risk: Drives policy evaluation and approval requirements.
        requires_approval: Force a human gate regardless of risk mapping.
        enabled_by_default: Tools that are dangerous enough to require an
            explicit opt-in are registered but disabled.
        idempotent: Whether repeating the call with identical arguments is safe.
            The engine refuses to auto-retry a non-idempotent tool.
        timeout_seconds: Hard per-call ceiling.
        retry_policy: Applied only when :attr:`idempotent` is true.
    """

    name: Slug
    description: str = Field(min_length=1, max_length=2_000)
    input_schema: JsonDict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: JsonDict | None = None
    risk: RiskLevel = RiskLevel.SAFE
    requires_approval: bool = False
    enabled_by_default: bool = True
    idempotent: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=600.0)
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY
    #: Free-form tags for capability matching, e.g. {"read", "filesystem"}.
    tags: frozenset[str] = Field(default_factory=frozenset)
    version: str = Field(default="1.0.0", max_length=32)

    @model_validator(mode="after")
    def _no_retry_for_side_effects(self) -> Self:
        """A non-idempotent tool must not carry a retrying policy.

        Caught at definition time rather than at call time: a
        ``send_email`` tool with ``max_attempts=3`` is a latent duplicate-send
        bug, and the cheapest place to reject it is here.
        """
        if not self.idempotent and self.retry_policy.max_attempts > 1:
            raise ValueError(
                f"tool {self.name!r} is not idempotent but declares "
                f"max_attempts={self.retry_policy.max_attempts}; use NO_RETRY_POLICY"
            )
        return self

    @model_validator(mode="after")
    def _high_risk_needs_gate(self) -> Self:
        """CRITICAL tools must be opt-in and approval-gated.

        Encoding this as an invariant means a future contributor cannot add a
        critical-risk tool that silently runs unattended.
        """
        if self.risk is RiskLevel.CRITICAL:
            if self.enabled_by_default:
                raise ValueError(
                    f"tool {self.name!r} has CRITICAL risk and must not be enabled by default"
                )
            if not self.requires_approval:
                raise ValueError(
                    f"tool {self.name!r} has CRITICAL risk and must set requires_approval=True"
                )
        return self

    @property
    def is_retryable(self) -> bool:
        return self.idempotent and self.retry_policy.max_attempts > 1

    def to_llm_schema(self) -> JsonDict:
        """Render as an OpenAI-style function declaration.

        Adapters reshape this for other providers; keeping one canonical form
        avoids maintaining N copies of every tool declaration.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolPermission(FrozenModel):
    """One entry in an agent's tool allowlist.

    A bare tool name grants unconditional access. The optional constraints
    narrow it -- which is what makes ``read_file`` usable by an agent that must
    not read outside a directory, without needing a second tool.
    """

    tool: Slug
    effect: PolicyEffect = PolicyEffect.ALLOW
    #: Argument-level constraints, e.g. {"path": {"prefix": "./data"}}.
    constraints: JsonDict = Field(default_factory=dict)
    #: Per-execution call ceiling for this agent/tool pair.
    max_calls: int | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=500)


class AgentCapability(FrozenModel):
    """A declared skill of an agent, used by the supervisor for delegation.

    Capabilities are matched by name and keyword, not inferred from the agent's
    prose description: the supervisor must be able to shortlist candidates
    deterministically before any LLM call is made.
    """

    name: Slug
    description: str = Field(min_length=1, max_length=500)
    #: Terms that, appearing in a task, suggest this capability.
    keywords: frozenset[str] = Field(default_factory=frozenset)
    #: Relative competence, used to break ties between candidate agents.
    proficiency: Score = 0.8

    def matched_terms(self, text: str) -> frozenset[str]:
        """Distinct words in ``text`` that this capability's keywords match.

        Returns matched *terms*, not matched keywords. Counting keywords lets an
        agent inflate its own score by declaring redundant variants -- listing
        both ``feature`` and ``features`` would score twice against the single
        word "features". Scoring by the terms actually covered makes the ranking
        depend on the match rather than on how the keyword list was written.

        Matching is prefix-based in both directions so simple morphology
        (price/pricing, feature/features) lands on the same term.
        """
        terms = {t for t in re.split(r"\W+", text.lower()) if len(t) > 1}
        keywords = [k.lower() for k in self.keywords]
        return frozenset(
            term
            for term in terms
            if any(term.startswith(kw) or kw.startswith(term) for kw in keywords)
        )

    def matches(self, text: str) -> int:
        """Number of distinct terms in ``text`` this capability covers."""
        return len(self.matched_terms(text))


class ToolInvocation(DomainModel):
    """Record of one attempt to call a tool.

    Persisted for audit and for idempotency: the ``idempotency_key`` is written
    before the call and consulted on resume so a completed side effect is not
    repeated after a crash.
    """

    id: str = Field(default_factory=id_factory("tool_invocation"))
    execution_id: str
    node_id: str | None = None
    agent_id: Slug | None = None
    tool: Slug
    arguments: JsonDict = Field(default_factory=dict)
    status: InvocationStatus = InvocationStatus.RUNNING
    attempt: int = Field(default=1, ge=1)
    result: JsonDict | None = None
    error: JsonDict | None = None
    policy_effect: PolicyEffect = PolicyEffect.ALLOW
    approval_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)

    @property
    def succeeded(self) -> bool:
        return self.status is InvocationStatus.SUCCEEDED

    def redacted_arguments(self, sensitive_keys: frozenset[str]) -> JsonDict:
        """Arguments with sensitive values masked, for logging and events."""
        return {k: ("***" if k.lower() in sensitive_keys else v) for k, v in self.arguments.items()}


class ToolResult(FrozenModel):
    """Normalised outcome handed back to an agent.

    Failures are values here, not exceptions: an agent should be able to see
    that a tool failed and reason about it (try a different source, report the
    gap) rather than having its whole turn aborted.
    """

    tool: Slug
    ok: bool
    output: JsonDict | None = None
    error_code: str | None = None
    error_message: BoundedText | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    attempts: int = Field(default=1, ge=1)

    @classmethod
    def success(
        cls, tool: str, output: JsonDict, *, duration_seconds: float = 0.0, attempts: int = 1
    ) -> ToolResult:
        return cls(
            tool=tool, ok=True, output=output, duration_seconds=duration_seconds, attempts=attempts
        )

    @classmethod
    def failure(
        cls,
        tool: str,
        *,
        error_code: str,
        error_message: str,
        duration_seconds: float = 0.0,
        attempts: int = 1,
    ) -> ToolResult:
        return cls(
            tool=tool,
            ok=False,
            error_code=error_code,
            error_message=error_message,
            duration_seconds=duration_seconds,
            attempts=attempts,
        )

    def as_llm_text(self) -> str:
        """Compact rendering fed back into an agent's context."""
        if self.ok:
            return f"[{self.tool}] ok: {self.output}"
        return f"[{self.tool}] error {self.error_code}: {self.error_message}"


#: Risk levels that the default policy escalates to a human.
DEFAULT_APPROVAL_RISK_LEVELS: frozenset[RiskLevel] = frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL})

#: Argument keys whose values are masked in logs, events, and traces.
SENSITIVE_ARGUMENT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "private_key",
        "access_key",
        "session",
        "cookie",
    }
)

__all__ = [
    "DEFAULT_APPROVAL_RISK_LEVELS",
    "NO_RETRY_POLICY",
    "SENSITIVE_ARGUMENT_KEYS",
    "AgentCapability",
    "ToolInvocation",
    "ToolPermission",
    "ToolResult",
    "ToolSpec",
]
