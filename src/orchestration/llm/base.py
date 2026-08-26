"""LLM provider contract and the structured-output layer.

Everything above this module speaks :class:`LLMRequest` / :class:`LLMResponse`
and never touches a provider-specific payload. That is what makes the engine
provider-agnostic in more than name: swapping OpenAI for Anthropic changes one
adapter and nothing else.

Two responsibilities live here:

:class:`LLMProvider`
    The interface every adapter implements, plus shared HTTP error mapping so
    each adapter does not reinvent "which status code means retry".

:func:`generate_structured`
    Obtaining a validated Pydantic object from a model. This is the mechanism
    that keeps the supervisor from ever parsing free-form prose: the model is
    asked for JSON matching a schema, the reply is validated, and on failure the
    *validation errors themselves* are fed back for exactly one repair attempt.

The repair attempt is deliberately not a retry. A retry re-sends the same
request hoping for different luck; a repair sends a different request containing
new information. They are counted and reported separately, because a supervisor
that habitually needs repairs is a signal worth seeing rather than hiding.
"""

from __future__ import annotations

import abc
import json
import re
import time
from typing import Any, Final, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from orchestration.domain.base import JsonDict
from orchestration.domain.enums import Provider
from orchestration.domain.model import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMRequest,
    LLMResponse,
    Message,
)
from orchestration.errors import (
    ConfigurationError,
    NetworkError,
    ProviderUnavailableError,
    RateLimitError,
    SchemaViolationError,
)

TModel = TypeVar("TModel", bound=BaseModel)

#: Header names whose values must never appear in a log, span, or event.
SENSITIVE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)

#: Response header names worth keeping for debugging. An allowlist rather than a
#: denylist: a new provider adding a header that happens to carry a token cannot
#: leak through a filter it was never considered against.
SAFE_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "x-request-id",
        "request-id",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "retry-after",
        "anthropic-ratelimit-requests-remaining",
        "anthropic-ratelimit-tokens-remaining",
    }
)


def safe_headers(headers: Any) -> JsonDict:
    """Filter response headers down to the debugging allowlist."""
    try:
        items = dict(headers)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {}
    return {k: v for k, v in items.items() if k.lower() in SAFE_RESPONSE_HEADERS}


class LLMProvider(abc.ABC):
    """Interface for a chat-completion provider."""

    #: Which provider family this adapter serves.
    provider: Provider

    @abc.abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Perform a completion.

        Raises:
            RateLimitError: Provider returned 429.
            ProviderUnavailableError: Provider returned 5xx.
            NetworkError: Transport-level failure.
            ConfigurationError: Missing credential or unusable configuration.
        """

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Produce embeddings.

        Optional: providers that cannot embed raise. Declared here rather than in
        a separate protocol because the evidence store needs to ask any provider
        for embeddings without knowing which kind it holds.
        """
        raise ConfigurationError(
            f"provider {self.provider.value!r} does not support embeddings",
            provider=self.provider.value,
        )

    async def aclose(self) -> None:  # noqa: B027 - intentional no-op default
        """Release any held connections. Idempotent.

        Not abstract: an in-memory provider (the mock) holds nothing to close,
        and forcing every such adapter to write an empty override adds noise
        without adding safety.
        """

    # -- shared HTTP error mapping ----------------------------------------

    @staticmethod
    def raise_for_status(response: httpx.Response, *, provider: str) -> None:
        """Translate an HTTP error status into the engine's error taxonomy.

        Centralised so every adapter classifies retryability identically. The
        response body is truncated into the error context: a provider error
        message is genuinely useful for debugging, but an unbounded one would
        make a log line unreadable.
        """
        status = response.status_code
        if status < 400:
            return

        detail = response.text[:500] if response.text else ""

        if status == 429:
            header = response.headers.get("retry-after")
            retry_after: float | None = None
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            raise RateLimitError(
                f"{provider} rate limit exceeded",
                retry_after=retry_after,
                provider=provider,
                status_code=status,
                detail=detail,
            )

        if status in {408, 409, 425} or status >= 500:
            raise ProviderUnavailableError(
                f"{provider} returned {status}",
                provider=provider,
                status_code=status,
                detail=detail,
            )

        if status in {401, 403}:
            raise ConfigurationError(
                f"{provider} rejected the credential ({status})",
                provider=provider,
                status_code=status,
            )

        # 400, 404, 422 and friends: a malformed request will stay malformed.
        raise ConfigurationError(
            f"{provider} rejected the request ({status})",
            provider=provider,
            status_code=status,
            detail=detail,
        )

    @staticmethod
    def wrap_transport_error(exc: Exception, *, provider: str) -> NetworkError:
        """Convert an httpx transport failure into a retryable engine error."""
        return NetworkError(
            f"{provider} request failed at the transport layer",
            provider=provider,
            detail=type(exc).__name__,
        )


