"""Tests for the LLM layer: JSON extraction, structured output, mock, adapters.

The most important behaviour under test is :func:`generate_structured`. It is the
mechanism that stops the engine ever acting on unvalidated model output, and its
repair path is what distinguishes "the model wrote bad JSON" from "the network
failed" -- two things that must never be conflated in the metrics.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from orchestration.domain.enums import ModelCapability, Provider
from orchestration.domain.model import (
    EmbeddingRequest,
    LLMRequest,
    Message,
    ModelConfig,
    RoutingCriteria,
)
from orchestration.domain.retry import NO_RETRY_POLICY, RetryPolicy
from orchestration.domain.routing import RoutingDecision
from orchestration.errors import (
    ConfigurationError,
    EngineTimeoutError,
    RateLimitError,
    SchemaViolationError,
)
from orchestration.llm.base import (
    LLMProvider,
    extract_json_object,
    generate_structured,
)
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import (
    CONTENT_FAULT_PAYLOADS,
    Fault,
    MockProvider,
    MockRule,
    MockScript,
    agent_output,
    routing_decision,
)
from orchestration.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    _gemini_schema,
    _harden_schema,
    _map_finish_reason,
    _parse_openai_tool_calls,
    ollama_provider,
)
from orchestration.models.catalog import (
    GROQ_GPT_OSS_120B,
    LLAMA_LOCAL,
    MOCK_FAST,
    MOCK_SMART,
    build_catalog,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def model() -> ModelConfig:
    return MOCK_FAST


def _request(model: ModelConfig, text: str = "do the thing", system: str = "") -> LLMRequest:
    messages = [Message.system(system)] if system else []
    messages.append(Message.user(text))
    return LLMRequest(messages=tuple(messages), model=model)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


class TestJsonExtraction:
    def test_plain_json(self) -> None:
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_whitespace_padded(self) -> None:
        assert extract_json_object('\n\n  {"a": 1}  \n') == {"a": 1}

    def test_fenced_block(self) -> None:
        text = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
        assert extract_json_object(text) == {"a": 1}

    def test_unlabelled_fence(self) -> None:
        assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}

    def test_embedded_in_prose(self) -> None:
        assert extract_json_object('Sure. {"a": 1} That is my answer.') == {"a": 1}

    def test_braces_inside_string_values(self) -> None:
        """A naive find/rfind slice breaks on this, which happens constantly."""
        text = 'Result: {"note": "use {placeholder} here", "b": 2} done'
        assert extract_json_object(text) == {"note": "use {placeholder} here", "b": 2}

    def test_escaped_quotes_inside_strings(self) -> None:
        text = r'{"note": "she said \"hi\" and {left}", "n": 1}'
        assert extract_json_object(text) == {"note": 'she said "hi" and {left}', "n": 1}

    def test_nested_objects(self) -> None:
        assert extract_json_object('prefix {"a": {"b": {"c": 3}}} suffix') == {"a": {"b": {"c": 3}}}

    def test_json_array_is_rejected(self) -> None:
        """The contract is an object; an array is a different shape, not a near-miss."""
        with pytest.raises(SchemaViolationError, match="not an object"):
            extract_json_object("[1, 2, 3]")

    @pytest.mark.parametrize("text", ["", "   ", "no json here at all", "{unterminated"])
    def test_unparsable_raises(self, text: str) -> None:
        with pytest.raises(SchemaViolationError):
            extract_json_object(text)

    def test_error_includes_a_preview_for_debugging(self) -> None:
        with pytest.raises(SchemaViolationError) as info:
            extract_json_object("I refuse to answer that question.")
        assert "refuse" in info.value.context["preview"]


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class TestGenerateStructured:
    async def test_valid_first_attempt(self, model: ModelConfig) -> None:
        provider = MockProvider(
            [MockRule(name="r", responses=(routing_decision("finalize", answer="done"),))]
        )
        decision, result = await generate_structured(provider, _request(model), RoutingDecision)
        assert decision.action.value == "finalize"
        assert result.attempts == 1
        assert result.repaired is False

    async def test_repairs_malformed_json(self, model: ModelConfig) -> None:
        """The repair sends the validation errors back, not the same request."""
        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        CONTENT_FAULT_PAYLOADS["malformed_json"],
                        routing_decision("finalize", answer="recovered"),
                    ),
                )
            ]
        )
        decision, result = await generate_structured(provider, _request(model), RoutingDecision)
        assert decision.answer == "recovered"
        assert result.attempts == 2
        assert result.repaired is True
        assert result.validation_errors

    async def test_repairs_wrong_schema(self, model: ModelConfig) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        CONTENT_FAULT_PAYLOADS["wrong_schema"],
                        routing_decision("finalize", answer="ok"),
                    ),
                )
            ]
        )
        _, result = await generate_structured(provider, _request(model), RoutingDecision)
        assert result.repaired is True

    async def test_repair_prompt_contains_the_validation_errors(self, model: ModelConfig) -> None:
        """The model must be told precisely what was wrong to have a chance."""
        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        json.dumps({"action": "delegate", "reason": "r"}),
                        routing_decision("finalize", answer="ok"),
                    ),
                )
            ]
        )
        await generate_structured(provider, _request(model), RoutingDecision)
        second = provider.calls[1]
        assert "rejected by schema validation" in second.user_preview

    async def test_gives_up_after_the_repair_budget(self, model: ModelConfig) -> None:
        provider = MockProvider(
            [MockRule(name="r", responses=(CONTENT_FAULT_PAYLOADS["prose_instead_of_json"],))]
        )
        with pytest.raises(SchemaViolationError) as info:
            await generate_structured(provider, _request(model), RoutingDecision)
        assert info.value.context["attempts"] == 2
        assert info.value.retryable is False, "a schema failure must not trigger a retry"

    async def test_zero_repairs_fails_immediately(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", responses=(CONTENT_FAULT_PAYLOADS["empty"],))])
        with pytest.raises(SchemaViolationError):
            await generate_structured(provider, _request(model), RoutingDecision, max_repairs=0)
        assert provider.call_count == 1

    async def test_schema_is_derived_from_the_model(self, model: ModelConfig) -> None:
        """The schema shown to the provider cannot drift from the validator."""
        provider = MockProvider(
            [MockRule(name="r", responses=(routing_decision("fail", failure_reason="x"),))]
        )
        await generate_structured(provider, _request(model), RoutingDecision)
        assert provider.calls[0].requested_schema == "RoutingDecision"

    async def test_accumulates_cost_across_attempts(self, model: ModelConfig) -> None:
        """A repair is not free, and the accounting must say so."""
        priced = MOCK_FAST.model_copy(
            update={"input_cost_per_mtok": 1.0, "output_cost_per_mtok": 2.0}
        )
        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        CONTENT_FAULT_PAYLOADS["malformed_json"],
                        routing_decision("finalize", answer="ok"),
                    ),
                )
            ]
        )
        _, result = await generate_structured(provider, _request(priced), RoutingDecision)
        assert result.total_cost_usd > 0
        assert result.total_input_tokens > 0


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------


class TestMockProvider:
    async def test_is_deterministic(self, model: ModelConfig) -> None:
        """The property the entire benchmark rests on."""
        first = await MockProvider().complete(_request(model, "same question"))
        second = await MockProvider().complete(_request(model, "same question"))
        assert first.content == second.content

    async def test_different_inputs_give_different_output(self, model: ModelConfig) -> None:
        a = await MockProvider().complete(_request(model, "question one"))
        b = await MockProvider().complete(_request(model, "question two"))
        assert a.content != b.content

    async def test_rule_matches_on_system_prompt(self, model: ModelConfig) -> None:
        provider = MockProvider(
            [
                MockRule(name="researcher", match_system="research", responses=("R",)),
                MockRule(name="critic", match_system="critic", responses=("C",)),
            ]
        )
        research = await provider.complete(_request(model, "go", system="You are a research bot"))
        critic = await provider.complete(_request(model, "go", system="You are a critic bot"))
        assert research.content == "R"
        assert critic.content == "C"

    async def test_more_specific_rule_wins(self, model: ModelConfig) -> None:
        """A targeted rule must beat a catch-all without manual priorities."""
        provider = MockProvider(
            [
                MockRule(name="catchall", responses=("generic",)),
                MockRule(
                    name="specific",
                    match_system="research",
                    match_user="pricing",
                    responses=("targeted",),
                ),
            ]
        )
        response = await provider.complete(_request(model, "find pricing", system="research agent"))
        assert response.content == "targeted"

    async def test_responses_advance_then_repeat(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", responses=("one", "two"))])
        outputs = [(await provider.complete(_request(model))).content for _ in range(4)]
        assert outputs == ["one", "two", "two", "two"]

    async def test_regex_matching(self, model: ModelConfig) -> None:
        provider = MockProvider(
            [MockRule(name="r", match_pattern=r"CRM\s+vendors?", responses=("matched",))]
        )
        response = await provider.complete(_request(model, "compare CRM vendors please"))
        assert response.content == "matched"

    async def test_strict_mode_rejects_unscripted_calls(self, model: ModelConfig) -> None:
        provider = MockProvider([], strict=True)
        with pytest.raises(ConfigurationError, match="strict and no rule matched"):
            await provider.complete(_request(model))

    async def test_synthesises_from_a_requested_schema(self, model: ModelConfig) -> None:
        """An unscripted structured call still yields something that validates."""
        provider = MockProvider()
        request = LLMRequest(
            messages=(Message.user("go"),),
            model=model,
            response_schema=RoutingDecision.model_json_schema(mode="validation"),
        )
        response = await provider.complete(request)
        payload = json.loads(response.content)
        assert "action" in payload

    async def test_records_calls_for_assertions(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", responses=("x",))])
        await provider.complete(_request(model, "hello"))
        assert provider.call_count == 1
        assert provider.calls[0].rule == "r"
        assert "hello" in provider.calls[0].user_preview

    async def test_reports_usage_and_cost(self) -> None:
        priced = MOCK_FAST.model_copy(
            update={"input_cost_per_mtok": 10.0, "output_cost_per_mtok": 30.0}
        )
        response = await MockProvider().complete(_request(priced, "x" * 400))
        assert response.usage.input_tokens > 0
        assert response.cost_usd > 0

    async def test_explicit_usage_overrides_the_estimate(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", responses=("x",), usage=(123, 45))])
        response = await provider.complete(_request(model))
        assert response.usage.input_tokens == 123
        assert response.usage.output_tokens == 45


class TestFaultInjection:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("timeout", EngineTimeoutError),
            ("rate_limit", RateLimitError),
        ],
    )
    async def test_transport_faults_raise(
        self, model: ModelConfig, kind: str, expected: type[Exception]
    ) -> None:
        provider = MockProvider(
            [MockRule(name="r", fault=Fault(kind=kind, attempts=(1,)))]  # type: ignore[arg-type]
        )
        with pytest.raises(expected):
            await provider.complete(_request(model))

    async def test_fault_applies_only_to_the_named_attempt(self, model: ModelConfig) -> None:
        """`attempts=(1,)` is the shape every recovery test needs."""
        provider = MockProvider(
            [MockRule(name="r", responses=("recovered",), fault=Fault("timeout", attempts=(1,)))]
        )
        with pytest.raises(EngineTimeoutError):
            await provider.complete(_request(model))
        response = await provider.complete(_request(model))
        assert response.content == "recovered"

    async def test_fault_can_target_later_attempts(self, model: ModelConfig) -> None:
        provider = MockProvider(
            [MockRule(name="r", responses=("ok",), fault=Fault("network", attempts=(2, 3)))]
        )
        assert (await provider.complete(_request(model))).content == "ok"
        for _ in range(2):
            with pytest.raises(Exception, match="connection reset"):
                await provider.complete(_request(model))

    async def test_rate_limit_carries_retry_after(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", fault=Fault("rate_limit", retry_after=2.5))])
        with pytest.raises(RateLimitError) as info:
            await provider.complete(_request(model))
        assert info.value.retry_after == 2.5

    async def test_content_faults_return_successfully(self, model: ModelConfig) -> None:
        """A corrupt reply is a different failure mode from an outage."""
        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(CONTENT_FAULT_PAYLOADS["malformed_json"],),
                    fault=Fault("malformed_json"),
                )
            ]
        )
        response = await provider.complete(_request(model))
        assert response.finish_reason == "stop"

    async def test_faulted_calls_are_recorded(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", fault=Fault("timeout"))])
        with pytest.raises(EngineTimeoutError):
            await provider.complete(_request(model))
        assert provider.calls[0].fault == "timeout"


class TestSyntheticLatency:
    async def test_latency_is_zero_by_default(self, model: ModelConfig) -> None:
        """Benchmarks must not silently include invented delays."""
        provider = MockProvider()
        started = asyncio.get_running_loop().time()
        await provider.complete(_request(model))
        assert asyncio.get_running_loop().time() - started < 0.05

    async def test_configured_latency_is_applied(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", responses=("x",), latency_seconds=0.05)])
        started = asyncio.get_running_loop().time()
        await provider.complete(_request(model))
        assert asyncio.get_running_loop().time() - started >= 0.04


class TestMockEmbeddings:
    async def test_deterministic_and_unit_norm(self) -> None:
        provider = MockProvider()
        request = EmbeddingRequest(texts=("alpha", "beta"), model=MOCK_FAST, dimensions=64)
        first = await provider.embed(request)
        second = await provider.embed(request)
        assert first.vectors == second.vectors
        for vector in first.vectors:
            assert len(vector) == 64
            assert abs(sum(v * v for v in vector) - 1.0) < 1e-6

    async def test_different_texts_give_different_vectors(self) -> None:
        result = await MockProvider().embed(
            EmbeddingRequest(texts=("alpha", "beta"), model=MOCK_FAST, dimensions=32)
        )
        assert result.vectors[0] != result.vectors[1]

    async def test_dimension_matches_the_request(self) -> None:
        result = await MockProvider().embed(
            EmbeddingRequest(texts=("x",), model=MOCK_FAST, dimensions=768)
        )
        assert result.dimension == 768


class TestMockScript:
    async def test_reads_as_a_script(self, model: ModelConfig) -> None:
        provider = (
            MockScript()
            .on_supervisor(
                routing_decision("parallel_delegate", agents=["research_agent", "pricing_agent"]),
                routing_decision("finalize", answer="report"),
            )
            .on_agent("research", agent_output("found vendors", evidence=["https://a"]))
            .build()
        )
        first = await provider.complete(_request(model, "go", system="You are the supervisor"))
        assert json.loads(first.content)["action"] == "parallel_delegate"
        second = await provider.complete(_request(model, "go", system="You are the supervisor"))
        assert json.loads(second.content)["action"] == "finalize"
        third = await provider.complete(_request(model, "go", system="research specialist"))
        assert json.loads(third.content)["content"] == "found vendors"

    async def test_supervisor_rule_outranks_an_agent_rule(self, model: ModelConfig) -> None:
        """A prompt mentioning both must still be treated as the supervisor."""
        provider = (
            MockScript()
            .on_supervisor(routing_decision("finalize", answer="sup"))
            .on_agent("research", agent_output("agent"))
            .build()
        )
        response = await provider.complete(
            _request(model, "go", system="You are the supervisor coordinating research")
        )
        assert json.loads(response.content)["action"] == "finalize"


# ---------------------------------------------------------------------------
# Client facade
# ---------------------------------------------------------------------------


class TestLLMClient:
    async def test_retries_a_transient_failure(self, model: ModelConfig) -> None:
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        provider = MockProvider(
            [MockRule(name="r", responses=("recovered",), fault=Fault("timeout", attempts=(1,)))]
        )
        client = LLMClient.mock(
            provider,
            retry_policy=RetryPolicy(max_attempts=3, initial_backoff_seconds=1.0, jitter="none"),
            sleep=fake_sleep,
        )
        response = await client.complete(_request(model))
        assert response.content == "recovered"
        assert slept == [1.0], "backoff was not applied exactly once"

    async def test_does_not_retry_a_terminal_failure(self, model: ModelConfig) -> None:
        calls = 0

        class Failing(LLMProvider):
            provider = Provider.MOCK

            async def complete(self, request: LLMRequest):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                raise ConfigurationError("bad credential")

        client = LLMClient({Provider.MOCK: Failing})
        with pytest.raises(ConfigurationError):
            await client.complete(_request(model))
        assert calls == 1

    async def test_exhausts_retries_then_raises(self, model: ModelConfig) -> None:
        async def fake_sleep(delay: float) -> None:
            return None

        provider = MockProvider([MockRule(name="r", fault=Fault("timeout", attempts=(1, 2, 3)))])
        client = LLMClient.mock(
            provider,
            retry_policy=RetryPolicy(max_attempts=3, initial_backoff_seconds=0.0, jitter="none"),
            sleep=fake_sleep,
        )
        with pytest.raises(EngineTimeoutError):
            await client.complete(_request(model))
        assert provider.call_count == 3

    async def test_retry_and_repair_compose(self, model: ModelConfig) -> None:
        """A rate limit during a repair must be handled as a rate limit."""

        async def fake_sleep(delay: float) -> None:
            return None

        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        CONTENT_FAULT_PAYLOADS["malformed_json"],
                        routing_decision("finalize", answer="ok"),
                    ),
                    fault=Fault("rate_limit", attempts=(2,)),
                )
            ]
        )
        client = LLMClient.mock(
            provider,
            retry_policy=RetryPolicy(max_attempts=3, initial_backoff_seconds=0.0, jitter="none"),
            sleep=fake_sleep,
        )
        decision, result = await client.complete_structured(_request(model), RoutingDecision)
        assert decision.answer == "ok"
        assert result.repaired is True

    async def test_unregistered_provider_fails_clearly(self) -> None:
        client = LLMClient({})
        with pytest.raises(ConfigurationError, match="no adapter is registered"):
            await client.provider_for(Provider.OPENAI)

    async def test_provider_is_built_once(self, model: ModelConfig) -> None:
        builds = 0

        def build() -> MockProvider:
            nonlocal builds
            builds += 1
            return MockProvider()

        client = LLMClient({Provider.MOCK: build})
        await asyncio.gather(*(client.complete(_request(model)) for _ in range(10)))
        assert builds == 1

    async def test_no_retry_policy_is_honoured(self, model: ModelConfig) -> None:
        provider = MockProvider([MockRule(name="r", fault=Fault("timeout", attempts=(1, 2)))])
        client = LLMClient.mock(provider, retry_policy=NO_RETRY_POLICY)
        with pytest.raises(EngineTimeoutError):
            await client.complete(_request(model))
        assert provider.call_count == 1

    async def test_context_manager_closes_providers(self, model: ModelConfig) -> None:
        async with LLMClient.mock() as client:
            await client.complete(_request(model))


# ---------------------------------------------------------------------------
# Adapter payload translation (no network)
# ---------------------------------------------------------------------------


class TestOpenAIAdapter:
    def test_builds_a_chat_completions_payload(self, model: ModelConfig) -> None:
        adapter = OpenAICompatibleProvider(api_key="sk-test")
        payload = adapter._build_payload(_request(model, "hello", system="be brief"))
        assert payload["model"] == model.model
        assert payload["messages"][0] == {"role": "system", "content": "be brief"}
        assert payload["max_completion_tokens"] == model.max_output_tokens

    def test_response_format_is_set_for_structured_output(self, model: ModelConfig) -> None:
        adapter = OpenAICompatibleProvider(api_key="sk-test")
        request = LLMRequest(
            messages=(Message.user("go"),),
            model=model,
            response_schema=RoutingDecision.model_json_schema(mode="validation"),
        )
        payload = adapter._build_payload(request)
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["name"] == "RoutingDecision"

    def test_missing_key_fails_with_an_actionable_message(self) -> None:
        adapter = OpenAICompatibleProvider(api_key=None)
        with pytest.raises(ConfigurationError, match="requires an API key") as info:
            adapter._auth_headers()
        assert "ORCH_OPENAI_API_KEY" in info.value.context["hint"]

    def test_local_endpoints_need_no_credential(self) -> None:
        """Requiring a key for Ollama would break the local path for no benefit."""
        adapter = ollama_provider()
        assert adapter._auth_headers() == {}
        assert adapter.provider is Provider.OLLAMA

    async def test_ollama_round_trip_against_a_real_openai_compatible_endpoint(
        self,
    ) -> None:
        """No local Ollama server is available in CI/this environment, so this
        proves the wiring the only way that's possible here: a real HTTP
        request/response cycle through httpx against a fake server that speaks
        Ollama's actual `/v1/chat/completions` shape (verified against Ollama's
        published OpenAI-compatibility docs), swapping only the transport --
        URL construction, headers, payload serialisation, and response parsing
        are all the same code a real Ollama server would exercise.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            assert "Authorization" not in request.headers
            body = json.loads(request.content)
            assert body["model"] == "llama3.1:8b"
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "hello from ollama"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                },
            )

        adapter = ollama_provider()
        adapter._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=adapter._base_url
        )
        try:
            response = await adapter.complete(_request(LLAMA_LOCAL, "hi"))
        finally:
            await adapter.aclose()

        assert response.content == "hello from ollama"
        assert response.usage.input_tokens == 12
        assert response.usage.output_tokens == 4

    def test_groq_missing_key_fails_with_an_actionable_message(self) -> None:
        """Groq reuses OpenAICompatibleProvider, so the hint must name Groq's
        own env var rather than a copy-pasted OpenAI one."""
        adapter = OpenAICompatibleProvider(provider=Provider.GROQ, api_key=None)
        with pytest.raises(ConfigurationError, match="requires an API key") as info:
            adapter._auth_headers()
        assert "ORCH_GROQ_API_KEY" in info.value.context["hint"]

    async def test_groq_round_trip_against_a_real_openai_compatible_endpoint(self) -> None:
        """Groq has no local server to hit in CI, so -- same approach as the
        Ollama test above -- this proves the wiring against a fake server
        speaking Groq's actual `/openai/v1/chat/completions` shape, swapping
        only the transport."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/openai/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer gsk-test"
            body = json.loads(request.content)
            assert body["model"] == "openai/gpt-oss-120b"
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "hello from groq"}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 3},
                },
            )

        adapter = OpenAICompatibleProvider(
            base_url="https://api.groq.com/openai/v1",
            api_key="gsk-test",
            provider=Provider.GROQ,
        )
        adapter._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=adapter._base_url
        )
        try:
            response = await adapter.complete(_request(GROQ_GPT_OSS_120B, "hi"))
        finally:
            await adapter.aclose()

        assert response.content == "hello from groq"
        assert response.provider is Provider.GROQ
        assert response.usage.input_tokens == 9
        assert response.usage.output_tokens == 3

    def test_tool_calls_are_parsed(self) -> None:
        calls = _parse_openai_tool_calls(
            [
                {
                    "id": "call_1",
                    "function": {"name": "web_search", "arguments": '{"query": "crm"}'},
                }
            ]
        )
        assert calls[0].name == "web_search"
        assert calls[0].arguments == {"query": "crm"}

    def test_malformed_tool_arguments_become_empty(self) -> None:
        """Schema validation then rejects it with a clear message."""
        calls = _parse_openai_tool_calls(
            [{"id": "c", "function": {"name": "t", "arguments": "{not json"}}]
        )
        assert calls[0].arguments == {}

    def test_non_object_tool_arguments_become_empty(self) -> None:
        calls = _parse_openai_tool_calls(
            [{"id": "c", "function": {"name": "t", "arguments": "[1,2]"}}]
        )
        assert calls[0].arguments == {}

    def test_schema_hardening_forbids_extra_properties(self) -> None:
        hardened = _harden_schema({"type": "object", "properties": {"a": {"type": "string"}}})
        assert hardened["additionalProperties"] is False

    def test_schema_hardening_recurses(self) -> None:
        hardened = _harden_schema(
            {
                "type": "object",
                "properties": {"inner": {"type": "object", "properties": {}}},
            }
        )
        assert hardened["properties"]["inner"]["additionalProperties"] is False


class TestAnthropicAdapter:
    def test_system_prompt_is_hoisted_out_of_messages(self, model: ModelConfig) -> None:
        adapter = AnthropicProvider(api_key="sk-ant")
        payload = adapter._build_payload(_request(model, "hello", system="be brief"))
        assert payload["system"] == "be brief"
        assert all(m["role"] != "system" for m in payload["messages"])

    def test_structured_output_forces_a_tool(self, model: ModelConfig) -> None:
        """Anthropic has no response_format, so a forced tool is the mechanism."""
        adapter = AnthropicProvider(api_key="sk-ant")
        request = LLMRequest(
            messages=(Message.user("go"),),
            model=model,
            response_schema=RoutingDecision.model_json_schema(mode="validation"),
        )
        payload = adapter._build_payload(request)
        assert payload["tool_choice"] == {"type": "tool", "name": "RoutingDecision"}
        assert payload["tools"][0]["name"] == "RoutingDecision"

    def test_a_system_only_conversation_gets_a_user_turn(self, model: ModelConfig) -> None:
        """The API rejects an empty message list."""
        adapter = AnthropicProvider(api_key="sk-ant")
        request = LLMRequest(messages=(Message.system("only system"),), model=model)
        payload = adapter._build_payload(request)
        assert payload["messages"] == [{"role": "user", "content": "Proceed."}]

    def test_api_version_is_pinned(self) -> None:
        assert AnthropicProvider.API_VERSION == "2023-06-01"


class TestGeminiAdapter:
    def test_builds_contents_and_system_instruction(self, model: ModelConfig) -> None:
        adapter = GeminiProvider(api_key="key")
        payload = adapter._build_payload(_request(model, "hello", system="be brief"))
        assert payload["systemInstruction"]["parts"][0]["text"] == "be brief"
        assert payload["contents"][0]["parts"][0]["text"] == "hello"

    def test_assistant_role_is_renamed_to_model(self, model: ModelConfig) -> None:
        adapter = GeminiProvider(api_key="key")
        request = LLMRequest(messages=(Message.user("a"), Message.assistant("b")), model=model)
        payload = adapter._build_payload(request)
        assert payload["contents"][1]["role"] == "model"

    def test_structured_output_sets_mime_type_and_schema(self, model: ModelConfig) -> None:
        adapter = GeminiProvider(api_key="key")
        request = LLMRequest(
            messages=(Message.user("go"),),
            model=model,
            response_schema=RoutingDecision.model_json_schema(mode="validation"),
        )
        config = adapter._build_payload(request)["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert "responseSchema" in config

    def test_unsupported_schema_keywords_are_stripped(self) -> None:
        cleaned = _gemini_schema(
            {
                "type": "object",
                "title": "Thing",
                "additionalProperties": False,
                "properties": {"a": {"type": "string", "default": "x"}},
            }
        )
        assert "title" not in cleaned
        assert "additionalProperties" not in cleaned
        assert "default" not in cleaned["properties"]["a"]

    def test_refs_are_resolved_inline(self) -> None:
        """Gemini rejects $ref, so a Pydantic nested model must be inlined."""
        cleaned = _gemini_schema(
            {
                "type": "object",
                "properties": {"inner": {"$ref": "#/$defs/Inner"}},
                "$defs": {"Inner": {"type": "object", "properties": {"n": {"type": "integer"}}}},
            }
        )
        assert cleaned["properties"]["inner"]["type"] == "object"
        assert "n" in cleaned["properties"]["inner"]["properties"]

    def test_the_real_routing_schema_survives_conversion(self) -> None:
        """The schema that actually matters, not a toy one."""
        cleaned = _gemini_schema(RoutingDecision.model_json_schema(mode="validation"))
        serialised = json.dumps(cleaned)
        assert "$ref" not in serialised
        assert "$defs" not in serialised
        assert "anyOf" not in serialised


class TestFinishReasonMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("stop", "stop"),
            ("end_turn", "stop"),
            ("length", "length"),
            ("max_tokens", "length"),
            ("MAX_TOKENS", "length"),
            ("tool_calls", "tool_calls"),
            ("tool_use", "tool_calls"),
            ("SAFETY", "content_filter"),
            ("something_new", "stop"),
        ],
    )
    def test_provider_reasons_normalise(self, raw: str, expected: str) -> None:
        assert _map_finish_reason(raw) == expected


# ---------------------------------------------------------------------------
# Model catalog and router
# ---------------------------------------------------------------------------


class TestModelCatalog:
    def test_mock_catalog_contains_only_mock_models(self) -> None:
        """A test or benchmark must not be able to trigger a billable call."""
        catalog = build_catalog(mock_only=True)
        assert all(m.provider is Provider.MOCK for m in catalog)

    def test_chat_models_exclude_embedding_models(self) -> None:
        catalog = build_catalog()
        assert all(not m.has(ModelCapability.EMBEDDING) for m in catalog.chat_models())

    def test_embedding_models_are_discoverable(self) -> None:
        assert build_catalog().embedding_models()

    def test_unknown_key_lists_alternatives(self) -> None:
        from orchestration.errors import NotFoundError

        with pytest.raises(NotFoundError) as info:
            build_catalog(mock_only=True).get("gpt-4o")
        assert "mock-fast" in info.value.context["available"]

    def test_every_catalogued_model_is_valid(self) -> None:
        """Guards against a typo making a model unusable only at call time."""
        for model in build_catalog():
            assert model.max_output_tokens < model.context_limit
            assert model.capabilities


class TestModelRouter:
    def test_mock_only_router_selects_a_mock_model(self) -> None:
        from orchestration.routing.model_router import build_default_router

        selection = build_default_router().select()
        assert selection.model.provider is Provider.MOCK
        assert selection.reason

    def test_pinned_model_is_honoured(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog())
        selection = router.select(RoutingCriteria(pinned_model="claude-opus-4-5"))
        assert selection.model.key == "claude-opus-4-5"
        assert "pinned" in selection.reason

    def test_pinning_an_unknown_model_fails(self) -> None:
        from orchestration.errors import NotFoundError
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog())
        with pytest.raises(NotFoundError):
            router.select(RoutingCriteria(pinned_model="not-a-model"))

    def test_forced_model_overrides_every_criterion(self) -> None:
        """An operator-forced model wins even over cost/capability preferences."""
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog(), force_model_key="claude-opus-4-5")
        selection = router.select(RoutingCriteria(prefer="cheapest"))
        assert selection.model.key == "claude-opus-4-5"
        assert "forced" in selection.reason

    def test_forced_model_overrides_an_agent_s_own_pin(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog(), force_model_key="claude-opus-4-5")
        selection = router.select(RoutingCriteria(pinned_model="gpt-4o-mini"))
        assert selection.model.key == "claude-opus-4-5"

    def test_forcing_an_unknown_model_fails_fast_at_construction(self) -> None:
        from orchestration.errors import NotFoundError
        from orchestration.routing.model_router import ModelRouter

        with pytest.raises(NotFoundError):
            ModelRouter(build_catalog(), force_model_key="not-a-model")

    def test_cheapest_preference(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog(), allowed_providers=["openai", "anthropic"])
        selection = router.select(RoutingCriteria(prefer="cheapest"))
        assert selection.model.key == "gpt-4o-mini"

    def test_most_capable_preference_prefers_reasoning(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog(), allowed_providers=["openai", "anthropic"])
        selection = router.select(RoutingCriteria(prefer="most_capable"))
        assert selection.model.has(ModelCapability.REASONING)

    def test_local_requirement_selects_a_local_model(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog())
        selection = router.select(RoutingCriteria(require_local=True))
        assert selection.model.has(ModelCapability.LOCAL)

    def test_unsatisfiable_criteria_explain_themselves(self) -> None:
        """An impossible routing request is a config bug and should say so."""
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog(mock_only=True))
        with pytest.raises(ConfigurationError) as info:
            router.select(RoutingCriteria(require_local=True))
        assert info.value.context["rejections"]

    def test_context_window_must_fit_input_plus_output(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog())
        selection = router.select(estimated_input_tokens=500_000)
        assert selection.model.context_limit >= 500_000

    def test_impossible_context_requirement_fails(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog())
        with pytest.raises(ConfigurationError):
            router.select(estimated_input_tokens=50_000_000)

    def test_selection_is_stable(self) -> None:
        """Phantom benchmark differences would otherwise appear between runs."""
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog())
        keys = {router.select(RoutingCriteria(prefer="balanced")).model.key for _ in range(20)}
        assert len(keys) == 1

    def test_complexity_upgrades_the_preference(self) -> None:
        from orchestration.domain.enums import TaskComplexity
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog(), allowed_providers=["openai", "anthropic"])
        simple = router.select(complexity=TaskComplexity.SIMPLE)
        complex_ = router.select(complexity=TaskComplexity.COMPLEX)
        assert complex_.model.has(ModelCapability.REASONING)
        assert simple.model.key != complex_.model.key

    def test_selection_records_what_was_considered(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        selection = ModelRouter(build_catalog()).select()
        assert len(selection.considered) > 1
        assert selection.as_event_payload()["model_key"] == selection.model.key

    def test_embedding_model_selection(self) -> None:
        from orchestration.routing.model_router import ModelRouter

        selection = ModelRouter(build_catalog()).select_embedding_model()
        assert selection.model.has(ModelCapability.EMBEDDING)


class TestSmartModelIsReachable:
    def test_mock_smart_is_selected_for_complex_work(self) -> None:
        from orchestration.domain.enums import TaskComplexity
        from orchestration.routing.model_router import ModelRouter

        router = ModelRouter(build_catalog(mock_only=True))
        selection = router.select(complexity=TaskComplexity.COMPLEX)
        assert selection.model.key == MOCK_SMART.key
