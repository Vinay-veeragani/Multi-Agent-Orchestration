"""Prometheus metrics.

One module-level registry, one set of module-level metric objects. Prometheus
client metrics are meant to be process-wide singletons -- creating a new
``Counter`` with the same name twice raises -- so unlike the tracer or logger
there is no per-call configuration step, only :func:`metrics_endpoint` to render
the current values.

Every metric named in the specification (§23) is here:

* execution count, success count, failure count, task completion rate
* agent invocation count, tool invocation count, retry count
* latency (average, p50, p95, p99 -- via a Histogram, which Prometheus derives
  quantiles from at query time rather than us computing them)
* estimated cost, token usage
* routing decisions

Task completion rate is not stored directly: it is ``executions_total{status=
"succeeded"} / executions_total`` at query time, which is how Prometheus
recording rules are meant to work rather than duplicating the arithmetic here.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

#: A dedicated registry rather than the global default. Building the engine's
#: metrics into their own registry means embedding this engine inside another
#: Prometheus-instrumented application cannot collide on metric names, and a
#: test suite that imports this module repeatedly does not hit the "duplicated
#: timeseries" error the default registry raises on re-registration.
REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------------------
# Executions
# ---------------------------------------------------------------------------

EXECUTIONS_TOTAL = Counter(
    "orch_executions_total",
    "Executions started, by terminal status once known (label starts as 'started').",
    labelnames=("status",),
    registry=REGISTRY,
)

EXECUTION_DURATION_SECONDS = Histogram(
    "orch_execution_duration_seconds",
    "Wall-clock duration of a completed execution.",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, float("inf")),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Nodes, agents, tools
# ---------------------------------------------------------------------------

NODE_EXECUTIONS_TOTAL = Counter(
    "orch_node_executions_total",
    "Node executions, by kind and outcome.",
    labelnames=("kind", "status"),
    registry=REGISTRY,
)

AGENT_INVOCATIONS_TOTAL = Counter(
    "orch_agent_invocations_total",
    "Agent invocations, by agent and outcome.",
    labelnames=("agent_id", "status"),
    registry=REGISTRY,
)

AGENT_LATENCY_SECONDS = Histogram(
    "orch_agent_latency_seconds",
    "Duration of one agent invocation.",
    labelnames=("agent_id",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, float("inf")),
    registry=REGISTRY,
)

TOOL_INVOCATIONS_TOTAL = Counter(
    "orch_tool_invocations_total",
    "Tool invocations, by tool and outcome.",
    labelnames=("tool", "status"),
    registry=REGISTRY,
)

TOOL_LATENCY_SECONDS = Histogram(
    "orch_tool_latency_seconds",
    "Duration of one tool invocation.",
    labelnames=("tool",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, float("inf")),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Retries and policy
# ---------------------------------------------------------------------------

RETRIES_TOTAL = Counter(
    "orch_retries_total",
    "Retry attempts, by node kind and the error code that triggered them.",
    labelnames=("kind", "error_code"),
    registry=REGISTRY,
)

POLICY_DECISIONS_TOTAL = Counter(
    "orch_policy_decisions_total",
    "Policy engine decisions, by effect.",
    labelnames=("effect",),
    registry=REGISTRY,
)

APPROVALS_TOTAL = Counter(
    "orch_approvals_total",
    "Approval requests, by final status.",
    labelnames=("status",),
    registry=REGISTRY,
)

BUDGET_EXCEEDED_TOTAL = Counter(
    "orch_budget_exceeded_total",
    "Executions stopped by a budget limit, by dimension.",
    labelnames=("dimension",),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# LLM usage and routing
# ---------------------------------------------------------------------------

LLM_CALLS_TOTAL = Counter(
    "orch_llm_calls_total",
    "LLM completion calls, by provider, model and outcome.",
    labelnames=("provider", "model", "status"),
    registry=REGISTRY,
)

LLM_LATENCY_SECONDS = Histogram(
    "orch_llm_latency_seconds",
    "Duration of one LLM call.",
    labelnames=("provider", "model"),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 60, float("inf")),
    registry=REGISTRY,
)

LLM_TOKENS_TOTAL = Counter(
    "orch_llm_tokens_total",
    "Tokens consumed, by provider, model and direction (input/output).",
    labelnames=("provider", "model", "direction"),
    registry=REGISTRY,
)

LLM_COST_USD_TOTAL = Counter(
    "orch_llm_cost_usd_total",
    "Estimated USD cost of LLM calls, by provider and model.",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)

ROUTING_DECISIONS_TOTAL = Counter(
    "orch_routing_decisions_total",
    "Supervisor routing decisions, by action and whether the model or the "
    "heuristic fallback produced it.",
    labelnames=("action", "source"),
    registry=REGISTRY,
)

SCHEMA_REPAIRS_TOTAL = Counter(
    "orch_schema_repairs_total",
    "Structured-output repair attempts, by whether the repair succeeded.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Concurrency (gauges, so 'currently in flight' is visible)
# ---------------------------------------------------------------------------

ACTIVE_EXECUTIONS = Gauge(
    "orch_active_executions",
    "Executions currently running.",
    registry=REGISTRY,
)

ACTIVE_AGENTS = Gauge(
    "orch_active_agents",
    "Agent invocations currently in flight.",
    registry=REGISTRY,
)

ACTIVE_NODES = Gauge(
    "orch_active_nodes",
    "Workflow nodes currently running, summed across all executions.",
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Convenience recorders
# ---------------------------------------------------------------------------


def record_execution_started() -> None:
    EXECUTIONS_TOTAL.labels(status="started").inc()
    ACTIVE_EXECUTIONS.inc()


def record_execution_finished(status: str, *, duration_seconds: float) -> None:
    """Record a terminal execution status and release its active-gauge slot."""
    EXECUTIONS_TOTAL.labels(status=status).inc()
    EXECUTION_DURATION_SECONDS.observe(max(0.0, duration_seconds))
    ACTIVE_EXECUTIONS.dec()


def record_node_execution(kind: str, status: str) -> None:
    NODE_EXECUTIONS_TOTAL.labels(kind=kind, status=status).inc()


def record_agent_invocation(agent_id: str, status: str, *, duration_seconds: float) -> None:
    AGENT_INVOCATIONS_TOTAL.labels(agent_id=agent_id, status=status).inc()
    AGENT_LATENCY_SECONDS.labels(agent_id=agent_id).observe(max(0.0, duration_seconds))


def record_tool_invocation(tool: str, status: str, *, duration_seconds: float) -> None:
    TOOL_INVOCATIONS_TOTAL.labels(tool=tool, status=status).inc()
    TOOL_LATENCY_SECONDS.labels(tool=tool).observe(max(0.0, duration_seconds))


def record_retry(kind: str, error_code: str) -> None:
    RETRIES_TOTAL.labels(kind=kind, error_code=error_code).inc()


def record_policy_decision(effect: str) -> None:
    POLICY_DECISIONS_TOTAL.labels(effect=effect).inc()


def record_approval(status: str) -> None:
    APPROVALS_TOTAL.labels(status=status).inc()


def record_budget_exceeded(dimension: str) -> None:
    BUDGET_EXCEEDED_TOTAL.labels(dimension=dimension).inc()


def record_llm_call(
    provider: str,
    model: str,
    status: str,
    *,
    duration_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    LLM_CALLS_TOTAL.labels(provider=provider, model=model, status=status).inc()
    LLM_LATENCY_SECONDS.labels(provider=provider, model=model).observe(max(0.0, duration_seconds))
    if input_tokens:
        LLM_TOKENS_TOTAL.labels(provider=provider, model=model, direction="input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS_TOTAL.labels(provider=provider, model=model, direction="output").inc(
            output_tokens
        )
    if cost_usd:
        LLM_COST_USD_TOTAL.labels(provider=provider, model=model).inc(cost_usd)


def record_routing_decision(action: str, *, degraded: bool) -> None:
    ROUTING_DECISIONS_TOTAL.labels(action=action, source="fallback" if degraded else "model").inc()


def record_schema_repair(*, succeeded: bool) -> None:
    SCHEMA_REPAIRS_TOTAL.labels(outcome="repaired" if succeeded else "failed").inc()


@contextmanager
def track_active_agents() -> Iterator[None]:
    ACTIVE_AGENTS.inc()
    try:
        yield
    finally:
        ACTIVE_AGENTS.dec()


@contextmanager
def track_active_nodes(count: int = 1) -> Iterator[None]:
    """Track a batch of concurrently running nodes (a parallel step)."""
    ACTIVE_NODES.inc(count)
    try:
        yield
    finally:
        ACTIVE_NODES.dec(count)


class Timer:
    """A small stopwatch, so callers do not each hand-roll ``time.perf_counter``.

    Used at call sites that need the duration for both a metric and a log field,
    which is common enough here to be worth one shared helper.
    """

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._start


def metrics_endpoint() -> tuple[bytes, str]:
    """Render the current metrics, for the ``GET /metrics`` route.

    Returns ``(body, content_type)`` so the API layer can hand both straight to
    the response without importing anything Prometheus-specific itself.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def reset_metrics() -> None:
    """Clear every metric's recorded values. For test isolation only.

    Prometheus client metrics are process-wide singletons with no public reset,
    so this reaches into each collector's internal value store. It is used
    exclusively by the test suite between assertions -- production code has no
    reason to call it.
    """
    for collector in list(REGISTRY._collector_to_names.keys()):
        if hasattr(collector, "_metrics"):
            collector._metrics.clear()
        value_holder = getattr(collector, "_value", None)
        if value_holder is not None:
            value_holder.set(0)
