"""Model catalog.

A registry of :class:`ModelConfig` entries the router chooses between.

**On the pricing figures below:** they are the values this reference
implementation ships as defaults, and they are the kind of data that goes stale
the moment a provider updates a rate card. The engine therefore never depends on
them being current -- cost accounting uses whatever is configured, and the
benchmark reports mock-provider cost (which is zero) separately from any
projection. Treat them as illustrative defaults to be overridden from
configuration, not as an authoritative price list.

Context limits and capability tags are structural rather than commercial, so they
age much more slowly.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from orchestration.domain.enums import ModelCapability, Provider
from orchestration.domain.model import ModelConfig
from orchestration.errors import DuplicateError, NotFoundError

# ---------------------------------------------------------------------------
# Capability shorthands
# ---------------------------------------------------------------------------

_CHAT = ModelCapability.CHAT
_STRUCT = ModelCapability.STRUCTURED_OUTPUT
_TOOLS = ModelCapability.TOOL_USE
_LONG = ModelCapability.LONG_CONTEXT
_REASON = ModelCapability.REASONING
_FAST = ModelCapability.FAST
_CHEAP = ModelCapability.CHEAP
_LOCAL = ModelCapability.LOCAL
_VISION = ModelCapability.VISION
_EMBED = ModelCapability.EMBEDDING


# ---------------------------------------------------------------------------
# Mock models -- the default, and the only ones used by tests and benchmarks
# ---------------------------------------------------------------------------

MOCK_FAST = ModelConfig(
    key="mock-fast",
    provider=Provider.MOCK,
    model="mock-fast",
    context_limit=128_000,
    max_output_tokens=4_096,
    input_cost_per_mtok=0.0,
    output_cost_per_mtok=0.0,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _FAST, _CHEAP}),
    latency_profile="fast",
)

MOCK_SMART = ModelConfig(
    key="mock-smart",
    provider=Provider.MOCK,
    model="mock-smart",
    context_limit=200_000,
    max_output_tokens=8_192,
    input_cost_per_mtok=0.0,
    output_cost_per_mtok=0.0,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _REASON, _LONG}),
    latency_profile="standard",
)

MOCK_EMBED = ModelConfig(
    key="mock-embed",
    provider=Provider.MOCK,
    model="mock-embed",
    context_limit=8_192,
    max_output_tokens=1,
    capabilities=frozenset({_EMBED}),
    latency_profile="fast",
)


# ---------------------------------------------------------------------------
# Hosted models -- illustrative defaults, override from configuration
# ---------------------------------------------------------------------------

CLAUDE_OPUS = ModelConfig(
    key="claude-opus-4-5",
    provider=Provider.ANTHROPIC,
    model="claude-opus-4-5",
    context_limit=200_000,
    max_output_tokens=32_000,
    input_cost_per_mtok=5.00,
    output_cost_per_mtok=25.00,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _REASON, _LONG, _VISION}),
    latency_profile="standard",
)

CLAUDE_SONNET = ModelConfig(
    key="claude-sonnet-4-5",
    provider=Provider.ANTHROPIC,
    model="claude-sonnet-4-5",
    context_limit=200_000,
    max_output_tokens=16_000,
    input_cost_per_mtok=3.00,
    output_cost_per_mtok=15.00,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _REASON, _LONG, _VISION}),
    latency_profile="standard",
)

CLAUDE_HAIKU = ModelConfig(
    key="claude-haiku-4-5",
    provider=Provider.ANTHROPIC,
    model="claude-haiku-4-5",
    context_limit=200_000,
    max_output_tokens=8_192,
    input_cost_per_mtok=1.00,
    output_cost_per_mtok=5.00,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _FAST, _CHEAP, _LONG}),
    latency_profile="fast",
)

GPT_4O = ModelConfig(
    key="gpt-4o",
    provider=Provider.OPENAI,
    model="gpt-4o",
    context_limit=128_000,
    max_output_tokens=16_384,
    input_cost_per_mtok=2.50,
    output_cost_per_mtok=10.00,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _REASON, _VISION}),
    latency_profile="standard",
)

GPT_4O_MINI = ModelConfig(
    key="gpt-4o-mini",
    provider=Provider.OPENAI,
    model="gpt-4o-mini",
    context_limit=128_000,
    max_output_tokens=16_384,
    input_cost_per_mtok=0.15,
    output_cost_per_mtok=0.60,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _FAST, _CHEAP}),
    latency_profile="fast",
)

GEMINI_FLASH = ModelConfig(
    key="gemini-2.5-flash",
    provider=Provider.GEMINI,
    model="gemini-2.5-flash",
    context_limit=1_000_000,
    max_output_tokens=8_192,
    input_cost_per_mtok=0.30,
    output_cost_per_mtok=2.50,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _FAST, _CHEAP, _LONG, _VISION}),
    latency_profile="fast",
)

GEMINI_PRO = ModelConfig(
    key="gemini-2.5-pro",
    provider=Provider.GEMINI,
    model="gemini-2.5-pro",
    context_limit=1_000_000,
    max_output_tokens=16_384,
    input_cost_per_mtok=1.25,
    output_cost_per_mtok=10.00,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _REASON, _LONG, _VISION}),
    latency_profile="standard",
)

GROQ_GPT_OSS_120B = ModelConfig(
    key="gpt-oss-120b-groq",
    provider=Provider.GROQ,
    model="openai/gpt-oss-120b",
    context_limit=131_072,
    max_output_tokens=32_768,
    input_cost_per_mtok=0.15,
    output_cost_per_mtok=0.75,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _REASON, _FAST, _CHEAP}),
    latency_profile="fast",
)

OPENAI_EMBED_SMALL = ModelConfig(
    key="text-embedding-3-small",
    provider=Provider.OPENAI,
    model="text-embedding-3-small",
    context_limit=8_191,
    max_output_tokens=1,
    input_cost_per_mtok=0.02,
    output_cost_per_mtok=0.0,
    capabilities=frozenset({_EMBED, _CHEAP}),
    latency_profile="fast",
)


# ---------------------------------------------------------------------------
# Local models
# ---------------------------------------------------------------------------

LLAMA_LOCAL = ModelConfig(
    key="llama3.1-local",
    provider=Provider.OLLAMA,
    model="llama3.1:8b",
    context_limit=128_000,
    max_output_tokens=4_096,
    input_cost_per_mtok=0.0,
    output_cost_per_mtok=0.0,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _LOCAL, _CHEAP}),
    latency_profile="slow",
)

QWEN_LOCAL = ModelConfig(
    key="qwen2.5-local",
    provider=Provider.OLLAMA,
    model="qwen2.5:14b",
    context_limit=32_768,
    max_output_tokens=4_096,
    input_cost_per_mtok=0.0,
    output_cost_per_mtok=0.0,
    capabilities=frozenset({_CHAT, _STRUCT, _TOOLS, _LOCAL, _REASON}),
    latency_profile="slow",
)


class ModelCatalog:
    """A registry of callable model configurations."""

    def __init__(self, models: Iterable[ModelConfig] = ()) -> None:
        self._models: dict[str, ModelConfig] = {}
        for model in models:
            self.register(model)

    def register(self, model: ModelConfig, *, replace: bool = False) -> ModelConfig:
        if model.key in self._models and not replace:
            raise DuplicateError(f"model {model.key!r} is already registered", model=model.key)
        self._models[model.key] = model
        return model

    def get(self, key: str) -> ModelConfig:
        model = self._models.get(key)
        if model is None:
            raise NotFoundError(
                f"model {key!r} is not in the catalog",
                model=key,
                available=sorted(self._models),
            )
        return model

    def try_get(self, key: str) -> ModelConfig | None:
        return self._models.get(key)

    def has(self, key: str) -> bool:
        return key in self._models

    def remove(self, key: str) -> None:
        if key not in self._models:
            raise NotFoundError(f"model {key!r} is not in the catalog", model=key)
        del self._models[key]

    def list_models(self) -> tuple[ModelConfig, ...]:
        return tuple(m for _, m in sorted(self._models.items()))

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def by_provider(self, provider: Provider) -> tuple[ModelConfig, ...]:
        return tuple(m for m in self.list_models() if m.provider is provider)

    def with_capabilities(self, *capabilities: ModelCapability) -> tuple[ModelConfig, ...]:
        return tuple(m for m in self.list_models() if m.has(*capabilities))

    def embedding_models(self) -> tuple[ModelConfig, ...]:
        return self.with_capabilities(ModelCapability.EMBEDDING)

    def chat_models(self) -> tuple[ModelConfig, ...]:
        """Chat-capable models, excluding embedding-only entries.

        The exclusion matters: an embedding model advertises no CHAT capability,
        and offering one to the router as a completion candidate would produce a
        confusing provider error much later.
        """
        return tuple(
            m
            for m in self.with_capabilities(ModelCapability.CHAT)
            if not m.has(ModelCapability.EMBEDDING)
        )

    def __len__(self) -> int:
        return len(self._models)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._models

    def __iter__(self) -> Iterator[ModelConfig]:
        return iter(self.list_models())

    def __repr__(self) -> str:
        return f"<ModelCatalog models={len(self._models)}>"


#: Every model this reference implementation knows about.
ALL_MODELS: tuple[ModelConfig, ...] = (
    MOCK_FAST,
    MOCK_SMART,
    MOCK_EMBED,
    CLAUDE_OPUS,
    CLAUDE_SONNET,
    CLAUDE_HAIKU,
    GPT_4O,
    GPT_4O_MINI,
    GEMINI_FLASH,
    GEMINI_PRO,
    GROQ_GPT_OSS_120B,
    OPENAI_EMBED_SMALL,
    LLAMA_LOCAL,
    QWEN_LOCAL,
)

#: Mock-only catalog: the default for tests, benchmarks, and a keyless install.
MOCK_MODELS: tuple[ModelConfig, ...] = (MOCK_FAST, MOCK_SMART, MOCK_EMBED)


def build_catalog(*, mock_only: bool = False) -> ModelCatalog:
    """Construct a catalog.

    Args:
        mock_only: Restrict to mock models. Used by tests and the benchmark so a
            misconfiguration cannot cause a real, billable API call.
    """
    return ModelCatalog(MOCK_MODELS if mock_only else ALL_MODELS)
