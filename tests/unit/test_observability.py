"""Tests for structured logging, tracing, and metrics.

The property that matters most here is the one stated as a hard rule in the
module docstrings: **a secret must never reach a log line or a span attribute.**
Every redaction test below tries to make that fail -- a key fragment match, a
credential embedded in a string value, a nested dict, a DSN passed as a plain
string rather than a keyed field -- because a backstop is only worth having if
it actually catches the cases nobody thought to redact explicitly.

Tracing is verified against a real in-memory span exporter rather than by
inspecting internals, so what is asserted is what a trace backend would
actually receive.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from orchestration.errors import EngineTimeoutError, InputValidationError
from orchestration.observability import tracing as tracing_module
from orchestration.observability.logging import (
    _is_sensitive_key,
    _redact_string,
    redact_processor,
)
from orchestration.observability.metrics import (
    REGISTRY,
    metrics_endpoint,
    record_agent_invocation,
    record_approval,
    record_budget_exceeded,
    record_execution_finished,
    record_execution_started,
    record_llm_call,
    record_node_execution,
    record_policy_decision,
    record_retry,
    record_routing_decision,
    record_schema_repair,
    record_tool_invocation,
    reset_metrics,
)
from orchestration.observability.tracing import (
    agent_span,
    checkpoint_span,
    current_span_id,
    current_trace_id,
    execution_span,
    llm_call_span,
    record_exception,
    retry_span,
    safe_attributes,
    supervisor_span,
    tool_span,
    traced_span,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Logging redaction
# ---------------------------------------------------------------------------


class TestSensitiveKeyDetection:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "api_key",
            "openai_api_key",
            "db_password",
            "Authorization",
            "AUTH_HEADER",
            "secret_token",
            "private_key",
            "session_id",
            "cookie",
            "connection_string",
            "dsn",
        ],
    )
    def test_recognised_fragments_are_sensitive(self, key: str) -> None:
        assert _is_sensitive_key(key) is True

    @pytest.mark.parametrize("key", ["confidence", "agent_id", "message", "status", "node_id"])
    def test_ordinary_keys_are_not_sensitive(self, key: str) -> None:
        assert _is_sensitive_key(key) is False


class TestRedactProcessor:
    def test_sensitive_top_level_key_is_masked(self) -> None:
        result = redact_processor(None, "info", {"api_key": "sk-abcdef1234567890"})
        assert result["api_key"] == "***"

    def test_nested_dict_is_redacted(self) -> None:
        """A stray password nested inside another field must still be caught."""
        result = redact_processor(None, "info", {"payload": {"password": "hunter2", "ok": True}})
        assert result["payload"]["password"] == "***"
        assert result["payload"]["ok"] is True

    def test_list_of_dicts_is_redacted(self) -> None:
        result = redact_processor(None, "info", {"items": [{"token": "abc"}, {"name": "safe"}]})
        assert result["items"][0]["token"] == "***"
        assert result["items"][1]["name"] == "safe"

    def test_ordinary_fields_pass_through_unchanged(self) -> None:
        result = redact_processor(None, "info", {"agent_id": "research_agent", "confidence": 0.8})
        assert result == {"agent_id": "research_agent", "confidence": 0.8}

    def test_a_dsn_embedded_in_a_plain_string_value_is_masked(self) -> None:
        """Key-based redaction cannot see inside a string; this is the backstop."""
        result = redact_processor(
            None,
            "info",
            {"message": "connecting to postgresql://orchestrator:orch_local_dev_only@127.0.0.1/db"},
        )
        assert "orch_local_dev_only" not in result["message"]
        assert "orchestrator" in result["message"]

    def test_a_bearer_token_embedded_in_free_text_is_masked(self) -> None:
        result = redact_processor(
            None,
            "info",
            {"message": "request failed with Authorization: Bearer sk-abcdefghij1234567890"},
        )
        assert "sk-abcdefghij1234567890" not in result["message"]

    def test_an_openai_style_key_embedded_in_text_is_masked(self) -> None:
        result = redact_processor(
            None, "error", {"message": "invalid key sk-proj-abcdefghijklmnop1234567890"}
        )
        assert "sk-proj-abcdefghijklmnop1234567890" not in result["message"]

    def test_tuple_values_are_redacted_and_stay_tuples(self) -> None:
        result = redact_processor(None, "info", {"items": ({"password": "x"},)})
        assert isinstance(result["items"], tuple)
        assert result["items"][0]["password"] == "***"

    def test_pathological_nesting_does_not_recurse_forever(self) -> None:
        nested: dict[str, Any] = {"password": "x"}
        current: dict[str, Any] = nested
        for _ in range(20):
            current["inner"] = {"password": "y"}
            current = current["inner"]
        # Must not raise (stack overflow / infinite loop); depth is capped.
        redact_processor(None, "info", {"data": nested})


class TestRedactString:
    def test_plain_text_is_untouched(self) -> None:
        assert _redact_string("hello world") == "hello world"

    def test_url_credentials_are_masked_but_the_host_is_kept(self) -> None:
        redacted = _redact_string("redis://user:hunter2@host:6379/1")
        assert "hunter2" not in redacted
        assert "host:6379" in redacted


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """A tracer bound to a private, in-memory-exported provider.

    The OpenTelemetry API only honours ``set_tracer_provider`` once per
    process -- if any other test module (or the executor, or the agent
    runtime) has already called ``get_tracer()`` first, installing a new
    global provider here would be silently ignored and every span in this
    file would vanish into whatever provider got there first. Patching
    ``get_tracer`` directly sidesteps that global singleton and its ordering
    hazard entirely, so this fixture behaves the same regardless of what ran
    before it.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("orchestration")
    monkeypatch.setattr(tracing_module, "get_tracer", lambda: tracer)
    yield exporter
    exporter.clear()


