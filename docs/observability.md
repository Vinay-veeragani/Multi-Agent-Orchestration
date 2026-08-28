# Observability

## Logging

`structlog`, configured once per process (`configure_logging`), with a
recursive redaction processor applied to every log line: key-fragment
matching (`token`, `key`, `secret`, `password`, `authorization`, ...) plus a
regex scan of string *values* for embedded credentials (a bearer token
pasted into a message, not just a field named suspiciously) -- both are
needed, since a secret can leak through either path.

`bind_execution_context()` attaches `execution_id`/`trace_id` to every log
line for the duration of a run, so grepping logs for one execution actually
works across every module that touches it.

## Tracing

OpenTelemetry, through a single entry point: `traced_span()`, wrapped by
named helpers per concern (`execution_span`, `supervisor_span`, `agent_span`,
`llm_call_span`, `tool_span`, `retrieval_span`, `checkpoint_span`,
`retry_span`). `safe_attributes()` filters every attribute dict through the
same key-fragment check the logging redactor uses, so a span cannot
accidentally carry an API key that a log line was already protected against.

Tracing degrades to a no-op, never to an error: with `ORCH_TRACING_ENABLED`
false or no exporter configured, spans are still created against an
effectively-discarding provider, so call sites never need an `if
tracing_enabled` branch. `ORCH_TRACE_EXPORTER` selects `none` / `console` /
`otlp`.

## Metrics

Prometheus, on a dedicated `CollectorRegistry` (not the global default --
importing this package into a process that already runs its own Prometheus
metrics must not collide with them). Covers execution outcomes, agent
invocations, LLM calls (by provider/model/status, with token and cost
histograms), tool calls, retries, budget-exceeded events, approval
decisions, and policy denials. `GET /metrics` serves the standard exposition
format.

## What ties it together

Every layer emits at the same three levels for the same event -- a retry, say,
produces a structured log line, a `retry_span`, and a `record_retry()`
metric increment, all keyed by the same `execution_id`. Cross-referencing a
slow or failed execution across logs, traces, and metrics uses the one id,
not three different correlation schemes.

## See also

- [`interfaces.md`](interfaces.md) -- `GET /executions/{id}/events` and
  `GET /executions/{id}/trace` expose the durable event log and a
  reconstructed trace view over HTTP.
