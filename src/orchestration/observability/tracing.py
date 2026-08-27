"""OpenTelemetry tracing.

Every execution carries a trace id, and every meaningful unit of work inside it
-- a supervisor decision, an agent invocation, an LLM call, a tool call, a
retrieval, a checkpoint, a retry -- gets its own span. The span names and
attribute keys are centralised here so a trace looks the same whether it came
from the executor, the supervisor, or the agent runtime.

Two things are enforced structurally rather than by convention:

**No secrets in span attributes.** :func:`safe_attributes` filters every
attribute dict through the same key-fragment check the logging module uses, so
a call site cannot accidentally attach an API key to a span.

**Tracing degrades to a no-op, never to an error.** When ``ORCH_TRACING_ENABLED``
is false or no exporter is configured, spans are still created (against an
OTel ``NoOpTracer`` equivalent via an unconfigured provider) so call sites never
need an ``if tracing_enabled`` branch.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from orchestration.config import Settings, get_settings
from orchestration.observability.logging import _is_sensitive_key

#: Instrumentation scope name, so spans from this engine are identifiable in a
#: trace backend alongside spans from other instrumented libraries (httpx, etc).
_TRACER_NAME = "orchestration"

_configured = False


def configure_tracing(settings: Settings | None = None) -> trace.Tracer:
    """Configure the global tracer provider once per process.

    Idempotent, and safe to call even when tracing is disabled: it installs a
    provider with no exporters, so every span is created and immediately
    discarded rather than requiring call sites to branch on configuration.
    """
    global _configured
    config = settings or get_settings()

    if not _configured:
        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: config.service_name}))

        if config.tracing_enabled and config.trace_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=config.otlp_endpoint))
            )
        elif config.tracing_enabled and config.trace_exporter == "console":
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        # trace_exporter == "none": the provider has no processors, so every
        # span is created (call sites stay simple) and dropped (no overhead
        # worth mentioning) without a single conditional anywhere else.

        trace.set_tracer_provider(provider)
        _configured = True

    return trace.get_tracer(_TRACER_NAME)


def reset_tracing_config() -> None:
    """Undo :func:`configure_tracing`. For tests."""
    global _configured
    _configured = False


def get_tracer() -> trace.Tracer:
    """Return the engine's tracer, configuring tracing on first use if needed."""
    if not _configured:
        configure_tracing()
    return trace.get_tracer(_TRACER_NAME)


def safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Filter span attributes so a secret cannot be attached to a trace.

    Uses the same key-fragment heuristic as the logging redactor. Values that
    are not OTel-attribute-safe types (str, bool, int, float, or a homogeneous
    sequence of one of those) are stringified, since the SDK rejects arbitrary
    objects outright.
    """
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if _is_sensitive_key(key):
            safe[key] = "***"
            continue
        if value is None:
            continue
        if isinstance(value, str | bool | int | float):
            safe[key] = value
        elif isinstance(value, list | tuple) and all(
            isinstance(v, str | bool | int | float) for v in value
        ):
            safe[key] = list(value)
        else:
            safe[key] = str(value)
    return safe


def current_trace_id() -> str | None:
    """The active span's trace id as a hex string, or ``None`` outside a span."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.trace_id == 0:
        return None
    return format(context.trace_id, "032x")


def current_span_id() -> str | None:
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.span_id == 0:
        return None
    return format(context.span_id, "016x")


def record_exception(span: Span, exc: BaseException) -> None:
    """Record an exception on a span without leaking its message unfiltered.

    ``record_exception`` on the OTel API includes the exception message and
    traceback verbatim, which is exactly the surface :func:`safe_attributes`
    exists to avoid; the taxonomy's own message strings are trusted (engine
    error messages are constructed by us and never echo raw credentials), but
    any structured context on the error is passed through the same filter.
    """
    from orchestration.errors import to_error_dict

    data = to_error_dict(exc)
    span.set_status(Status(StatusCode.ERROR, str(data.get("message", ""))[:500]))
    span.set_attributes(
        safe_attributes(
            {
                "error.type": data.get("type", type(exc).__name__),
                "error.code": data.get("code", ""),
                "error.retryable": bool(data.get("retryable", False)),
            }
        )
    )


