"""LLM model configuration and the message/response contract.

These types are the boundary between the engine and any provider. Everything
above this layer speaks :class:`LLMRequest` / :class:`LLMResponse` and never
touches a provider-specific payload shape.

Cost is expressed per *million* tokens because that is how every provider
publishes pricing; storing it in the published unit avoids a class of silent
off-by-1e6 errors when someone updates a rate.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from orchestration.domain.base import BoundedText, DomainModel, FrozenModel, JsonDict, Slug
from orchestration.domain.enums import MessageRole, ModelCapability, Provider


class ModelConfig(FrozenModel):
    """Declarative description of one callable model.

    Attributes:
        key: Registry key, e.g. ``"gpt-4o-mini"``.
        provider: Which adapter can serve it.
        model: The provider-side model identifier sent on the wire.
        context_limit: Total token window.
        max_output_tokens: Cap applied to generation requests.
        input_cost_per_mtok: USD per million input tokens.
        output_cost_per_mtok: USD per million output tokens.
        capabilities: Tags the router filters on.
        latency_profile: Coarse speed class used for routing, not a measurement.
        supports_json_mode: Whether the provider can be asked to guarantee JSON.
    """

    key: Slug
    provider: Provider
    model: str = Field(min_length=1, max_length=128)
    context_limit: int = Field(default=128_000, gt=0)
    max_output_tokens: int = Field(default=4_096, gt=0)
    input_cost_per_mtok: float = Field(default=0.0, ge=0)
    output_cost_per_mtok: float = Field(default=0.0, ge=0)
    capabilities: frozenset[ModelCapability] = Field(
        default_factory=lambda: frozenset({ModelCapability.CHAT})
    )
    latency_profile: Literal["fast", "standard", "slow"] = "standard"
    supports_json_mode: bool = True
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    extra_params: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _output_fits_context(self) -> Self:
        if self.max_output_tokens >= self.context_limit:
            raise ValueError(
                f"max_output_tokens ({self.max_output_tokens}) must be less than "
                f"context_limit ({self.context_limit}) for model {self.key!r}"
            )
        return self

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """USD cost for a call of the given size.

        Rounded to 8 decimal places: per-call costs are frequently in the
        1e-6 range, and float noise there accumulates visibly across a
        thousand-step benchmark.
        """
        cost = (input_tokens / 1_000_000) * self.input_cost_per_mtok + (
            output_tokens / 1_000_000
        ) * self.output_cost_per_mtok
        return round(cost, 8)

    def has(self, *capabilities: ModelCapability) -> bool:
        """Whether the model advertises every requested capability."""
        return set(capabilities).issubset(self.capabilities)

    @property
    def is_free(self) -> bool:
        """Local or mock models that cost nothing to call."""
        return self.input_cost_per_mtok == 0.0 and self.output_cost_per_mtok == 0.0


class Message(FrozenModel):
    """One turn in an LLM conversation."""

    role: MessageRole
    content: BoundedText
    name: str | None = Field(default=None, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=128)

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=MessageRole.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=MessageRole.USER, content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role=MessageRole.ASSISTANT, content=content)

    @classmethod
    def tool_result(cls, content: str, *, tool_call_id: str, name: str | None = None) -> Message:
        return cls(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=tool_call_id,
            name=name,
        )


class TokenUsage(FrozenModel):
    """Token accounting for a single LLM call."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


class ToolCallRequest(FrozenModel):
    """A model-requested tool invocation, before any policy check."""

    id: str = Field(min_length=1, max_length=128)
    name: Slug
    arguments: JsonDict = Field(default_factory=dict)


class LLMRequest(FrozenModel):
    """Provider-agnostic completion request.

    ``response_schema`` carries a JSON Schema when the caller requires
    structured output. Adapters translate it to whatever the provider supports
    (json_schema response format, tool-forcing, or a prompt instruction) -- the
    caller never has to know which mechanism was used.
    """

    messages: tuple[Message, ...] = Field(min_length=1)
    model: ModelConfig
    max_output_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    response_schema: JsonDict | None = None
    tools: tuple[JsonDict, ...] = ()
    stop: tuple[str, ...] = ()
    timeout_seconds: float | None = Field(default=None, gt=0)
    #: Opaque key used by the mock provider to look up a scripted response and
    #: by real providers for idempotency/caching correlation.
    request_key: str | None = Field(default=None, max_length=256)

    @property
    def effective_max_output_tokens(self) -> int:
        return self.max_output_tokens or self.model.max_output_tokens

    @property
    def effective_temperature(self) -> float:
        return self.model.temperature if self.temperature is None else self.temperature

    def with_messages(self, messages: tuple[Message, ...]) -> LLMRequest:
        """Copy carrying different messages (used by the schema-repair path)."""
        return self.model_copy(update={"messages": messages})


class LLMResponse(FrozenModel):
    """Provider-agnostic completion result."""

    content: BoundedText
    model_key: Slug
    provider: Provider
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = Field(default=0.0, ge=0)
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "error"] = "stop"
    tool_calls: tuple[ToolCallRequest, ...] = ()
    latency_seconds: float = Field(default=0.0, ge=0)
    #: Provider metadata worth keeping for debugging (request id, rate headers).
    #: Never contains credentials: adapters copy an explicit allowlist of keys.
    raw_metadata: JsonDict = Field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """Whether generation stopped because it hit the output cap."""
        return self.finish_reason == "length"


class EmbeddingRequest(FrozenModel):
    """Batch embedding request, used for pgvector-backed evidence retrieval."""

    texts: tuple[str, ...] = Field(min_length=1)
    model: ModelConfig
    dimensions: int = Field(default=768, ge=8, le=4096)


class EmbeddingResponse(FrozenModel):
    """Embedding vectors, aligned positionally with the request texts."""

    vectors: tuple[tuple[float, ...], ...]
    model_key: Slug
    provider: Provider
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _uniform_dimensions(self) -> Self:
        if not self.vectors:
            return self
        widths = {len(v) for v in self.vectors}
        if len(widths) != 1:
            raise ValueError(f"embedding vectors have inconsistent dimensions: {sorted(widths)}")
        return self

    @property
    def dimension(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


class RoutingCriteria(DomainModel):
    """What the caller needs from a model, handed to the router.

    Deliberately small. Model routing is easy to over-build; this captures the
    dimensions that change the answer and nothing more.
    """

    required_capabilities: frozenset[ModelCapability] = Field(default_factory=frozenset)
    prefer: Literal["balanced", "cheapest", "fastest", "most_capable"] = "balanced"
    #: Force local-only inference (no data leaves the host).
    require_local: bool = False
    max_cost_per_mtok: float | None = Field(default=None, gt=0)
    min_context_limit: int | None = Field(default=None, gt=0)
    #: Explicit override; when set the router returns this key or fails.
    pinned_model: Slug | None = None


class ModelSelection(FrozenModel):
    """The router's answer, with its reasoning recorded for observability."""

    model: ModelConfig
    reason: str = Field(max_length=500)
    considered: tuple[Slug, ...] = ()
    criteria: RoutingCriteria | None = None

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "model_key": self.model.key,
            "provider": self.model.provider.value,
            "reason": self.reason,
            "considered": list(self.considered),
        }
