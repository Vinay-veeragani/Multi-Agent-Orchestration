"""Provider factory and the client facade the engine actually calls.

:class:`LLMClient` is the single entry point for every LLM call in the system. It
owns three responsibilities that would otherwise be duplicated at each call site:

1. **Dispatch** -- pick the adapter for a model's provider, lazily, so an install
   with only one credential never constructs the others.
2. **Retry** -- apply a :class:`RetryPolicy` around the call, using the error
   taxonomy to decide retryability. Sleeping happens here, and nowhere else.
3. **Structured output** -- expose ``complete_structured`` so callers get a
   validated Pydantic object instead of a string they have to parse.

Keeping retry here rather than inside each adapter means the *policy* is
configurable per call while the *classification* stays uniform.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel

from orchestration.config import Settings, get_settings
from orchestration.domain.enums import Provider
from orchestration.domain.model import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMRequest,
    LLMResponse,
)
from orchestration.domain.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from orchestration.errors import ConfigurationError, is_retryable
from orchestration.llm.base import LLMProvider, StructuredResult, generate_structured
from orchestration.llm.mock import MockProvider
from orchestration.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    ollama_provider,
)

TModel = TypeVar("TModel", bound=BaseModel)

#: Builds a provider on demand. Called at most once per provider per client.
ProviderBuilder = Callable[[], LLMProvider]


def build_provider_builders(settings: Settings) -> dict[Provider, ProviderBuilder]:
    """Map each provider to a lazy constructor.

    Lazy because constructing an adapter validates its credential. Eager
    construction would make an install with only an Anthropic key fail at
    startup over a missing OpenAI key it never intended to use.
    """
    return {
        Provider.MOCK: lambda: MockProvider(),
        Provider.OPENAI: lambda: OpenAICompatibleProvider(
            base_url=settings.openai_base_url,
            api_key=(
                settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
            ),
            timeout_seconds=settings.llm_timeout_seconds,
            provider=Provider.OPENAI,
        ),
        Provider.ANTHROPIC: lambda: AnthropicProvider(
            base_url=settings.anthropic_base_url,
            api_key=(
                settings.anthropic_api_key.get_secret_value()
                if settings.anthropic_api_key
                else None
            ),
            timeout_seconds=settings.llm_timeout_seconds,
        ),
        Provider.GEMINI: lambda: GeminiProvider(
            base_url=settings.gemini_base_url,
            api_key=(
                settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
            ),
            timeout_seconds=settings.llm_timeout_seconds,
        ),
        Provider.OLLAMA: lambda: ollama_provider(
            base_url=settings.ollama_base_url,
            timeout_seconds=max(settings.llm_timeout_seconds, 120.0),
        ),
    }


def configured_providers(settings: Settings) -> tuple[str, ...]:
    """Providers this deployment can actually reach.

    Handed to the model router so it never selects a model whose credential is
    absent. Mock is always available; Ollama is assumed reachable if configured,
    since a local endpoint has no credential to check.
    """
    available = ["mock"]
    if settings.openai_api_key:
        available.append("openai")
    if settings.anthropic_api_key:
        available.append("anthropic")
    if settings.gemini_api_key:
        available.append("gemini")
    if settings.ollama_base_url:
        available.append("ollama")
    return tuple(available)


class LLMClient:
    """Retry-wrapped, provider-dispatching facade over the adapters.

    Args:
        builders: Provider constructors. Defaults to those derived from settings.
        retry_policy: Default policy; overridable per call.
        rng: Random source for jitter. Injectable so tests are deterministic.
        sleep: Delay function. Injectable so a retry test does not actually wait.
    """

    def __init__(
        self,
        builders: dict[Provider, ProviderBuilder] | None = None,
        *,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        # `is None`, not truthiness: an explicitly empty mapping means "no
        # providers", and falling back to the defaults there would silently give
        # a caller network-capable adapters it deliberately withheld.
        self._builders = (
            build_provider_builders(get_settings()) if builders is None else dict(builders)
        )
        self._instances: dict[Provider, LLMProvider] = {}
        self._retry_policy = retry_policy
        self._rng = rng
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()

    @classmethod
    def mock(cls, provider: MockProvider | None = None, **kwargs: object) -> LLMClient:
        """Client backed solely by a mock provider.

        The constructor tests and benchmarks use: it cannot reach a network, so a
        misconfiguration cannot produce a billable call.
        """
        instance = provider or MockProvider()
        client = cls({Provider.MOCK: lambda: instance}, **kwargs)  # type: ignore[arg-type]
        return client

    def register_provider(self, provider: Provider, instance: LLMProvider) -> None:
        """Install a pre-built provider, replacing any lazy builder."""
        self._instances[provider] = instance

    async def provider_for(self, provider: Provider) -> LLMProvider:
        """Return (constructing if needed) the adapter for ``provider``."""
        existing = self._instances.get(provider)
        if existing is not None:
            return existing
        async with self._lock:
            # Re-check: another coroutine may have built it while we waited.
            existing = self._instances.get(provider)
            if existing is not None:
                return existing
            builder = self._builders.get(provider)
            if builder is None:
                raise ConfigurationError(
                    f"no adapter is registered for provider {provider.value!r}",
                    provider=provider.value,
                    available=[p.value for p in self._builders],
                )
            instance = builder()
            self._instances[provider] = instance
            return instance

    # -- calls -------------------------------------------------------------

    async def complete(
        self, request: LLMRequest, *, retry_policy: RetryPolicy | None = None
    ) -> LLMResponse:
        """Perform a completion with retry.

        Retry decisions come from the policy plus the error taxonomy; this method
        contributes only the sleeping and the attempt counting.
        """
        policy = retry_policy or self._retry_policy
        provider = await self.provider_for(request.model.provider)

        attempt = 0
        while True:
            attempt += 1
            try:
                return await provider.complete(request)
            except Exception as exc:
                if not policy.should_retry(attempt, exc):
                    raise
                delay = policy.backoff_for(attempt, exc, rng=self._rng)
                if delay > 0:
                    await self._sleep(delay)

    async def complete_structured(
        self,
        request: LLMRequest,
        schema_model: type[TModel],
        *,
        retry_policy: RetryPolicy | None = None,
        max_repairs: int = 1,
    ) -> tuple[TModel, StructuredResult]:
        """Obtain a validated ``schema_model`` instance.

        Retry (for transport faults) and repair (for schema faults) compose here:
        each individual attempt inside ``generate_structured`` is itself
        retry-wrapped, so a rate limit during a repair attempt is handled as a
        rate limit rather than being mistaken for a schema failure.
        """
        policy = retry_policy or self._retry_policy
        provider = await self.provider_for(request.model.provider)
        wrapped = _RetryingProvider(provider, policy, self._rng, self._sleep)
        return await generate_structured(wrapped, request, schema_model, max_repairs=max_repairs)

    async def embed(
        self, request: EmbeddingRequest, *, retry_policy: RetryPolicy | None = None
    ) -> EmbeddingResponse:
        policy = retry_policy or self._retry_policy
        provider = await self.provider_for(request.model.provider)

        attempt = 0
        while True:
            attempt += 1
            try:
                return await provider.embed(request)
            except Exception as exc:
                if not policy.should_retry(attempt, exc):
                    raise
                delay = policy.backoff_for(attempt, exc, rng=self._rng)
                if delay > 0:
                    await self._sleep(delay)

    async def aclose(self) -> None:
        """Close every constructed adapter."""
        for instance in list(self._instances.values()):
            await instance.aclose()
        self._instances.clear()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class _RetryingProvider(LLMProvider):
    """Wraps a provider so each call inside a structured generation retries.

    Exists because :func:`generate_structured` drives the conversation and must
    not also own retry logic -- separating them keeps "the transport failed" and
    "the model produced invalid output" as distinct, separately counted events.
    """

    def __init__(
        self,
        inner: LLMProvider,
        policy: RetryPolicy,
        rng: random.Random | None,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self.provider = inner.provider
        self._inner = inner
        self._policy = policy
        self._rng = rng
        self._sleep = sleep

    async def complete(self, request: LLMRequest) -> LLMResponse:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._inner.complete(request)
            except Exception as exc:
                if not self._policy.should_retry(attempt, exc):
                    raise
                delay = self._policy.backoff_for(attempt, exc, rng=self._rng)
                if delay > 0:
                    await self._sleep(delay)

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        return await self._inner.embed(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def retryable_reason(exc: BaseException) -> str:
    """Human-readable retryability verdict, for events and log lines."""
    return "retryable" if is_retryable(exc) else "terminal"
