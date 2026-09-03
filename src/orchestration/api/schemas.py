"""Request/response shapes that do not already exist as domain models.

Everywhere a request or response *is* a domain entity -- an agent definition, a
workflow, a checkpoint summary -- the route uses that model directly rather than
a hand-duplicated copy of its fields. What is defined here is only the small set
of shapes with no domain-model equivalent: the execution-creation request and
the approval decisions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestration.domain.base import JsonDict


class CreateExecutionRequest(BaseModel):
    """Body for ``POST /executions``.

    Exactly one of two shapes: give ``workflow_id`` to run a hand-authored,
    already-registered workflow; omit it and the task is handed to the
    supervisor-driven :class:`~orchestration.runtime.orchestrator.
    ExecutionOrchestrator`, which decides the plan as it goes.
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=20_000)
    success_criteria: tuple[str, ...] = ()
    workflow_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    #: Per-request overrides, tightened against (never widening) the
    #: deployment's configured defaults.
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_duration_seconds: float | None = Field(default=None, gt=0)
    max_agent_steps: int | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0)
    #: Dynamic (workflow-less) executions only; ignored when ``workflow_id`` is set.
    max_turns: int | None = Field(default=None, ge=1, le=200)


class ExecutionAccepted(BaseModel):
    """Response for a just-created execution: it is queued, not finished."""

    execution_id: str
    workflow_id: str
    status: str


class ApprovalDecisionRequest(BaseModel):
    """Body for ``POST /executions/{id}/approve`` and ``/reject``.

    ``approval_id`` is optional: most executions have exactly one pending
    approval at a time, so it may be omitted and resolved from the execution's
    own pending list. It becomes required the moment more than one is pending,
    since the choice is no longer unambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: str | None = None
    by: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2_000)
    #: Approve only: a reviewer may narrow the request instead of rejecting it
    #: outright -- approving an email but editing the recipient list, say.
    modified_arguments: JsonDict | None = None


class CancelRequest(BaseModel):
    """Body for ``POST /executions/{id}/cancel``. Empty body is also accepted."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="cancelled by operator", max_length=2_000)


class ModelOption(BaseModel):
    """One catalog model belonging to a provider -- the Providers page's model dropdown."""

    key: str
    model: str
    context_limit: int
    input_cost_per_mtok: float
    output_cost_per_mtok: float
    capabilities: tuple[str, ...]


class ProviderInfo(BaseModel):
    """One LLM provider's configuration status, for ``GET /providers``.

    The API key itself is never returned -- only whether one is set, where it
    came from, and a masked preview -- so this response is safe to render
    directly in the UI.
    """

    provider: str
    label: str
    configured: bool
    source: Literal["database", "environment", "none"]
    masked_api_key: str | None = None
    base_url: str | None = None
    selected_model_key: str | None = None
    models: tuple[ModelOption, ...] = ()


class UpdateProviderRequest(BaseModel):
    """Body for ``PUT /providers/{provider}``.

    ``api_key`` omitted (``None``) leaves whatever is already stored alone --
    a UI re-saving just the model selection must not accidentally blank out a
    key the user isn't retyping. ``clear_api_key`` is the explicit, separate
    way to actually remove one.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, min_length=1, max_length=500)
    clear_api_key: bool = False
    base_url: str | None = Field(default=None, max_length=512)
    selected_model_key: str | None = None


class HealthResponse(BaseModel):
    status: str
    database: bool
    redis: bool
    #: True when no real LLM provider is configured, so every routing
    #: decision comes from MockProvider + the deterministic heuristic
    #: fallback rather than a real model. Not a degraded state -- the
    #: engine is fully functional this way -- just worth surfacing so a
    #: caller doesn't mistake it for a broken credential.
    demo_mode: bool