@contextlib.contextmanager
def traced_span(
    name: str, *, kind: SpanKind = SpanKind.INTERNAL, **attributes: Any
) -> Iterator[Span]:
    """Open a span, tag it, and record any exception raised inside the block.

    The single entry point every helper below uses, so span lifecycle and error
    recording behave identically everywhere.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name, kind=kind) as span:
        if attributes:
            span.set_attributes(safe_attributes(attributes))
        try:
            yield span
        except BaseException as exc:
            record_exception(span, exc)
            raise
        else:
            span.set_status(Status(StatusCode.OK))


# ---------------------------------------------------------------------------
# Named spans for each traced unit of work
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def execution_span(execution_id: str, workflow_id: str, task_description: str) -> Iterator[Span]:
    """The root span for one execution."""
    with traced_span(
        "execution",
        kind=SpanKind.SERVER,
        execution_id=execution_id,
        workflow_id=workflow_id,
        task_preview=task_description[:200],
    ) as span:
        yield span


@contextlib.contextmanager
def supervisor_span(execution_id: str, *, step: int) -> Iterator[Span]:
    """One supervisor routing decision."""
    with traced_span("supervisor.decide", execution_id=execution_id, step=step) as span:
        yield span


@contextlib.contextmanager
def agent_span(
    execution_id: str, agent_id: str, *, node_id: str | None = None, attempt: int = 1
) -> Iterator[Span]:
    """One agent invocation attempt."""
    with traced_span(
        "agent.invoke",
        execution_id=execution_id,
        agent_id=agent_id,
        node_id=node_id,
        attempt=attempt,
    ) as span:
        yield span


@contextlib.contextmanager
def llm_call_span(
    execution_id: str, model_key: str, provider: str, *, purpose: str = "completion"
) -> Iterator[Span]:
    """One request to an LLM provider.

    ``purpose`` distinguishes a routing call from an agent turn from a schema
    repair attempt in a trace view, without needing three separate span names.
    """
    with traced_span(
        "llm.call",
        kind=SpanKind.CLIENT,
        execution_id=execution_id,
        model_key=model_key,
        provider=provider,
        purpose=purpose,
    ) as span:
        yield span


def annotate_llm_usage(
    span: Span, *, input_tokens: int, output_tokens: int, cost_usd: float, latency_seconds: float
) -> None:
    """Attach usage figures to an LLM span after the call completes."""
    span.set_attributes(
        safe_attributes(
            {
                "llm.input_tokens": input_tokens,
                "llm.output_tokens": output_tokens,
                "llm.cost_usd": cost_usd,
                "llm.latency_seconds": round(latency_seconds, 6),
            }
        )
    )


@contextlib.contextmanager
def tool_span(
    execution_id: str, tool: str, *, agent_id: str | None = None, risk: str = "safe"
) -> Iterator[Span]:
    """One tool invocation attempt."""
    with traced_span(
        "tool.invoke",
        kind=SpanKind.CLIENT,
        execution_id=execution_id,
        tool=tool,
        agent_id=agent_id,
        risk=risk,
    ) as span:
        yield span


@contextlib.contextmanager
def retrieval_span(execution_id: str, *, query_preview: str, top_k: int) -> Iterator[Span]:
    """One pgvector evidence retrieval."""
    with traced_span(
        "retrieval.search",
        execution_id=execution_id,
        query_preview=query_preview[:200],
        top_k=top_k,
    ) as span:
        yield span


@contextlib.contextmanager
def checkpoint_span(
    execution_id: str, *, reason: str, sequence: int | None = None
) -> Iterator[Span]:
    """One checkpoint write."""
    with traced_span(
        "checkpoint.write", execution_id=execution_id, reason=reason, sequence=sequence
    ) as span:
        yield span


@contextlib.contextmanager
def retry_span(execution_id: str, node_id: str, *, attempt: int, error_code: str) -> Iterator[Span]:
    """One retry attempt, opened around the backoff sleep and the re-attempt."""
    with traced_span(
        "retry.attempt",
        execution_id=execution_id,
        node_id=node_id,
        attempt=attempt,
        error_code=error_code,
    ) as span:
        yield span
