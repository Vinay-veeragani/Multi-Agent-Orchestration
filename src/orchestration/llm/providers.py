"""Provider adapters: OpenAI-compatible, Anthropic, and Gemini.

Each adapter's only job is translation: engine request in, provider payload out,
provider body in, engine response out. Retry, budgeting, tracing and error
classification all live above this layer, so an adapter is small enough to verify
by reading.

Implemented directly over ``httpx`` rather than three vendor SDKs. The reasons
are concrete: one dependency instead of three, one place where timeouts and error
mapping are defined, and no exposure to three independent SDK release cadences
for what amounts to a JSON POST. The cost is that new provider features need
adapter work rather than arriving with an SDK upgrade -- an acceptable trade for a
system that only needs chat completion with structured output.

The OpenAI adapter doubles as the Ollama, vLLM, LM Studio and OpenRouter adapter,
since all of them expose the same ``/chat/completions`` shape.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from orchestration.domain.base import JsonDict
from orchestration.domain.enums import MessageRole, Provider
from orchestration.domain.model import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCallRequest,
)
from orchestration.errors import ProviderUnavailableError
from orchestration.llm.base import HttpLLMProvider, estimate_tokens

# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(HttpLLMProvider):
    """Adapter for the OpenAI ``/chat/completions`` API and its clones."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        provider: Provider = Provider.OPENAI,
        require_key: bool = True,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout_seconds=timeout_seconds)
        self.provider = provider
        # Local servers (Ollama, vLLM) need no credential; requiring one would
        # make the local path unusable for no security benefit.
        self._require_key = require_key

    def _auth_headers(self) -> JsonDict:
        if not self._require_key and not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self.require_api_key()}"}

    def _build_payload(self, request: LLMRequest) -> JsonDict:
        payload: JsonDict = {
            "model": request.model.model,
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                    **({"name": message.name} if message.name else {}),
                    **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
                }
                for message in request.messages
            ],
            "max_completion_tokens": request.effective_max_output_tokens,
            "temperature": request.effective_temperature,
        }
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.tools:
            payload["tools"] = list(request.tools)
        if request.response_schema is not None:
            # Structured Outputs requires the schema to forbid extra keys and to
            # mark every property required. Pydantic already emits `required` for
            # our models; adding additionalProperties keeps the API from
            # rejecting the request outright.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": str(request.response_schema.get("title", "response")),
                    "schema": _harden_schema(request.response_schema),
                    "strict": False,
                },
            }
        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        body = await self.post_json(
            "/chat/completions", self._build_payload(request), headers=self._auth_headers()
        )
        latency = time.perf_counter() - started

        choices = body.get("choices") or []
        if not choices:
            raise ProviderUnavailableError(
                "provider returned no choices", provider=self.provider.value
            )
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""

        usage_body = body.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(usage_body.get("prompt_tokens", 0))
            or sum(estimate_tokens(m.content) for m in request.messages),
            output_tokens=int(usage_body.get("completion_tokens", 0)) or estimate_tokens(content),
            cached_input_tokens=int(
                (usage_body.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            ),
        )

        return LLMResponse(
            content=content,
            model_key=request.model.key,
            provider=self.provider,
            usage=usage,
            cost_usd=request.model.estimate_cost(usage.input_tokens, usage.output_tokens),
            finish_reason=_map_finish_reason(choice.get("finish_reason")),
            tool_calls=_parse_openai_tool_calls(message.get("tool_calls") or []),
            latency_seconds=round(latency, 6),
            raw_metadata={
                "provider_model": body.get("model"),
                "response_headers": body.get("__response_headers__", {}),
            },
        )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        body = await self.post_json(
            "/embeddings",
            {
                "model": request.model.model,
                "input": list(request.texts),
                "dimensions": request.dimensions,
            },
            headers=self._auth_headers(),
        )
        data = body.get("data") or []
        usage_body = body.get("usage") or {}
        input_tokens = int(usage_body.get("prompt_tokens", 0))
        return EmbeddingResponse(
            vectors=tuple(tuple(float(v) for v in item["embedding"]) for item in data),
            model_key=request.model.key,
            provider=self.provider,
            usage=TokenUsage(input_tokens=input_tokens),
            cost_usd=request.model.estimate_cost(input_tokens, 0),
        )


def _parse_openai_tool_calls(raw: list[JsonDict]) -> tuple[ToolCallRequest, ...]:
    """Parse tool calls, tolerating malformed argument JSON.

    A model occasionally emits invalid JSON in ``arguments``. Surfacing that as
    an empty argument dict lets schema validation reject it with a clear message,
    which is far more useful than an opaque decode error from deep in the adapter.
    """
    calls: list[ToolCallRequest] = []
    for item in raw:
        function = item.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        arguments: JsonDict = {}
        if isinstance(raw_args, dict):
            arguments = dict(raw_args)
        elif isinstance(raw_args, str):
            try:
                decoded = json.loads(raw_args)
            except ValueError:
                decoded = None
            # A model that emits a JSON array or scalar here has misunderstood
            # the call; an empty dict lets schema validation say so clearly.
            if isinstance(decoded, dict):
                arguments = decoded
        calls.append(
            ToolCallRequest(
                id=str(item.get("id") or f"call_{len(calls)}"),
                name=str(function.get("name") or "unknown"),
                arguments=arguments,
            )
        )
    return tuple(calls)


FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]


def _map_finish_reason(raw: Any) -> FinishReason:
    """Normalise provider finish reasons onto the engine's closed set."""
    mapping: dict[str, FinishReason] = {
        "stop": "stop",
        "end_turn": "stop",
        "STOP": "stop",
        "length": "length",
        "max_tokens": "length",
        "MAX_TOKENS": "length",
        "tool_calls": "tool_calls",
        "tool_use": "tool_calls",
        "function_call": "tool_calls",
        "content_filter": "content_filter",
        "SAFETY": "content_filter",
    }
    return mapping.get(str(raw), "stop")


def _harden_schema(schema: JsonDict) -> JsonDict:
    """Recursively set ``additionalProperties: false`` on object schemas.

    Providers reject a structured-output schema that permits extra keys. Doing
    this here rather than in the domain model keeps the provider quirk in the
    adapter layer where it belongs.
    """
    hardened: JsonDict = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            hardened[key] = _harden_schema(value)
        elif isinstance(value, list):
            hardened[key] = [
                _harden_schema(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            hardened[key] = value
    if hardened.get("type") == "object" and "additionalProperties" not in hardened:
        hardened["additionalProperties"] = False
    return hardened


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicProvider(HttpLLMProvider):
    """Adapter for the Anthropic Messages API.

    Two shape differences from OpenAI drive the translation: the system prompt is
    a top-level field rather than a message, and structured output is obtained by
    forcing a single tool call rather than via a response-format parameter.
    """

    provider = Provider.ANTHROPIC

    #: Pinned so a server-side default change cannot alter behaviour silently.
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        base_url: str = "https://api.anthropic.com/v1",
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout_seconds=timeout_seconds)

    def _auth_headers(self) -> JsonDict:
        return {
            "x-api-key": self.require_api_key(),
            "anthropic-version": self.API_VERSION,
        }

    def _build_payload(self, request: LLMRequest) -> JsonDict:
        system_parts = [m.content for m in request.messages if m.role is MessageRole.SYSTEM]
        conversation = [
            {
                "role": "assistant" if m.role is MessageRole.ASSISTANT else "user",
                "content": m.content,
            }
            for m in request.messages
            if m.role is not MessageRole.SYSTEM
        ]
        # The API requires at least one message and rejects a leading assistant
        # turn, so a conversation that is only a system prompt gets a nudge.
        if not conversation:
            conversation = [{"role": "user", "content": "Proceed."}]

        payload: JsonDict = {
            "model": request.model.model,
            "messages": conversation,
            "max_tokens": request.effective_max_output_tokens,
            "temperature": request.effective_temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.stop:
            payload["stop_sequences"] = list(request.stop)

        if request.response_schema is not None:
            name = str(request.response_schema.get("title", "response"))
            payload["tools"] = [
                {
                    "name": name,
                    "description": "Return the result using this schema.",
                    "input_schema": request.response_schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": name}
        elif request.tools:
            payload["tools"] = [_openai_tool_to_anthropic(t) for t in request.tools]

        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        body = await self.post_json(
            "/messages", self._build_payload(request), headers=self._auth_headers()
        )
        latency = time.perf_counter() - started

        blocks = body.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                tool_calls.append(
                    ToolCallRequest(
                        id=str(block.get("id") or f"call_{len(tool_calls)}"),
                        name=str(block.get("name") or "unknown"),
                        arguments=dict(block.get("input") or {}),
                    )
                )

        # When structured output was requested, the payload arrives as the forced
        # tool's input. Serialising it into `content` means the caller's JSON
        # extraction path works identically across all providers.
        content = "\n".join(text_parts)
        if request.response_schema is not None and tool_calls:
            content = json.dumps(tool_calls[0].arguments)

        usage_body = body.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(usage_body.get("input_tokens", 0)),
            output_tokens=int(usage_body.get("output_tokens", 0)),
            cached_input_tokens=int(usage_body.get("cache_read_input_tokens", 0)),
        )

        return LLMResponse(
            content=content,
            model_key=request.model.key,
            provider=self.provider,
            usage=usage,
            cost_usd=request.model.estimate_cost(usage.input_tokens, usage.output_tokens),
            finish_reason=_map_finish_reason(body.get("stop_reason")),
            tool_calls=tuple(tool_calls),
            latency_seconds=round(latency, 6),
            raw_metadata={
                "provider_model": body.get("model"),
                "response_headers": body.get("__response_headers__", {}),
            },
        )


def _openai_tool_to_anthropic(tool: JsonDict) -> JsonDict:
    """Reshape an OpenAI function declaration into Anthropic's tool format."""
    function = tool.get("function") or tool
    return {
        "name": function.get("name", "unknown"),
        "description": function.get("description", ""),
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class GeminiProvider(HttpLLMProvider):
    """Adapter for the Gemini ``generateContent`` API.

    Gemini names things differently again: ``contents`` with ``parts``, ``model``
    instead of ``assistant``, and a ``systemInstruction`` field. Structured output
    is a ``responseMimeType`` plus ``responseSchema``.
    """

    provider = Provider.GEMINI

    def __init__(
        self,
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout_seconds=timeout_seconds)

    def _build_payload(self, request: LLMRequest) -> JsonDict:
        system_parts = [m.content for m in request.messages if m.role is MessageRole.SYSTEM]
        contents = [
            {
                "role": "model" if m.role is MessageRole.ASSISTANT else "user",
                "parts": [{"text": m.content}],
            }
            for m in request.messages
            if m.role is not MessageRole.SYSTEM
        ]
        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Proceed."}]}]

        generation_config: JsonDict = {
            "maxOutputTokens": request.effective_max_output_tokens,
            "temperature": request.effective_temperature,
        }
        if request.stop:
            generation_config["stopSequences"] = list(request.stop)
        if request.response_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = _gemini_schema(request.response_schema)

        payload: JsonDict = {"contents": contents, "generationConfig": generation_config}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        # Gemini takes the credential as a header rather than a query parameter,
        # which keeps it out of URLs and therefore out of access logs.
        body = await self.post_json(
            f"/models/{request.model.model}:generateContent",
            self._build_payload(request),
            headers={"x-goog-api-key": self.require_api_key()},
        )
        latency = time.perf_counter() - started

        candidates = body.get("candidates") or []
        if not candidates:
            raise ProviderUnavailableError(
                "gemini returned no candidates", provider=self.provider.value
            )
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        content = "".join(str(p.get("text", "")) for p in parts)

        usage_body = body.get("usageMetadata") or {}
        usage = TokenUsage(
            input_tokens=int(usage_body.get("promptTokenCount", 0)),
            output_tokens=int(usage_body.get("candidatesTokenCount", 0)),
            cached_input_tokens=int(usage_body.get("cachedContentTokenCount", 0)),
        )

        return LLMResponse(
            content=content,
            model_key=request.model.key,
            provider=self.provider,
            usage=usage,
            cost_usd=request.model.estimate_cost(usage.input_tokens, usage.output_tokens),
            finish_reason=_map_finish_reason(candidate.get("finishReason")),
            latency_seconds=round(latency, 6),
            raw_metadata={"response_headers": body.get("__response_headers__", {})},
        )


#: JSON Schema keywords Gemini's responseSchema does not accept.
_GEMINI_UNSUPPORTED = frozenset(
    {
        "$defs",
        "$ref",
        "$schema",
        "additionalProperties",
        "const",
        "default",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "title",
        "anyOf",
        "oneOf",
        "allOf",
        "discriminator",
    }
)


def _gemini_schema(schema: JsonDict) -> JsonDict:
    """Strip keywords Gemini rejects, resolving ``$ref`` inline first.

    Lossy by necessity: Gemini's schema dialect is a subset. The engine still
    validates the reply against the *full* Pydantic schema afterwards, so a
    weaker generation-time constraint costs an occasional repair attempt rather
    than correctness.
    """
    defs = schema.get("$defs", {})
    return _gemini_node(schema, defs, depth=0)


def _gemini_node(node: JsonDict, defs: JsonDict, *, depth: int) -> JsonDict:
    if depth > 8 or not isinstance(node, dict):
        return {"type": "string"}

    if "$ref" in node:
        ref = str(node["$ref"]).rsplit("/", 1)[-1]
        target = defs.get(ref)
        if isinstance(target, dict):
            return _gemini_node(target, defs, depth=depth + 1)
        return {"type": "string"}

    for union_key in ("anyOf", "oneOf"):
        options = node.get(union_key)
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict) and option.get("type") != "null":
                    return _gemini_node(option, defs, depth=depth + 1)
            return {"type": "string"}

    result: JsonDict = {}
    for key, value in node.items():
        if key in _GEMINI_UNSUPPORTED:
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {
                name: _gemini_node(sub, defs, depth=depth + 1)
                for name, sub in value.items()
                if isinstance(sub, dict)
            }
        elif (key == "items" and isinstance(value, dict)) or isinstance(value, dict):
            result[key] = _gemini_node(value, defs, depth=depth + 1)
        else:
            result[key] = value

    if "type" not in result:
        result["type"] = "object" if "properties" in result else "string"
    return result


# ---------------------------------------------------------------------------
# Ollama convenience
# ---------------------------------------------------------------------------


def ollama_provider(
    *, base_url: str = "http://127.0.0.1:11434/v1", timeout_seconds: float = 120.0
) -> OpenAICompatibleProvider:
    """Local Ollama, via its OpenAI-compatible endpoint.

    A longer default timeout than the hosted providers: local inference on CPU is
    routinely slower than an API call, and a 60s ceiling would make the local
    path look broken when it is merely slow.
    """
    return OpenAICompatibleProvider(
        base_url=base_url,
        api_key=None,
        timeout_seconds=timeout_seconds,
        provider=Provider.OLLAMA,
        require_key=False,
    )
