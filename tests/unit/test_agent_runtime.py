"""Tests for the shared agent runtime.

The behaviours that matter:

* Tool authorisation is delegated, and a denial is reported to the agent as a
  value it can adapt to rather than an exception that destroys its turn.
* The iteration ceiling is enforced, and reaching it still yields the agent's
  best answer instead of discarding the work.
* Malformed agent output degrades to low confidence rather than failing, because
  a formatting mistake is not a reason to throw away findings.
* Budget is consulted *before* each model and tool call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.agents.definitions import (
    ANALYST_AGENT,
    CRITIC_AGENT,
    DATA_AGENT,
    RESEARCH_AGENT,
)
from orchestration.agents.runtime import (
    AgentRunContext,
    AgentRuntime,
    _coerce_output_payload,
)
from orchestration.domain.base import JsonDict
from orchestration.domain.enums import InvocationStatus, PolicyEffect
from orchestration.domain.tool import ToolResult
from orchestration.errors import (
    BudgetExceededError,
    ConfigurationError,
    EngineTimeoutError,
)
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import Fault, MockProvider, MockRule, agent_output
from orchestration.routing.model_router import build_default_router
from orchestration.tools.registry import build_default_registry

pytestmark = pytest.mark.unit


def _tool_call_reply(tool: str, arguments: JsonDict) -> str:
    """An inline tool-call reply, the shape a local model tends to emit."""
    return json.dumps({"tool_calls": [{"name": tool, "arguments": arguments}]})


def _runtime(
    provider: MockProvider,
    *,
    authoriser: object = None,
    budget_check: object = None,
    tools: object = None,
    observer: object = None,
) -> AgentRuntime:
    return AgentRuntime(
        llm=LLMClient.mock(provider),
        tools=tools or build_default_registry(),  # type: ignore[arg-type]
        router=build_default_router(),
        authoriser=authoriser,  # type: ignore[arg-type]
        budget_check=budget_check,  # type: ignore[arg-type]
        tool_observer=observer,  # type: ignore[arg-type]
    )


@pytest.fixture
def context(tmp_path: Path) -> AgentRunContext:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "notes.txt").write_text("CRM notes", encoding="utf-8")
    return AgentRunContext(
        execution_id="exec_1",
        node_id="research",
        instruction="Research the top CRM vendors.",
        sandbox_root=tmp_path,
        deadline_seconds=10.0,
    )


class TestHappyPath:
    async def test_direct_structured_answer(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="analyst",
                    responses=(
                        agent_output(
                            "Salesforce leads on features.",
                            confidence=0.88,
                            claims=("Salesforce leads",),
                            evidence=("https://example.test/sf",),
                        ),
                    ),
                )
            ]
        )
        result = await _runtime(provider).run(ANALYST_AGENT, context)
        assert result.succeeded
        assert result.output is not None
        assert result.output.confidence == 0.88
        assert result.output.evidence == ("https://example.test/sf",)
        assert result.invocation.status is InvocationStatus.SUCCEEDED
        assert result.invocation.iterations == 1

    async def test_records_token_usage_and_cost(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [MockRule(name="r", responses=(agent_output("x"),), usage=(500, 200))]
        )
        result = await _runtime(provider).run(ANALYST_AGENT, context)
        assert result.invocation.input_tokens == 500
        assert result.invocation.output_tokens == 200
        assert result.invocation.total_tokens == 700

    async def test_model_is_selected_and_recorded(self, context: AgentRunContext) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        result = await _runtime(provider).run(ANALYST_AGENT, context)
        assert result.invocation.model_key is not None

    async def test_pinned_model_is_used(self, context: AgentRunContext) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        pinned = ANALYST_AGENT.merged(model_key="mock-smart")
        result = await _runtime(provider).run(pinned, context)
        assert result.invocation.model_key == "mock-smart"

    async def test_disabled_agent_is_refused(self, context: AgentRunContext) -> None:
        provider = MockProvider()
        with pytest.raises(ConfigurationError, match="is disabled"):
            await _runtime(provider).run(ANALYST_AGENT.merged(enabled=False), context)


class TestPromptConstruction:
    async def test_system_prompt_and_instruction_are_sent(self, context: AgentRunContext) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        await _runtime(provider).run(ANALYST_AGENT, context)
        call = provider.calls[0]
        assert "analyst" in call.system_preview.lower()
        assert "CRM vendors" in call.user_preview

    async def test_prior_outputs_are_injected_with_confidence(
        self, context: AgentRunContext
    ) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        context.prior_outputs = {
            "research": {
                "content": "found five vendors",
                "confidence": 0.7,
                "evidence": ["https://a"],
            }
        }
        await _runtime(provider).run(ANALYST_AGENT, context)
        # The recorded preview is truncated to 160 chars, so assert against the
        # renderer directly rather than against a possibly-cut prompt excerpt.
        rendered = AgentRuntime._render_prior_outputs(context.prior_outputs)
        assert "found five vendors" in rendered
        assert "confidence 0.7" in rendered
        assert "https://a" in rendered

    def test_verbose_upstream_output_cannot_crowd_out_others(self) -> None:
        """Truncation is per output, not in aggregate."""
        rendered = AgentRuntime._render_prior_outputs(
            {
                "loud": {"content": "x" * 50_000, "confidence": 0.5},
                "quiet": {"content": "important finding", "confidence": 0.9},
            },
            per_output=1_000,
        )
        assert "important finding" in rendered

    async def test_available_tools_are_named_in_the_prompt(self, context: AgentRunContext) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        await _runtime(provider).run(RESEARCH_AGENT, context)
        full = provider.calls[0].user_preview
        assert "web_search" in full or "Tools available" in full


class TestToolUse:
    async def test_executes_an_authorised_tool_then_answers(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    responses=(
                        _tool_call_reply("web_search", {"query": "CRM vendors"}),
                        agent_output("Found vendors", evidence=["https://example.test"]),
                    ),
                )
            ]
        )
        result = await _runtime(provider).run(RESEARCH_AGENT, context)
        assert result.succeeded
        assert result.invocation.tool_calls == 1
        assert result.tool_results[0].ok is True
        assert result.tool_results[0].tool == "web_search"

    async def test_tool_output_is_fed_back_to_the_model(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    responses=(
                        _tool_call_reply("web_search", {"query": "CRM"}),
                        agent_output("done"),
                    ),
                )
            ]
        )
        await _runtime(provider).run(RESEARCH_AGENT, context)
        assert "Tool results" in provider.calls[1].user_preview

    async def test_a_denied_tool_is_a_value_not_an_exception(
        self, context: AgentRunContext
    ) -> None:
        """The agent must be able to adapt rather than lose its whole turn."""

        async def deny(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
            return PolicyEffect.DENY, "not in your allowlist"

        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    responses=(
                        _tool_call_reply("web_search", {"query": "CRM"}),
                        agent_output("worked without search", confidence=0.4),
                    ),
                )
            ]
        )
        result = await _runtime(provider, authoriser=deny).run(RESEARCH_AGENT, context)
        assert result.succeeded
        assert result.tool_results[0].ok is False
        assert result.tool_results[0].error_code == "permission_denied"
        assert "not in your allowlist" in provider.calls[1].user_preview

    async def test_a_failing_tool_is_reported_as_a_value(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    responses=(
                        _tool_call_reply("read_file", {"path": "does_not_exist.txt"}),
                        agent_output("could not read the file", confidence=0.3),
                    ),
                )
            ]
        )
        result = await _runtime(provider).run(RESEARCH_AGENT, context)
        assert result.succeeded
        assert result.tool_results[0].ok is False

    async def test_parallel_tool_calls_all_execute(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    responses=(
                        json.dumps(
                            {
                                "tool_calls": [
                                    {"name": "web_search", "arguments": {"query": "crm"}},
                                    {"name": "web_search", "arguments": {"query": "pricing"}},
                                ]
                            }
                        ),
                        agent_output("done"),
                    ),
                )
            ]
        )
        result = await _runtime(provider).run(RESEARCH_AGENT, context)
        assert len(result.tool_results) == 2

    async def test_authorisation_precedes_all_execution_in_a_batch(
        self, context: AgentRunContext
    ) -> None:
        """One forbidden call must not let the rest slip through concurrently."""
        seen: list[str] = []

        async def record(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
            seen.append(tool)
            return (
                (PolicyEffect.DENY, "denied")
                if tool == "read_file"
                else (
                    PolicyEffect.ALLOW,
                    "ok",
                )
            )

        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    responses=(
                        json.dumps(
                            {
                                "tool_calls": [
                                    {"name": "web_search", "arguments": {"query": "crm"}},
                                    {"name": "read_file", "arguments": {"path": "x"}},
                                ]
                            }
                        ),
                        agent_output("done"),
                    ),
                )
            ]
        )
        result = await _runtime(provider, authoriser=record).run(RESEARCH_AGENT, context)
        assert seen == ["web_search", "read_file"]
        codes = {r.tool: r.ok for r in result.tool_results}
        assert codes == {"web_search": True, "read_file": False}

    async def test_approval_requirement_suspends_the_run(self, context: AgentRunContext) -> None:
        """The runtime cannot pause durably, so it hands the signal upward."""

        async def gate(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
            return PolicyEffect.REQUIRE_APPROVAL, "sends external email"

        provider = MockProvider(
            [
                MockRule(
                    name="finalizer",
                    responses=(
                        _tool_call_reply("write_file", {"path": "reports/r.md", "content": "x"}),
                    ),
                )
            ]
        )
        from orchestration.agents.definitions import FINALIZER_AGENT

        result = await _runtime(provider, authoriser=gate).run(FINALIZER_AGENT, context)
        assert result.pending_approval is not None
        assert result.output is None
        assert result.pending_approval.context["tool"] == "write_file"

    async def test_approval_arguments_are_redacted(self, context: AgentRunContext) -> None:
        """An audit record must not become a secret store."""

        async def gate(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
            return PolicyEffect.REQUIRE_APPROVAL, "risky"

        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        json.dumps(
                            {
                                "tool_calls": [
                                    {
                                        "name": "write_file",
                                        "arguments": {
                                            "path": "reports/r.md",
                                            "content": "x",
                                            "api_key": "sk-secret-value",
                                        },
                                    }
                                ]
                            }
                        ),
                    ),
                )
            ]
        )
        from orchestration.agents.definitions import FINALIZER_AGENT

        result = await _runtime(provider, authoriser=gate).run(FINALIZER_AGENT, context)
        assert result.pending_approval is not None
        arguments = result.pending_approval.context["arguments"]
        assert arguments["api_key"] == "***"
        assert "sk-secret-value" not in json.dumps(arguments)

    async def test_unlisted_tool_is_blocked_by_defence_in_depth(
        self, context: AgentRunContext
    ) -> None:
        """Even with a permissive authoriser, the allowlist still holds."""
        provider = MockProvider(
            [
                MockRule(
                    name="analyst",
                    responses=(
                        _tool_call_reply("web_search", {"query": "x"}),
                        agent_output("done"),
                    ),
                )
            ]
        )
        # ANALYST_AGENT may only use calculator; the default authoriser allows all.
        result = await _runtime(provider).run(ANALYST_AGENT, context)
        assert result.tool_results[0].ok is False
        assert result.tool_results[0].error_code == "permission_denied"

    async def test_tool_observer_is_notified(self, context: AgentRunContext) -> None:
        observed: list[tuple[str, str]] = []

        async def observe(agent_id: str, result: ToolResult) -> None:
            observed.append((agent_id, result.tool))

        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        _tool_call_reply("web_search", {"query": "x"}),
                        agent_output("done"),
                    ),
                )
            ]
        )
        await _runtime(provider, observer=observe).run(RESEARCH_AGENT, context)
        assert observed == [("research_agent", "web_search")]


class TestIterationCeiling:
    async def test_stops_at_max_iterations_and_still_answers(
        self, context: AgentRunContext
    ) -> None:
        """A looping agent must yield its best answer, not nothing."""
        provider = MockProvider(
            [
                MockRule(
                    name="loop",
                    # Always asks for another tool, never answers.
                    responses=(_tool_call_reply("web_search", {"query": "again"}),),
                ),
                MockRule(
                    name="final",
                    match_user="tool-use limit",
                    responses=(agent_output("best effort", confidence=0.4),),
                    priority=20,
                ),
            ]
        )
        agent = RESEARCH_AGENT.merged(max_iterations=3)
        result = await _runtime(provider).run(agent, context)
        assert result.succeeded
        assert result.output is not None
        assert result.output.content == "best effort"
        assert result.invocation.iterations == 3

    async def test_final_request_withholds_tools(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [
                MockRule(name="loop", responses=(_tool_call_reply("web_search", {"query": "x"}),)),
                MockRule(
                    name="final",
                    match_user="tool-use limit",
                    responses=(agent_output("done"),),
                    priority=20,
                ),
            ]
        )
        await _runtime(provider).run(RESEARCH_AGENT.merged(max_iterations=2), context)
        final_call = provider.calls_for_rule("final")[0]
        assert final_call.requested_schema == "AgentOutput"


class TestOutputDegradation:
    async def test_prose_reply_becomes_low_confidence_output(
        self, context: AgentRunContext
    ) -> None:
        """A formatting mistake must not discard usable content."""
        provider = MockProvider(
            [MockRule(name="r", responses=("Salesforce is the market leader, I think.",))]
        )
        result = await _runtime(provider).run(ANALYST_AGENT, context)
        assert result.succeeded
        assert result.output is not None
        assert result.output.confidence == 0.3
        assert "Salesforce" in result.output.content
        assert result.output.gaps

    async def test_json_with_wrong_fields_degrades_gracefully(
        self, context: AgentRunContext
    ) -> None:
        provider = MockProvider(
            [MockRule(name="r", responses=(json.dumps({"totally": "unexpected"}),))]
        )
        result = await _runtime(provider).run(ANALYST_AGENT, context)
        assert result.succeeded
        assert result.output is not None
        assert result.output.confidence <= 0.5


class TestOutputCoercion:
    """Near-miss shapes are normalised rather than rejected."""

    @pytest.mark.parametrize("alias", ["answer", "result", "summary", "text", "findings"])
    def test_content_aliases(self, alias: str) -> None:
        assert _coerce_output_payload({alias: "the body"})["content"] == "the body"

    def test_percentage_confidence_is_scaled(self) -> None:
        assert _coerce_output_payload({"content": "x", "confidence": 85})["confidence"] == 0.85

    def test_verbal_confidence_is_mapped(self) -> None:
        assert _coerce_output_payload({"content": "x", "confidence": "high"})["confidence"] == 0.9
        assert _coerce_output_payload({"content": "x", "confidence": "low"})["confidence"] == 0.3

    def test_scalar_lists_are_wrapped(self) -> None:
        coerced = _coerce_output_payload({"content": "x", "claims": "one claim"})
        assert coerced["claims"] == ["one claim"]

    def test_empty_string_list_becomes_empty(self) -> None:
        assert _coerce_output_payload({"content": "x", "evidence": ""})["evidence"] == []

    def test_non_dict_data_is_wrapped(self) -> None:
        assert _coerce_output_payload({"content": "x", "data": [1, 2]})["data"] == {"value": [1, 2]}

    def test_non_string_content_is_serialised(self) -> None:
        coerced = _coerce_output_payload({"content": {"a": 1}})
        assert coerced["content"] == '{"a": 1}'

    def test_unexpected_keys_are_dropped(self) -> None:
        """Extra keys are a model quirk, not a reason to abort the turn."""
        coerced = _coerce_output_payload({"content": "x", "hallucinated": True})
        assert "hallucinated" not in coerced

    def test_none_lists_are_removed_so_defaults_apply(self) -> None:
        assert "claims" not in _coerce_output_payload({"content": "x", "claims": None})


class TestBudgetEnforcement:
    async def test_budget_is_checked_before_the_first_model_call(
        self, context: AgentRunContext
    ) -> None:
        """Checked before, not after the tokens are already spent."""
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])

        async def deny(reason: str) -> None:
            raise BudgetExceededError("out of tokens", dimension="tokens", limit=100, used=101)

        result = await _runtime(provider, budget_check=deny).run(ANALYST_AGENT, context)
        assert result.invocation.status is InvocationStatus.FAILED
        assert provider.call_count == 0, "a model call happened despite an exhausted budget"

    async def test_budget_is_checked_before_each_tool_call(self, context: AgentRunContext) -> None:
        reasons: list[str] = []

        async def record(reason: str) -> None:
            reasons.append(reason)

        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(
                        _tool_call_reply("web_search", {"query": "x"}),
                        agent_output("done"),
                    ),
                )
            ]
        )
        await _runtime(provider, budget_check=record).run(RESEARCH_AGENT, context)
        assert any(r.startswith("tool:") for r in reasons)
        assert any(r.startswith("agent:") for r in reasons)

    async def test_budget_failure_records_the_error(self, context: AgentRunContext) -> None:
        async def deny(reason: str) -> None:
            raise BudgetExceededError("over", dimension="cost_usd", limit=1.0, used=1.1)

        provider = MockProvider()
        result = await _runtime(provider, budget_check=deny).run(ANALYST_AGENT, context)
        assert result.invocation.error is not None
        assert result.invocation.error["code"] == "budget_exceeded"


class TestFailurePropagation:
    async def test_provider_timeout_propagates_after_retries(
        self, context: AgentRunContext
    ) -> None:
        provider = MockProvider(
            [MockRule(name="r", fault=Fault("timeout", attempts=(1, 2, 3, 4, 5, 6)))]
        )
        runtime = AgentRuntime(
            llm=LLMClient.mock(provider, sleep=_no_sleep),
            tools=build_default_registry(),
            router=build_default_router(),
        )
        with pytest.raises(EngineTimeoutError):
            await runtime.run(ANALYST_AGENT, context)

    async def test_recovers_from_a_transient_failure(self, context: AgentRunContext) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="r",
                    responses=(agent_output("recovered"),),
                    fault=Fault("timeout", attempts=(1,)),
                )
            ]
        )
        runtime = AgentRuntime(
            llm=LLMClient.mock(provider, sleep=_no_sleep),
            tools=build_default_registry(),
            router=build_default_router(),
        )
        result = await runtime.run(ANALYST_AGENT, context)
        assert result.succeeded
        assert result.output is not None
        assert result.output.content == "recovered"


async def _no_sleep(delay: float) -> None:
    return None


class TestComplexityEstimation:
    def test_synthesis_agents_are_treated_as_complex(self) -> None:
        from orchestration.domain.enums import TaskComplexity

        context = AgentRunContext(execution_id="e", instruction="short")
        for agent in (ANALYST_AGENT, CRITIC_AGENT):
            assert AgentRuntime._estimate_complexity(agent, context) is TaskComplexity.COMPLEX

    def test_large_context_raises_complexity(self) -> None:
        from orchestration.domain.enums import TaskComplexity

        small = AgentRunContext(execution_id="e", instruction="tiny")
        assert AgentRuntime._estimate_complexity(DATA_AGENT, small) is TaskComplexity.SIMPLE
        large = AgentRunContext(
            execution_id="e",
            instruction="x" * 9_000,
        )
        assert AgentRuntime._estimate_complexity(DATA_AGENT, large) is TaskComplexity.COMPLEX


class TestInlineToolCallParsing:
    """Local models emit tool calls inside JSON rather than natively."""

    def test_parses_the_tool_calls_key(self) -> None:
        runtime = _runtime(MockProvider())
        calls = runtime._requested_tool_calls(
            json.dumps(
                {"tool_calls": [{"name": "calculator", "arguments": {"expression": "1+1"}}]}
            ),
            (),
        )
        assert calls[0].name == "calculator"

    def test_accepts_the_tool_and_args_aliases(self) -> None:
        runtime = _runtime(MockProvider())
        calls = runtime._requested_tool_calls(
            json.dumps({"tools": [{"tool": "calculator", "args": {"expression": "2"}}]}), ()
        )
        assert calls[0].name == "calculator"
        assert calls[0].arguments == {"expression": "2"}

    def test_a_plain_answer_is_not_a_tool_call(self) -> None:
        runtime = _runtime(MockProvider())
        assert runtime._requested_tool_calls(agent_output("done"), ()) == ()

    def test_native_calls_take_precedence(self) -> None:
        from orchestration.domain.model import ToolCallRequest

        runtime = _runtime(MockProvider())
        native = (ToolCallRequest(id="c1", name="web_search", arguments={"query": "x"}),)
        calls = runtime._requested_tool_calls(
            json.dumps({"tool_calls": [{"name": "calculator", "arguments": {}}]}), native
        )
        assert calls == native

    def test_unparsable_content_yields_no_calls(self) -> None:
        runtime = _runtime(MockProvider())
        assert runtime._requested_tool_calls("just prose", ()) == ()