class HttpLLMProvider(LLMProvider):
    """Base class for adapters that talk HTTP.

    Holds one shared :class:`httpx.AsyncClient` for connection reuse -- creating a
    client per call would defeat keep-alive and dominate latency on short
    completions.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float = 60.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._default_headers = default_headers or {}
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._default_headers,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def require_api_key(self) -> str:
        """Return the credential, or fail with an actionable message."""
        if not self._api_key:
            raise ConfigurationError(
                f"provider {self.provider.value!r} requires an API key but none is configured",
                provider=self.provider.value,
                hint=f"set ORCH_{self.provider.value.upper()}_API_KEY",
            )
        return self._api_key

    async def post_json(
        self, path: str, payload: JsonDict, *, headers: JsonDict | None = None
    ) -> JsonDict:
        """POST JSON and return the decoded body, mapping errors to the taxonomy."""
        try:
            response = await self.client.post(path, json=payload, headers=headers or {})
        except httpx.HTTPError as exc:
            raise self.wrap_transport_error(exc, provider=self.provider.value) from exc

        self.raise_for_status(response, provider=self.provider.value)

        try:
            body: JsonDict = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(
                f"{self.provider.value} returned a non-JSON body",
                provider=self.provider.value,
            ) from exc
        body["__response_headers__"] = safe_headers(response.headers)
        return body


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------

#: Matches a ```json fenced block, which models emit even when told not to.
_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)\s*```", re.DOTALL | re.IGNORECASE
)


def extract_json_object(text: str) -> JsonDict:
    """Recover a JSON object from a model reply.

    Models wrap JSON in prose and code fences despite instructions. Three
    strategies, in order of preference:

    1. The whole reply parses as JSON.
    2. A fenced code block parses as JSON.
    3. The outermost balanced ``{...}`` span parses as JSON.

    Being tolerant *here* is safe because the result is validated against a
    strict schema immediately afterwards -- leniency in extraction does not mean
    leniency in acceptance.

    Raises:
        SchemaViolationError: If no JSON object can be recovered.
    """
    stripped = text.strip()

    if stripped:
        try:
            parsed = json.loads(stripped)
        except ValueError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed
            raise SchemaViolationError(
                "model returned JSON that is not an object",
                received_type=type(parsed).__name__,
            )

    fenced = _FENCE_RE.search(text)
    if fenced:
        try:
            parsed = json.loads(fenced.group("body"))
        except ValueError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

    span = _outermost_object_span(text)
    if span is not None:
        try:
            parsed = json.loads(span)
        except ValueError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

    raise SchemaViolationError(
        "model reply contained no parsable JSON object",
        preview=text[:200],
    )


def _outermost_object_span(text: str) -> str | None:
    """Return the outermost balanced ``{...}`` substring, respecting strings.

    A naive ``text[text.find('{'):text.rfind('}')+1]`` breaks whenever a brace
    appears inside a string value, which happens constantly in generated
    content. Tracking string state and escapes costs a few lines and removes a
    whole class of intermittent parse failures.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def format_validation_errors(exc: ValidationError, *, limit: int = 12) -> list[str]:
    """Render Pydantic errors as instructions a model can act on.

    Phrased as field paths plus messages rather than dumped as a Python repr:
    the text goes into a prompt, so it should read as a correction list.
    """
    problems: list[str] = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(p) for p in error["loc"]) or "<root>"
        problems.append(f"{location}: {error['msg']}")
    return problems


