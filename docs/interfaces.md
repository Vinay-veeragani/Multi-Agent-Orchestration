# Interfaces: HTTP API and CLI

## HTTP API

FastAPI app, `orchestration.api.app:create_app` (a factory, not a
module-level object -- `uvicorn ... --factory`). Every route except
`/health` and `/metrics` requires an `X-API-Key` header matching one of
`ORCH_API_KEYS` when `ORCH_API_REQUIRE_AUTH` is true.

Errors are translated from the engine's own taxonomy by one exception
handler (`orchestration.api.errors`): the HTTP status is a function of the
error's *class* (`NotFoundError` -> 404, `PermissionDeniedError`/
`PolicyViolationError` -> 403, `BudgetExceededError` -> 402,
`InputValidationError`/`GraphValidationError` -> 400, a conflict-type error
-> 409, ...), and the body carries the engine's structured error (`code`,
`message`, `retryable`, `context`) -- the same shape the `orchestrator` CLI
prints.

| Method & path | Purpose |
|---|---|
| `POST /agents` | Register (or update) an agent definition |
| `GET /agents` | List registered agents |
| `GET /agents/{agent_id}` | Fetch one agent |
| `POST /workflows` | Register a workflow (validated -- rejects one that could never execute) |
| `GET /workflows` | List registered workflows |
| `GET /workflows/{workflow_id}` | Fetch one workflow |
| `GET /executions` | Recent executions, newest first (`limit`, `status_filter`) -- a dashboard's listing source |
| `POST /executions` | Start an execution -- dynamic (omit `workflow_id`) or against a registered workflow |
| `GET /executions/{execution_id}` | Current state (live, if running in this process; the durable checkpoint otherwise) |
| `POST /executions/{execution_id}/cancel` | Cooperative cancellation of an execution in flight |
| `POST /executions/{execution_id}/resume` | Resume an execution stranded by a crashed/restarted process |
| `GET /executions/{execution_id}/approvals` | Pending approvals -- action, risk reason, parameters -- for a HITL UI to render before deciding |
| `POST /executions/{execution_id}/approve` | Decide a pending approval: granted |
| `POST /executions/{execution_id}/reject` | Decide a pending approval: refused |
| `GET /executions/{execution_id}/events` | The durable, ordered event log |
| `GET /executions/{execution_id}/agent-invocations` | Every agent attempt -- model, tokens, cost, status |
| `GET /executions/{execution_id}/tool-invocations` | Every tool call -- tool, status, policy effect (arguments/results omitted) |
| `GET /executions/{execution_id}/stream` | Live event log over Server-Sent Events (best-effort, via Redis -- see below) |
| `GET /executions/{execution_id}/trace` | An event-derived trace view (not a live OTel query -- see the endpoint's own docstring) |
| `GET /health` | Database + Redis reachability |
| `GET /metrics` | Prometheus exposition format |

`POST /executions` accepts `task`, optional `workflow_id`, `success_criteria`,
per-request budget overrides (`max_cost_usd`, `max_tokens`,
`max_duration_seconds`, `max_agent_steps` -- tightened against, never
widening, the deployment default), `max_turns` (dynamic runs only), and
`idempotency_key` (a retried request with the same key returns the existing
execution rather than starting a second one).

Execution runs as a background `asyncio` task in the API process
(`ExecutionRunner`) -- a single-process design; see the "What this project
is NOT" section of the root README for the documented limit this implies for
cross-process cancellation.

### Live event streaming

`GET /executions/{execution_id}/stream` follows an execution's events as
Server-Sent Events (`text/event-stream`), reading from the Redis stream
`RedisEventSink` already publishes every event to. It replays the backlog
first (from the stream's start, or from `?after_id=<redis-stream-id>` to
resume a dropped connection), then blocks on Redis `XREAD` for live events,
and closes the connection itself once a terminal execution event
(`execution_completed`/`_failed`/`_cancelled`) has been sent.

This is a **best-effort live view, not the audit trail** -- the stream is
capped at 10,000 entries and, per the event bus's own fault-tolerance
contract, silently drops an event if Redis was briefly unreachable when it
was emitted. `GET /executions/{execution_id}/events` against PostgreSQL is
still the complete, durable history. It also still requires the same
`X-API-Key` header as every other route on this router, which rules out a
plain browser `EventSource` (no custom headers) -- a browser client needs
`fetch` + a `ReadableStream` reader, or a small server-side proxy.

## CLI

Console script `orchestrator` (typer). Every command except `benchmark` is a
thin HTTP client over `--api-url` (default `http://127.0.0.1:8000`, or
`$ORCH_API_URL`) and `--api-key` (or `$ORCH_API_KEY`).

```bash
orchestrator agents list
orchestrator run "compare CRM vendors on pricing" [--workflow-id ID] [--max-turns N] [--wait]
orchestrator status EXECUTION_ID
orchestrator cancel EXECUTION_ID [--reason TEXT]
orchestrator resume EXECUTION_ID
orchestrator approve EXECUTION_ID --by "you@example.test" [--note TEXT] [--approval-id ID]
orchestrator reject EXECUTION_ID --by "you@example.test" [--note TEXT] [--approval-id ID]
orchestrator benchmark [--category NAME]... [--scenario ID]... [--test-db] [--concurrency N] [--output PATH]
```

`benchmark` is the one exception: it drives `orchestration.evaluation`
directly against the configured (or `--test-db`) database -- no running API
process needed. `benchmarks/run_benchmark.py` is a thin argument-parsing
wrapper around the exact same function, kept as a standalone entry point.

A CLI error prints the engine's own structured message (`[not_found] ...`)
rather than a raw HTTP status or a stack trace, and exits non-zero.

## See also

- [`evaluation-benchmark.md`](evaluation-benchmark.md) -- what `benchmark`
  actually measures.
- [`deployment.md`](deployment.md) -- running the API via Docker.