def _attrs(span: Any) -> dict[str, object]:
    """Unwrap a finished span's attributes, which OTel types as optional."""
    assert span.attributes is not None
    return dict(span.attributes)


class TestSafeAttributes:
    def test_sensitive_keys_are_masked(self) -> None:
        assert safe_attributes({"api_key": "sk-123"})["api_key"] == "***"

    def test_none_values_are_dropped(self) -> None:
        """OTel rejects None outright; dropping is correct, not lossy-by-mistake."""
        assert "node_id" not in safe_attributes({"node_id": None, "ok": 1})

    def test_scalars_pass_through(self) -> None:
        attrs = safe_attributes({"count": 3, "ratio": 0.5, "ok": True, "name": "x"})
        assert attrs == {"count": 3, "ratio": 0.5, "ok": True, "name": "x"}

    def test_homogeneous_sequences_pass_through_as_lists(self) -> None:
        assert safe_attributes({"tags": ("a", "b")})["tags"] == ["a", "b"]

    def test_unsupported_types_are_stringified(self) -> None:
        attrs = safe_attributes({"payload": {"nested": 1}})
        assert attrs["payload"] == "{'nested': 1}"


class TestTracedSpan:
    def test_span_is_recorded_with_attributes(self, span_exporter: InMemorySpanExporter) -> None:
        with traced_span("unit.test", foo="bar", count=3):
            pass
        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "unit.test"
        assert _attrs(spans[0])["foo"] == "bar"
        assert _attrs(spans[0])["count"] == 3
        assert spans[0].status.status_code == StatusCode.OK

    def test_secret_attributes_never_reach_the_exporter(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        with traced_span("unit.test", api_key="sk-should-not-leak-1234567890"):
            pass
        span = span_exporter.get_finished_spans()[0]
        assert _attrs(span)["api_key"] == "***"

    def test_exception_sets_error_status_and_is_reraised(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        with pytest.raises(InputValidationError), traced_span("unit.test"):
            raise InputValidationError("bad input", field="x")
        span = span_exporter.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.ERROR
        assert _attrs(span)["error.code"] == "validation_error"
        assert _attrs(span)["error.retryable"] is False

    def test_retryable_error_is_flagged_on_the_span(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        with pytest.raises(EngineTimeoutError), traced_span("unit.test"):
            raise EngineTimeoutError("slow")
        span = span_exporter.get_finished_spans()[0]
        assert _attrs(span)["error.retryable"] is True

    def test_error_context_does_not_leak_a_credential_via_the_message(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        with pytest.raises(EngineTimeoutError), traced_span("unit.test"):
            raise EngineTimeoutError("timed out", api_key="sk-leaked-value-1234567890")
        span = span_exporter.get_finished_spans()[0]
        # error.code/error.type/error.retryable are the only attrs record_exception
        # sets; the raw context (which could carry a key) is never attached.
        assert all("sk-leaked-value" not in str(v) for v in _attrs(span).values())


class TestNamedSpans:
    def test_execution_span_has_the_expected_shape(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        with execution_span("exec_1", "wkf_1", "compare CRM vendors"):
            pass
        span = span_exporter.get_finished_spans()[0]
        assert span.name == "execution"
        assert _attrs(span)["execution_id"] == "exec_1"
        assert _attrs(span)["workflow_id"] == "wkf_1"

    def test_agent_span_records_identity(self, span_exporter: InMemorySpanExporter) -> None:
        with agent_span("exec_1", "research_agent", node_id="n1", attempt=2):
            pass
        span = span_exporter.get_finished_spans()[0]
        assert _attrs(span)["agent_id"] == "research_agent"
        assert _attrs(span)["attempt"] == 2

    def test_spans_nest_under_a_shared_trace(self, span_exporter: InMemorySpanExporter) -> None:
        """A supervisor decision and the agent it delegates to share one trace."""
        with execution_span("exec_1", "wkf_1", "task"):
            root_trace = current_trace_id()
            with (
                supervisor_span("exec_1", step=0),
                agent_span("exec_1", "research_agent"),
                llm_call_span("exec_1", "mock-fast", "mock"),
            ):
                pass
        spans = span_exporter.get_finished_spans()
        assert len(spans) == 4
        trace_ids = {format(s.context.trace_id, "032x") for s in spans}
        assert trace_ids == {root_trace}

    def test_tool_span_records_risk(self, span_exporter: InMemorySpanExporter) -> None:
        with tool_span("exec_1", "send_email", agent_id="finalizer_agent", risk="high"):
            pass
        span = span_exporter.get_finished_spans()[0]
        assert _attrs(span)["risk"] == "high"

    def test_checkpoint_span_records_the_reason(self, span_exporter: InMemorySpanExporter) -> None:
        with checkpoint_span("exec_1", reason="before_node", sequence=3):
            pass
        span = span_exporter.get_finished_spans()[0]
        assert _attrs(span)["reason"] == "before_node"

    def test_retry_span_records_attempt_and_error(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        with retry_span("exec_1", "n1", attempt=2, error_code="timeout"):
            pass
        span = span_exporter.get_finished_spans()[0]
        assert _attrs(span)["attempt"] == 2
        assert _attrs(span)["error_code"] == "timeout"


class TestTraceIdHelpers:
    def test_no_trace_id_outside_a_span(self) -> None:
        assert current_trace_id() is None
        assert current_span_id() is None

    def test_trace_id_is_present_inside_a_span(self, span_exporter: InMemorySpanExporter) -> None:
        with execution_span("exec_1", "wkf_1", "task"):
            assert current_trace_id() is not None
            assert current_span_id() is not None
        assert current_trace_id() is None


class TestRecordException:
    def test_masks_a_secret_in_the_error_message(self) -> None:
        """record_exception must not simply forward exc's message verbatim.

        A minimal duck-typed double stands in for a real ``Span``: the point of
        this test is what ``record_exception`` *sends*, not OTel's own SDK
        behaviour, so a lightweight recorder is clearer than constructing a real
        span just to inspect it afterwards.
        """

        class Recorder:
            def __init__(self) -> None:
                self.status: Status | None = None
                self.attributes: dict[str, object] = {}

            def set_status(self, status: Status) -> None:
                self.status = status

            def set_attributes(self, attrs: dict[str, object]) -> None:
                self.attributes.update(attrs)

        recorder = Recorder()
        record_exception(
            recorder,  # type: ignore[arg-type]
            InputValidationError("bad field", api_key="sk-leak-1234567890"),
        )
        assert all("sk-leak" not in str(v) for v in recorder.attributes.values())
        assert recorder.status is not None
        assert recorder.status.status_code == StatusCode.ERROR


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    reset_metrics()
    yield
    reset_metrics()


def _body() -> str:
    return metrics_endpoint()[0].decode()


class TestMetricsEndpoint:
    def test_content_type_is_prometheus_text_format(self) -> None:
        _, content_type = metrics_endpoint()
        assert "text/plain" in content_type

    def test_execution_lifecycle_is_recorded(self) -> None:
        record_execution_started()
        record_execution_finished("succeeded", duration_seconds=2.5)
        body = _body()
        assert 'orch_executions_total{status="started"} 1.0' in body
        assert 'orch_executions_total{status="succeeded"} 1.0' in body
        assert "orch_execution_duration_seconds_sum" in body

    def test_active_executions_gauge_returns_to_zero(self) -> None:
        record_execution_started()
        record_execution_started()
        assert "orch_active_executions 2.0" in _body()
        record_execution_finished("succeeded", duration_seconds=1.0)
        record_execution_finished("failed", duration_seconds=1.0)
        assert "orch_active_executions 0.0" in _body()

    def test_node_execution_is_labelled_by_kind_and_status(self) -> None:
        record_node_execution("agent", "succeeded")
        record_node_execution("join", "succeeded")
        record_node_execution("agent", "failed")
        body = _body()
        assert 'orch_node_executions_total{kind="agent",status="succeeded"} 1.0' in body
        assert 'orch_node_executions_total{kind="agent",status="failed"} 1.0' in body
        assert 'orch_node_executions_total{kind="join",status="succeeded"} 1.0' in body

    def test_agent_invocation_latency_is_observed(self) -> None:
        record_agent_invocation("research_agent", "succeeded", duration_seconds=1.5)
        body = _body()
        assert (
            'orch_agent_invocations_total{agent_id="research_agent",status="succeeded"} 1.0' in body
        )
        assert 'orch_agent_latency_seconds_count{agent_id="research_agent"} 1.0' in body

    def test_tool_invocation_is_recorded(self) -> None:
        record_tool_invocation("web_search", "succeeded", duration_seconds=0.2)
        body = _body()
        assert 'orch_tool_invocations_total{status="succeeded",tool="web_search"} 1.0' in body

    def test_retries_are_labelled_by_kind_and_error_code(self) -> None:
        record_retry("agent", "timeout")
        record_retry("agent", "timeout")
        record_retry("tool", "rate_limit")
        body = _body()
        assert 'orch_retries_total{error_code="timeout",kind="agent"} 2.0' in body
        assert 'orch_retries_total{error_code="rate_limit",kind="tool"} 1.0' in body

    def test_llm_usage_and_cost_are_accumulated(self) -> None:
        record_llm_call(
            "mock",
            "mock-fast",
            "succeeded",
            duration_seconds=0.01,
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.002,
        )
        record_llm_call(
            "mock",
            "mock-fast",
            "succeeded",
            duration_seconds=0.02,
            input_tokens=200,
            output_tokens=25,
            cost_usd=0.001,
        )
        body = _body()
        assert (
            'orch_llm_tokens_total{direction="input",model="mock-fast",provider="mock"} 300.0'
            in body
        )
        assert (
            'orch_llm_tokens_total{direction="output",model="mock-fast",provider="mock"} 75.0'
            in body
        )
        assert 'orch_llm_cost_usd_total{model="mock-fast",provider="mock"} 0.003' in body

    def test_llm_call_with_zero_usage_records_no_token_series(self) -> None:
        """A failed call with no usage must not fabricate a zero-token series.

        Prometheus always emits HELP/TYPE header comments for every registered
        metric name regardless of whether it has samples, so the assertion must
        look for an actual data line (one starting with the metric name) rather
        than the name appearing anywhere in the exposition text.
        """
        record_llm_call("mock", "mock-fast", "failed", duration_seconds=0.01)
        data_lines = [
            line for line in _body().splitlines() if line.startswith("orch_llm_tokens_total{")
        ]
        assert data_lines == []

    def test_routing_decisions_distinguish_model_from_fallback(self) -> None:
        record_routing_decision("delegate", degraded=False)
        record_routing_decision("delegate", degraded=True)
        body = _body()
        assert 'orch_routing_decisions_total{action="delegate",source="model"} 1.0' in body
        assert 'orch_routing_decisions_total{action="delegate",source="fallback"} 1.0' in body

    def test_schema_repair_outcomes(self) -> None:
        record_schema_repair(succeeded=True)
        record_schema_repair(succeeded=False)
        body = _body()
        assert 'orch_schema_repairs_total{outcome="repaired"} 1.0' in body
        assert 'orch_schema_repairs_total{outcome="failed"} 1.0' in body

    def test_policy_decisions_by_effect(self) -> None:
        record_policy_decision("allow")
        record_policy_decision("deny")
        record_policy_decision("deny")
        body = _body()
        assert 'orch_policy_decisions_total{effect="allow"} 1.0' in body
        assert 'orch_policy_decisions_total{effect="deny"} 2.0' in body

    def test_approvals_by_final_status(self) -> None:
        record_approval("approved")
        record_approval("rejected")
        body = _body()
        assert 'orch_approvals_total{status="approved"} 1.0' in body
        assert 'orch_approvals_total{status="rejected"} 1.0' in body

    def test_budget_exceeded_by_dimension(self) -> None:
        record_budget_exceeded("cost_usd")
        body = _body()
        assert 'orch_budget_exceeded_total{dimension="cost_usd"} 1.0' in body

    def test_reset_clears_counters(self) -> None:
        record_execution_started()
        assert "orch_executions_total" in _body()
        reset_metrics()
        assert 'orch_executions_total{status="started"}' not in _body()


class TestMetricsRegistryIsolated:
    def test_metrics_use_a_dedicated_registry_not_the_default(self) -> None:
        """Prevents collision if this engine is embedded in another app."""
        from prometheus_client import REGISTRY as DEFAULT_REGISTRY

        assert REGISTRY is not DEFAULT_REGISTRY