class StructuredResult(BaseModel):
    """A structured generation outcome, including how it was reached."""

    model_config = {"arbitrary_types_allowed": True}

    value: Any
    attempts: int
    repaired: bool
    raw_outputs: tuple[str, ...]
    validation_errors: tuple[str, ...]
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    latency_seconds: float


async def generate_structured(
    provider: LLMProvider,
    request: LLMRequest,
    schema_model: type[TModel],
    *,
    max_repairs: int = 1,
) -> tuple[TModel, StructuredResult]:
    """Obtain a validated instance of ``schema_model`` from the provider.

    Args:
        provider: The adapter to call.
        request: The base request. Its ``response_schema`` is populated from
            ``schema_model`` so the schema shown to the model and the schema used
            to validate can never diverge.
        schema_model: The Pydantic type to validate against.
        max_repairs: Repair attempts after the first failure. One is the default:
            a model that cannot satisfy the schema when handed its own
            validation errors is unlikely to succeed on a third try, and the
            engine has a deterministic fallback for that case.

    Returns:
        The validated object and a :class:`StructuredResult` audit record.

    Raises:
        SchemaViolationError: If no attempt produced a valid object. Terminal by
            classification -- the caller (the supervisor) falls back to its
            heuristic router rather than looping.
    """
    schema = schema_model.model_json_schema(mode="validation")
    attempt_request = request.model_copy(update={"response_schema": schema})

    raw_outputs: list[str] = []
    all_errors: list[str] = []
    input_tokens = 0
    output_tokens = 0
    cost = 0.0
    started = time.perf_counter()

    for attempt in range(1, max_repairs + 2):
        response = await provider.complete(attempt_request)
        raw_outputs.append(response.content)
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        cost += response.cost_usd

        try:
            payload = extract_json_object(response.content)
            value = schema_model.model_validate(payload)
        except (SchemaViolationError, ValidationError) as exc:
            problems = (
                format_validation_errors(exc) if isinstance(exc, ValidationError) else [exc.message]
            )
            all_errors.extend(problems)

            if attempt > max_repairs:
                raise SchemaViolationError(
                    f"model failed to produce valid {schema_model.__name__} "
                    f"after {attempt} attempt(s)",
                    model=request.model.key,
                    attempts=attempt,
                    problems=problems,
                    preview=response.content[:300],
                ) from exc

            attempt_request = _build_repair_request(attempt_request, response.content, problems)
            continue

        return value, StructuredResult(
            value=value,
            attempts=attempt,
            repaired=attempt > 1,
            raw_outputs=tuple(raw_outputs),
            validation_errors=tuple(all_errors),
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            total_cost_usd=round(cost, 8),
            latency_seconds=round(time.perf_counter() - started, 6),
        )

    # Unreachable: the loop either returns or raises.
    raise AssertionError("generate_structured exited its loop without a result")


def _build_repair_request(
    request: LLMRequest, previous_output: str, problems: list[str]
) -> LLMRequest:
    """Append the failed output and its validation errors to the conversation.

    The model is shown what it produced and precisely why that was rejected.
    This is why the repair is a genuinely different request rather than a retry.
    """
    correction = "\n".join(f"- {p}" for p in problems)
    messages = (
        *request.messages,
        Message.assistant(previous_output[:4_000]),
        Message.user(
            "Your previous response was rejected by schema validation with these "
            f"errors:\n{correction}\n\n"
            "Return only a corrected JSON object that satisfies the schema. "
            "Do not include any explanation, prose, or code fences."
        ),
    )
    return request.with_messages(messages)


def estimate_tokens(text: str) -> int:
    """Rough token count for budgeting when a provider reports no usage.

    Uses ~4 characters per token, the widely-used English approximation. It is an
    estimate and is only ever used when the provider returned nothing better;
    real usage figures always take precedence so cost accounting stays honest.
    """
    return max(1, len(text) // 4)
