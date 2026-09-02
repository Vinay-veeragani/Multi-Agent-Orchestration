# agent-orchestration-engine

A production-grade multi-agent supervisor and workflow orchestration engine:
an LLM supervisor routes work across a registry of specialist agents, either
along a hand-authored workflow DAG or by deciding the plan turn by turn, with
retries, parallel fan-out, budget enforcement, durable checkpoint/resume, and
human-in-the-loop approval built in rather than bolted on.

PostgreSQL (with pgvector) is the durable source of truth and Redis handles
coordination (locks, semaphores, event streams) -- there is no SQLite
fallback, by design. No LLM API key is required to run or evaluate the
engine: a deterministic mock provider drives every test, demo, and benchmark
scenario in this repository.

## Contents

- [Quickstart](#quickstart)
- [Web UI](#web-ui)
- [What this is](#what-this-is)
- [Why this architecture](#why-this-architecture)
- [What this project is NOT](#what-this-project-is-not)
- [Evaluation results](#evaluation-results)
- [Documentation](#documentation)
- [Project layout](#project-layout)

## Quickstart

### Bare metal

Requires PostgreSQL 16+ with the `vector` extension available, and Redis.

```bash
pip install -e ".[dev]"
cp .env.example .env               # edit ORCH_PG_DSN / ORCH_REDIS_URL if needed
alembic upgrade head
uvicorn orchestration.api.app:create_app --factory --reload
curl http://127.0.0.1:8000/health
```

### Docker

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
```

This starts PostgreSQL+pgvector, Redis, runs migrations once, then the API.
See [`docs/deployment.md`](docs/deployment.md) for configuration.

### Try it

```bash
orchestrator agents list
orchestrator run "compare CRM vendors on pricing" --wait
orchestrator benchmark --category simple --test-db
```

Three narrated, runnable demos live under [`examples/`](examples/):
competitive intelligence (parallel research fan-out), data analysis (a real
tool call mid-run), and human approval (a durable pause across a simulated
process restart). `python scripts/seed_demo_data.py` runs all three plus a
benchmark slice in one go, so a freshly migrated database has something to
look at in [the web UI](#web-ui) instead of empty lists.

## Web UI

A Next.js app under [`frontend/`](frontend/) -- a dashboard of recent
executions, an execution detail page (nodes, budget usage, live SSE event
stream while running, a play/step/scrub replay once it's finished, and the
recorded agent/tool invocations), a human-in-the-loop approve/reject panel,
and an evaluation/ablation dashboard. It talks to the same API above; the
orchestrator API key stays server-side (Server Components and a same-origin
SSE proxy) and never reaches the browser bundle.

```bash
cd frontend
cp .env.local.example .env.local   # point at the API above; set the same key
npm install
npm run dev
```

Requires the API from the Quickstart above to already be running. See
[`frontend/`](frontend/) for details; there is no separate write path here --
every mutation goes through the same HTTP API the CLI uses.

When no real LLM provider is configured (the default -- see "What this is
NOT" above), the UI shows a **demo mode** banner rather than staying silent
about it; `GET /health`'s `demo_mode` field is what drives it.

## What this is

- **A supervisor** that decides, per turn, whether to delegate to one agent,
  fan out to several in parallel, retry a failed node, replan the graph,
  request human approval, or finalise -- validated through three layers
  (schema, semantic, deterministic heuristic fallback) so a malformed or
  hallucinated decision never reaches the executor.
- **A workflow engine** that runs both a hand-authored static DAG and a
  graph the supervisor grows one decision at a time, sharing the same
  scheduler, retry policy, join semantics, and budget enforcement either way.
- **Durable by construction**: every execution checkpoints to PostgreSQL
  (content-hash deduplicated, optimistic-concurrency versioned), so a killed
  process resumes from exactly where it left off -- not from scratch.
- **Human-in-the-loop that survives a restart**: an approval's identity is
  derived from *what* is being approved (`sha256(execution, node, action,
  arguments)`), so a resumed node finds its prior decision instead of pausing
  forever, and an approval never silently authorises a different action.
- **Deny-by-default tool permissions**, budget ceilings enforced across cost,
  tokens, duration, agent steps, tool calls, and retries, and full
  observability (structured logs with secret redaction, OpenTelemetry traces,
  Prometheus metrics).
- **Provider-agnostic, including local/offline models.** OpenAI, Anthropic,
  and Gemini adapters are real (not stubs), and so is Ollama support
  (`ORCH_OLLAMA_ENABLED=true`) via the same OpenAI-compatible adapter, for
  running entirely without a cloud API key -- see
  [`docs/getting-started.md`](docs/getting-started.md) for setup and the
  caveat below on what's actually been verified against a real model.
- **An HTTP API, a CLI, a benchmark, and a web UI** over all of the above --
  see [Documentation](#documentation) and [Web UI](#web-ui).

## Why this architecture

**Structured output only, never free-text parsing.** Every supervisor
decision is a schema-validated `RoutingDecision`; a model that can't produce
one degrades to a deterministic heuristic router rather than having its prose
guessed at. This is what makes routing testable at all: a benchmark can
assert on the *decision*, not on whether a regex happened to match.

**The dynamic orchestrator reuses the static executor, not a parallel
implementation.** A supervisor-driven run compiles each decision into
`WorkflowNode`s and hands the growing graph to the same `WorkflowExecutor`
that runs hand-authored workflows. Retries, parallel dispatch, and budget
checks are one implementation, exercised by both paths -- see
[`docs/dynamic-orchestration.md`](docs/dynamic-orchestration.md) for the one
real design problem this reuse produced (and how it's solved).

**Approval identity, not a boolean flag.** A naive "is this approved" check
would let one approval authorise a differently-argued call, or re-pause an
already-decided node forever on resume. Keying the approval on a hash of the
exact action and arguments closes both holes at once.

**Everything is checked against real infrastructure.** No test in this
repository mocks PostgreSQL or Redis. That is also how most of the bugs
documented across this project's phase history were actually found -- a
harness that never touches a real database cannot catch a checkpoint dedup
bug, a foreign-key seeding gap, or an event-payload nesting bug, and several
of exactly those were found and fixed this way.

## What this project is NOT

- **Not a demonstration of a real LLM's routing quality.** Every scenario,
  test, demo, and benchmark run in this repository uses a deterministic mock
  provider. The engine mechanics (routing validation, scheduling, retries,
  budgets, checkpointing, approvals) are real and fully exercised; model
  *judgement* is not, because no API key has been configured for this
  project. Benchmark latency figures are engine wall-clock, explicitly
  labelled as such, never real provider latency. The same is true of Ollama:
  the adapter is verified with a real HTTP request/response cycle against a
  fake server shaped exactly like Ollama's documented API (URL, no-auth
  headers, payload, response parsing all real), but not yet run against an
  actually-installed Ollama on real hardware.
- **Not a sandboxed code execution platform.** `python_exec` and (disabled by
  default) `exec_shell` run with the same filesystem and network access as
  the engine process. The isolation is process-level (subprocess, timeout,
  output cap), not a security boundary -- permission scoping per agent is the
  real control.
- **Not idempotent across a crash-resume of a tool call, yet.** The
  mechanism for this is real and tested in isolation -- `NodeState.
  committed_keys`, `InvocationRepository.claim_tool`/`find_completed_tool`
  (see [`docs/checkpointing-and-resume.md`](docs/checkpointing-and-resume.md))
  -- but nothing in the live execution path calls it. If an execution
  checkpoints, crashes, and resumes between a tool call completing and its
  result being recorded, the resumed attempt calls the tool again. For a
  non-idempotent tool (`send_email`, `write_file`) that is a real duplicate
  side effect, not a hypothetical one.
- **Not multi-region or multi-tenant.** Concurrency limits, coordination
  locks, and the reference deployment are single-cluster. Cross-process
  cancellation propagation (an execution cancelled by one worker while
  running on another) is a documented known limitation, not a silent gap.
- **Not a finished product.** It is a reference implementation of the
  architecture: production-shaped, but without the operational surface
  (multi-region failover, a workflow builder, tool-call arguments/results
  exposed for inspection) a commercial product would need on top. The web
  UI is a real, working dashboard/replay/evaluation view -- not a mockup --
  but it is deliberately read-mostly (see [Web UI](#web-ui)).

## Evaluation results

54 deterministic scenarios (parallel fan-out, retry recovery, tool
permission denial, human approval, budget exhaustion, and more) run under
four arms -- see [`docs/evaluation-benchmark.md`](docs/evaluation-benchmark.md)
for what each arm actually isolates and how to reproduce this:

| arm                  | passed | completion | routing accuracy | avg latency* | tokens |
|----------------------|-------:|-----------:|------------------:|-------------:|-------:|
| baseline              | 11/54 |      20.4% |             56.7% |        3.8ms | 14,669 |
| supervisor            | 48/54 |      88.9% |            100.0% |      345.2ms | 23,784 |
| supervisor + retry    | 54/54 |     100.0% |            100.0% |      374.4ms | 25,995 |
| supervisor + parallel | 54/54 |     100.0% |            100.0% |      355.8ms | 25,995 |

<sub>*Latency is mock-provider engine wall-clock (routing, scheduling,
checkpoint plumbing) -- not real LLM latency; see the note above. Reproduce
with `orchestrator benchmark --test-db`, report id
`eval_c760d1ff9d464cbfa89e`, commit `aed23dd`.</sub>

`baseline` (a single heuristically-chosen agent, no LLM supervisor, no
retry, no parallelism) is a genuine ablation floor, not a strawman: it uses
the engine's own deterministic keyword/capability router, the same one the
LLM-driven path falls back to. The gap between `supervisor` and the two
retry-enabled arms is exactly the 6 retry-recovery scenarios, which are
*designed* to fail without retry -- that gap is the point, not noise.

## Documentation

| Page | Covers |
|---|---|
| [`docs/getting-started.md`](docs/getting-started.md) | Environment setup, both bare-metal and Docker |
| [`docs/architecture.md`](docs/architecture.md) | System components and how they fit together |
| [`docs/supervisor-and-routing.md`](docs/supervisor-and-routing.md) | The three-layer routing validation and heuristic fallback |
| [`docs/dynamic-orchestration.md`](docs/dynamic-orchestration.md) | Workflow-less, supervisor-driven execution |
| [`docs/workflow-engine.md`](docs/workflow-engine.md) | The DAG scheduler: joins, conditions, retries, parallelism |
| [`docs/checkpointing-and-resume.md`](docs/checkpointing-and-resume.md) | Durability: what's checkpointed, and how resume works |
| [`docs/human-in-the-loop.md`](docs/human-in-the-loop.md) | Approval identity, durable pause, and decision scoping |
| [`docs/budget-and-policies.md`](docs/budget-and-policies.md) | Budget enforcement and deny-by-default tool permissions |
| [`docs/observability.md`](docs/observability.md) | Logging, tracing, and metrics |
| [`docs/interfaces.md`](docs/interfaces.md) | The HTTP API and the `orchestrator` CLI |
| [`docs/mcp-tools.md`](docs/mcp-tools.md) | Connecting an MCP server: discovery, the two gates that keep it deny-by-default, what's out of scope |
| [`docs/evaluation-benchmark.md`](docs/evaluation-benchmark.md) | Benchmark methodology, scenarios, and how to reproduce the numbers above |
| [`docs/deployment.md`](docs/deployment.md) | Docker/compose and production configuration notes |
| [`frontend/README.md`](frontend/README.md) | The web UI: pages, how it talks to the API, running it |

## Project layout

```
src/orchestration/
  domain/        Pydantic v2 models: execution state, workflow, routing, budget, checkpoint, approval
  agents/        Agent registry, reference agents, the reason/act runtime
  supervisor/    LLM-driven routing (3-layer validated) + deterministic heuristic fallback
  runtime/       ExecutionOrchestrator -- dynamic, workflow-less execution
  workflow/      Static DAG executor, graph validation, condition evaluation
  checkpoint/    Checkpoint writing and resume
  policies/      Tool permission engine and the approval service
  budget/        Budget meter and enforcement
  events/        Event bus and sinks (in-memory, PostgreSQL, Redis)
  observability/ Structured logging, OpenTelemetry tracing, Prometheus metrics
  llm/           Provider-agnostic client, real provider adapters, the mock provider
  tools/         Built-in tools (calculator, web_search, file I/O, python_exec, ...)
  persistence/   SQLAlchemy tables and repositories
  coordination/  Redis locks, semaphores, event streaming
  evaluation/    The benchmark: scenarios, judge, harness, report
  api/           FastAPI application
  cli/           The `orchestrator` console script
tests/           unit/ (no network) and integration/ (real PostgreSQL + Redis)
examples/        Runnable, narrated demos
benchmarks/      Standalone benchmark entry point and results
migrations/      Alembic migrations
docs/            This documentation
frontend/        Next.js web UI -- dashboard, execution detail, replay, benchmarks
```
