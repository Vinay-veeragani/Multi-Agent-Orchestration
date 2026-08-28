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
| `POST /executions` | Start an execution -- dynamic (omit `workflow_id`) or against a registered workflow |
| `GET /executions/{execution_id}` | Current state (live, if running in this process; the durable checkpoint otherwise) |
| `POST /executions/{execution_id}/cancel` | Cooperative cancellation of an execution in flight |
| `POST /executions/{execution_id}/resume` | Resume an execution stranded by a crashed/restarted process |
| `POST /executions/{execution_id}/approve` | Decide a pending approval: granted |
| `POST /executions/{execution_id}/reject` | Decide a pending approval: refused |
| `GET /executions/{execution_id}/events` | The durable, ordered event log |
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
