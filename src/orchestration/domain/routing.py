"""Supervisor routing decisions.

This is the contract between the supervisor's LLM call and the execution engine.
It is intentionally strict: ``extra="forbid"``, a closed action enum, and
cross-field validators that reject a decision whose payload does not match its
action. A model that invents an action, names three agents for a single
delegation, or asks to finalize without an answer fails validation and never
reaches the executor.

The engine therefore never parses free-form natural language to decide what to
do next.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from orchestration.domain.base import BoundedText, DomainModel, JsonDict, Score, Slug
from orchestration.domain.enums import SupervisorAction
from orchestration.domain.workflow import DynamicPlan


class DelegationTarget(DomainModel):
    """One agent assignment produced by the supervisor."""

    agent_id: Slug
    #: The specific instruction for this agent, narrower than the overall task.
    instruction: BoundedText = Field(min_length=1)
    #: Where to store the result in execution variables.
    output_key: str | None = Field(default=None, max_length=128)
    #: Priority hint used when concurrency limits force serialisation.
    priority: int = Field(default=0, ge=-100, le=100)
    context_keys: tuple[str, ...] = ()


class RoutingDecision(DomainModel):
    """A validated instruction from the supervisor to the engine.

    Attributes:
        action: What to do next. Closed set -- the engine dispatches on it.
        targets: Agents to invoke, for delegate / parallel_delegate.
        answer: The response text, for respond_directly / finalize.
        plan: A new subgraph, for replan.
        retry_node_id: Which node to re-attempt, for retry.
        confidence: The supervisor's own confidence in this decision. Used for
            observability and for the low-confidence benchmark category, never
            as a substitute for validation.
        reason: Why this action was chosen. Required: an unexplained routing
            decision is not debuggable after the fact.
    """

    action: SupervisorAction
    reason: BoundedText = Field(min_length=1, max_length=4_000)
    confidence: Score = 0.5

    targets: tuple[DelegationTarget, ...] = ()
    answer: BoundedText | None = None
    plan: DynamicPlan | None = None
    retry_node_id: Slug | None = None
    #: For request_human_approval.
    approval_action: str | None = Field(default=None, max_length=256)
    approval_risk_reason: BoundedText | None = None
    #: For fail.
    failure_reason: BoundedText | None = None
    metadata: JsonDict = Field(default_factory=dict)

    # -- action/payload coherence -----------------------------------------

    @model_validator(mode="after")
    def _payload_matches_action(self) -> Self:
        """Reject decisions whose payload contradicts their action.

        Each branch states what the executor will need. Validating here means the
        executor can rely on the invariant instead of defensively re-checking.
        """
        match self.action:
            case SupervisorAction.RESPOND_DIRECTLY | SupervisorAction.FINALIZE:
                if not (self.answer and self.answer.strip()):
                    raise ValueError(f"action {self.action.value!r} requires a non-empty 'answer'")

            case SupervisorAction.DELEGATE:
                if len(self.targets) != 1:
                    raise ValueError(
                        f"action 'delegate' requires exactly 1 target, got {len(self.targets)}; "
                        "use 'parallel_delegate' for more than one"
                    )

            case SupervisorAction.PARALLEL_DELEGATE:
                if len(self.targets) < 2:
                    raise ValueError(
                        f"action 'parallel_delegate' requires at least 2 targets, "
                        f"got {len(self.targets)}"
                    )

            case SupervisorAction.RETRY:
                if not self.retry_node_id:
                    raise ValueError("action 'retry' requires 'retry_node_id'")

            case SupervisorAction.REPLAN:
                if self.plan is None:
                    raise ValueError("action 'replan' requires a 'plan'")

            case SupervisorAction.REQUEST_HUMAN_APPROVAL:
                if not self.approval_action:
                    raise ValueError("action 'request_human_approval' requires 'approval_action'")
                if not (self.approval_risk_reason and self.approval_risk_reason.strip()):
                    raise ValueError(
                        "action 'request_human_approval' requires 'approval_risk_reason' so the "
                        "reviewer is told why they are being asked"
                    )

            case SupervisorAction.FAIL:
                if not (self.failure_reason and self.failure_reason.strip()):
                    raise ValueError("action 'fail' requires 'failure_reason'")

        return self

    @model_validator(mode="after")
    def _no_duplicate_targets(self) -> Self:
        ids = [t.agent_id for t in self.targets]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"routing decision names the same agent twice: {duplicates}")
        return self

    # -- queries -----------------------------------------------------------

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(t.agent_id for t in self.targets)

    @property
    def is_parallel(self) -> bool:
        return self.action is SupervisorAction.PARALLEL_DELEGATE

    @property
    def is_terminal(self) -> bool:
        """Whether this decision ends the execution."""
        return self.action in {
            SupervisorAction.RESPOND_DIRECTLY,
            SupervisorAction.FINALIZE,
            SupervisorAction.FAIL,
        }

    @property
    def requires_agents(self) -> bool:
        return self.action in {SupervisorAction.DELEGATE, SupervisorAction.PARALLEL_DELEGATE}

    def as_event_payload(self) -> JsonDict:
        return {
            "action": self.action.value,
            "agents": list(self.agent_ids),
            "confidence": self.confidence,
            "reason": self.reason[:500],
            "retry_node_id": self.retry_node_id,
            "plan_nodes": [n.id for n in self.plan.nodes] if self.plan else [],
        }

    @classmethod
    def json_schema_for_llm(cls) -> JsonDict:
        """JSON Schema handed to the provider to constrain generation.

        Derived from the model itself, so the schema shown to the LLM can never
        drift from the schema used to validate its reply.
        """
        return cls.model_json_schema(mode="validation")


class RoutingAttempt(DomainModel):
    """Audit record of one attempt to obtain a routing decision.

    Retained even when it failed validation. A supervisor that needed a repair
    attempt is a signal worth measuring -- it is reported as routing degradation
    in the benchmark rather than being hidden by the retry that fixed it.
    """

    attempt: int = Field(default=1, ge=1)
    raw_output: BoundedText = ""
    valid: bool = False
    validation_errors: tuple[str, ...] = ()
    decision: RoutingDecision | None = None
    model_key: Slug | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    latency_seconds: float = Field(default=0.0, ge=0)
    #: True when the heuristic fallback produced the decision instead of the LLM.
    from_fallback: bool = False


class RoutingOutcome(DomainModel):
    """The end result of a supervisor turn, including how it was reached."""

    decision: RoutingDecision
    attempts: tuple[RoutingAttempt, ...] = ()
    degraded: bool = False

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(a.cost_usd for a in self.attempts), 8)

    @property
    def total_tokens(self) -> int:
        return sum(a.input_tokens + a.output_tokens for a in self.attempts)

    @property
    def used_fallback(self) -> bool:
        return any(a.from_fallback for a in self.attempts)
