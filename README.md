# Multi-Agent Orchestration Engine

### A Production-Shaped Runtime for Autonomous AI Agent Execution

**Multi-Agent Orchestration Engine** is a production-oriented AI runtime for coordinating autonomous agents across complex, long-running tasks.

Instead of treating agents as simple LLM calls, this project focuses on the infrastructure required to **safely execute, coordinate, observe, recover, and evaluate multi-agent systems**.

The engine provides:

* 🧠 LLM-driven supervisor routing
* 🔀 Dynamic agent delegation and parallel fan-out
* 🔁 Retry and failure recovery
* 💾 Durable checkpointing and resume
* 👤 Human-in-the-loop approval
* 🔐 Tool permission policies
* 💰 Execution budgets and limits
* 📡 Real-time SSE execution streaming
* 📊 Structured observability
* 🧪 Deterministic multi-agent evaluation
* 🔌 Pluggable LLM providers (OpenAI, Anthropic, Gemini, Groq, Ollama)
* 🖥️ Local/offline LLM support through Ollama
* 🗄️ PostgreSQL (+ pgvector)-backed durable execution state
* ⚡ Redis-backed runtime coordination
* 🖼️ A real Next.js web UI over the same API the CLI uses

> **Core idea:** LLMs decide *what should happen next*. The orchestration runtime decides *whether and how that decision is executed*.

---

# Why This Project?

Most multi-agent demonstrations look like:

```text
User
  ↓
LLM
  ↓
Agent A
  ↓
Agent B
  ↓
Final Answer
```

That is useful for demonstrating agent collaboration, but it leaves out the difficult engineering problems.

A real autonomous agent system must answer questions such as:

* What happens when an agent fails?
* Should the system retry or replan?
* Can independent agents execute concurrently?
* How is execution state persisted?
* What happens if the process crashes?
* Can a human pause and approve an action?
* How are agent tools authorized?
* How do we stop runaway execution?
* How are token and cost budgets enforced?
* How can a client observe execution in real time?
* How can an execution resume after interruption?
* How can different orchestration strategies be evaluated?
* How do we prevent malformed LLM decisions from directly controlling infrastructure?

This project is built around those problems.

---

# Architecture

```text
                         USER REQUEST
                              │
                              ▼
                    ┌────────────────────┐
                    │   AI SUPERVISOR    │
                    │                    │
                    │ Analyze            │
                    │ Route              │
                    │ Delegate           │
                    │ Fan-out            │
                    │ Retry              │
                    │ Replan             │
                    │ Approve            │
                    │ Finalize           │
                    └─────────┬──────────┘
                              │
                              ▼
                  ┌────────────────────────┐
                  │  ROUTING VALIDATION    │
                  │                        │
                  │ Schema Validation      │
                  │ Semantic Validation    │
                  │ Deterministic Fallback │
                  └───────────┬────────────┘
                              │
                              ▼
                 ┌───────────────────────────┐
                 │   ORCHESTRATION RUNTIME   │
                 │                           │
                 │ Scheduler                │
                 │ Dependencies              │
                 │ Parallel Execution        │
                 │ Retry Policies            │
                 │ Budget Enforcement        │
                 │ Tool Policies             │
                 │ Checkpointing             │
                 │ State Management          │
                 └─────────────┬─────────────┘
                               │
                ┌──────────────┼──────────────┐
                │              │              │
                ▼              ▼              ▼
           ┌─────────┐    ┌─────────┐    ┌─────────┐
           │ Agent A │    │ Agent B │    │ Agent C │
           └────┬────┘    └────┬────┘    └────┬────┘
                │              │              │
                ▼              ▼              ▼
             Tools          Tools          Tools
                │              │              │
                └──────────────┼──────────────┘
                               │
                               ▼
                    ┌────────────────────┐
                    │  EXECUTION STATE   │
                    │                    │
                    │ PostgreSQL         │
                    │ Checkpoints        │
                    │ Events             │
                    │ Results            │
                    └─────────┬──────────┘
                              │
                              ▼
                        FINAL RESULT
```

---

# Core Architecture Principle

The most important architectural decision is the separation between **AI decision-making** and **deterministic execution**.

### The Supervisor

The supervisor answers:

```text
"What should happen next?"
```

### The Runtime

The orchestration engine answers:

```text
"Is that decision valid?"

"Is it allowed?"

"Can we afford it?"

"Should it execute now?"

"Can it run in parallel?"

"What happens if it fails?"

"How do we recover?"

"How do we persist the state?"
```

This creates a controlled boundary around autonomous AI.

---

# Supervisor Routing

The supervisor dynamically determines how a task should progress, one decision at a time, against the *live* graph -- there is no upfront plan for a dynamic execution.

Supported execution decisions include:

```text
DELEGATE
FAN_OUT   (delegate to several agents in one turn)
RETRY
REPLAN
REQUEST_HUMAN_APPROVAL
RESPOND_DIRECTLY / FINALIZE
FAIL
```

Example:

```text
User Request
     │
     ▼
 Supervisor
     │
     ├──────────────┐
     ▼              ▼
Research         Analysis
Agent             Agent
     │              │
     └──────┬───────┘
            ▼
        Synthesis
            │
            ▼
         Finalizer
```

Every decision is validated before the routing layer lets it reach the execution engine.

---

# Three-Layer Routing Validation

LLM-generated routing decisions are not trusted blindly.

The system validates decisions through multiple layers:

```text
              LLM Decision
                   │
                   ▼
        ┌─────────────────────┐
        │ Schema Validation   │
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │ Semantic Validation │
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │ Deterministic       │
        │ Fallback Logic      │
        └──────────┬──────────┘
                   ▼
            Valid Decision
                   │
                   ▼
          Execution Runtime
```

Schema-invalid or semantically-wrong output degrades to a deterministic heuristic router rather than being retried against the model indefinitely or, worse, guessed at from prose. This is what makes routing testable at all: a benchmark can assert on the *decision*, not on whether a regex happened to match.

This matters because malformed model output should never directly become infrastructure behavior.

---

# Parallel Agent Execution

Independent work can be executed concurrently.

Example:

```text
                    Research Task
                         │
                         ▼
                      FAN-OUT
                 ┌───────┼───────┐
                 │       │       │
                 ▼       ▼       ▼
             Agent A  Agent B  Agent C
                 │       │       │
                 └───────┼───────┘
                         ▼
                        JOIN
                         │
                         ▼
                     Synthesis
```

The supervisor determines that work can be parallelized.

The runtime controls the actual concurrency, budget-checked per branch so a five-way fan-out that would blow the execution's budget is narrowed rather than started and failing halfway through.

This separation prevents the LLM from becoming responsible for low-level scheduling.

---

# Retry & Failure Recovery

Failures are treated as first-class execution states.

```text
Agent Execution
      │
      ▼
   Failure
      │
      ▼
 Failure Classification
      │
      ▼
   Supervisor
      │
 ┌────┼────────┐
 ▼    ▼        ▼
Retry Replan  Fail
```

Retries operate at the appropriate execution node rather than blindly restarting the entire execution graph.

Retry behavior is bounded by execution policies and budgets (a retry-storm cannot itself become the runaway cost).

---

# Durable Checkpointing

Long-running agent executions need durable state.

The runtime persists checkpoints to PostgreSQL -- content-hash deduplicated and optimistic-concurrency versioned, so two writers can never silently clobber each other's checkpoint.

```text
Execution
   │
   ├── Step 1 ✓
   ├── Step 2 ✓
   ├── Step 3 ✓
   ├── Step 4 ✓
   ├── Step 5 → interruption
   │
   ▼
Checkpoint
   │
   ▼
Resume
   │
   ├── Restore state
   ├── Restore completed work
   └── Continue execution
```

Checkpoint state contains the information required to continue an execution without restarting the entire task.

**Known limitation, not hidden:** the mechanism for tool-call idempotency across a crash-resume (`NodeState.committed_keys`, `InvocationRepository.claim_tool`/`find_completed_tool`) is real and tested in isolation, but nothing in the live execution path calls it yet. If an execution crashes between a tool call completing and its result being recorded, the resumed attempt calls the tool again -- a real duplicate side effect for a non-idempotent tool (`send_email`, `write_file`), not a hypothetical one. See [`docs/checkpointing-and-resume.md`](docs/checkpointing-and-resume.md).

---

# Human-in-the-Loop

Autonomous systems sometimes require human authorization.

The runtime supports explicit approval gates.

```text
Agent
  │
  ▼
Sensitive Action
  │
  ▼
Approval Required
  │
  ▼
┌───────────────────────┐
│ Human Approval        │
│                       │
│ APPROVE / REJECT      │
└───────────┬───────────┘
            │
       ┌────┴────┐
       ▼         ▼
    Continue    Stop
```

The execution remains durable while waiting for the approval decision -- including across a process restart. Approval identity is derived from *what* is being approved (`sha256(execution, node, action, arguments)`), not a boolean flag: a resumed node finds its prior decision instead of pausing forever, and an approval can never silently authorise a differently-argued action.

---

# Budget Enforcement

Agentic systems can consume unpredictable amounts of:

* tokens
* model calls
* tool calls
* execution time
* money
* retries

The runtime therefore treats resource limits as execution constraints.

Example:

```text
Execution Budget
────────────────────────
Max Tokens       20,000
Max Cost          $0.50
Max Steps            25
Max Tool Calls       50
Max Retries           3
Max Duration       120s
```

The runtime enforces these boundaries independently of the model -- both a hard `check()` before spending and a `reserve()`/`commit()` path for spends whose size is knowable in advance (a fan-out), so an over-budget branch is refused before it starts rather than discovered after all branches have run.

---

# Tool Permission System

Tools operate behind an explicit policy boundary.

```text
Agent
  │
  ▼
Tool Request
  │
  ▼
Policy Engine
  │
  ├── Allowed ──────► Execute
  │
  └── Denied ───────► Reject
```

The system follows a **deny-by-default** approach: every agent gets an explicit tool allowlist, scoped as narrowly as its job needs (e.g. the data agent gets no network tools at all; the code agent gets no shell or database access). Sensitive capabilities such as `exec_shell` are disabled by default and require explicit configuration.

---

# Real-Time Execution Streaming

Agent executions generate structured runtime events, including:

```text
EXECUTION_STARTED
SUPERVISOR_DECIDED
ROUTING_DEGRADED
NODE_STARTED
AGENT_INVOKED
TOOL_INVOKED
TOOL_COMPLETED
NODE_COMPLETED
CHECKPOINT_CREATED
RETRY_STARTED
REPLANNED
APPROVAL_REQUESTED
APPROVAL_GRANTED / APPROVAL_REJECTED
EXECUTION_FINALIZED
```

These events are persisted to PostgreSQL and streamed through Redis, so a client observes an execution while it is running instead of only seeing the final response:

```text
Backend
   │
   ▼
Execution Event
   │
   ├──────────► PostgreSQL
   │
   └──────────► Redis Stream
                    │
                    ▼
                   SSE
                    │
                    ▼
                Frontend
```

---

# Execution State

The execution state is treated as a durable domain object. It contains:

```text
Execution ID
Current status
Node states
Agent outputs
Budget usage
Pending approvals
Checkpoint information
Errors
Execution metadata
```

This allows the system to reconstruct and resume long-running executions from exactly where they left off.

---

# PostgreSQL + Redis

The system deliberately separates durable state from runtime coordination. There is no SQLite fallback, by design.

```text
             Orchestration Runtime
                      │
             ┌────────┴────────┐
             ▼                 ▼
       PostgreSQL            Redis
             │                 │
             │                 ├── Streams
             │                 ├── Coordination
             │                 ├── Locks
             │                 └── Concurrency
             │
             ├── Executions
             ├── Checkpoints
             ├── Events
             └── Results
```

### PostgreSQL (+ pgvector)

Used as the durable source of truth for executions, checkpoints, events, and results.

### Redis

Used for runtime coordination (locks, semaphores) and event streaming.

---

# LLM Provider Abstraction

The orchestration engine is decoupled from any individual model provider.

```text
                 LLM Provider Interface
                         │
        ┌────────┬───────┼───────┬────────┐
        ▼        ▼       ▼       ▼        ▼
      OpenAI  Anthropic Gemini  Groq    Ollama
                                          │
                                          ▼
                                    Local Models
```

OpenAI, Anthropic, and Gemini adapters are real, HTTP-verified implementations, not stubs. Groq and Ollama both speak the same OpenAI-compatible `/chat/completions` shape and reuse that adapter. No LLM API key is required to run this project at all -- a deterministic mock provider drives every test, demo, and benchmark scenario in this repository.

---

# Offline / Local LLM Support

The architecture supports local LLM execution through Ollama, via the same OpenAI-compatible adapter (`ORCH_OLLAMA_ENABLED=true`).

```text
Orchestration Runtime
        │
        ▼
   Provider Layer
        │
        ▼
      Ollama
        │
        ▼
   Local LLM
```

This allows experimentation without requiring a cloud LLM provider.

> The Ollama adapter is verified with a real HTTP request/response cycle against a fake server shaped exactly like Ollama's documented API (URL, no-auth headers, payload, response parsing all real) -- but has not yet been run against an actually-installed Ollama instance on real hardware. Verify on the target machine before treating a specific local model as production-tested.

---

# Deterministic Mock Provider

The project includes a deterministic mock LLM provider.

This is intentionally used for:

* automated testing
* demonstrations
* benchmark scenarios
* reproducible evaluation

The provider produces scripted responses instead of random model outputs, which makes it possible to evaluate the **orchestration runtime itself** without introducing model variability into every test.

---

# Evaluation & Benchmarking

The project does not evaluate multi-agent systems solely through screenshots or subjective examples. It includes a deterministic benchmark methodology for comparing orchestration strategies -- 54 scenarios (parallel fan-out, retry recovery, tool permission denial, human approval, budget exhaustion, and more) run under four arms:

| arm                    | passed | completion | routing accuracy | avg latency* | tokens |
|------------------------|-------:|-----------:|------------------:|-------------:|-------:|
| baseline                | 11/54 |      20.4% |             56.7% |        3.8ms | 14,669 |
| supervisor              | 48/54 |      88.9% |            100.0% |      345.2ms | 23,784 |
| supervisor + retry      | 54/54 |     100.0% |            100.0% |      374.4ms | 25,995 |
| supervisor + parallel   | 54/54 |     100.0% |            100.0% |      355.8ms | 25,995 |

<sub>*Latency is mock-provider engine wall-clock (routing, scheduling, checkpoint plumbing) -- not real LLM latency. Reproduce with `orchestrator benchmark --test-db`; see [`docs/evaluation-benchmark.md`](docs/evaluation-benchmark.md) for what each arm isolates and how to reproduce these numbers.</sub>

`baseline` (a single heuristically-chosen agent, no LLM supervisor, no retry, no parallelism) is a genuine ablation floor, not a strawman -- it uses the engine's own deterministic keyword/capability router, the same one the LLM-driven path falls back to. The gap between `supervisor` and the two retry-enabled arms is exactly the 6 retry-recovery scenarios, designed to fail without retry.

---

# Observability

Agentic systems are difficult to debug when the only output is:

```text
"Here is the final answer."
```

This runtime exposes the execution lifecycle across:

```text
Execution
Agent
Node
LLM Call
Tool
Retry
Checkpoint
Approval
Budget
Latency
Errors
Routing
```

via structured logs (with secret redaction), OpenTelemetry tracing, and Prometheus-compatible metrics.

---

# Reference Agents

Eight specialist agents ship with the engine (`src/orchestration/agents/definitions.py`), each a data definition, not a subclass -- an equivalent agent can be registered over the HTTP API with no code deploy. Every one carries an explicit, deny-by-default tool allowlist:

| Agent | Job | Tools |
|---|---|---|
| `research_agent` | Searches and reads to gather information, with mandatory citations | `web_search`, `read_file`, `http_request` |
| `pricing_agent` | Research variant focused on prices, tiers, billing periods, TCO | (same as research) |
| `feature_agent` | Research variant focused on what a product actually does vs. roadmap claims | (same as research) |
| `data_agent` | Profiles tabular data, computes statistics, checks data quality, charts | `read_file`, `python_exec`, `write_file` (scoped), `calculator` -- no network |
| `code_agent` | Navigates a repo, searches code, runs tests | `read_file`, `write_file` (scoped), `python_exec` -- no shell, no DB |
| `analyst_agent` | Compares/aggregates other agents' outputs, flags disagreements | `calculator` only |
| `critic_agent` | Audits another agent's output for unsupported claims and contradictions | `web_search` (capped, spot-checks only) |
| `finalizer_agent` | Synthesises everything into the final answer the user receives | `write_file` (scoped) |

For a dynamic execution, the supervisor picks which of these to delegate to (and how many in parallel) purely from the task text against each agent's declared capabilities -- there is no hardcoded routing table.

---

# End-to-End Example

Consider:

```text
Research three competitors,
compare their pricing,
analyze their strengths,
and produce a recommendation.
```

The runtime can execute:

```text
                    User Request
                         │
                         ▼
                     Supervisor
                         │
                         ▼
                      FAN-OUT
             ┌───────────┼───────────┐
             ▼           ▼           ▼
        Research A   Research B   Research C
             │           │           │
             └───────────┼───────────┘
                         ▼
                     Synthesis
                         │
                         ▼
                   Recommendation
                         │
                         ▼
                       Result
```

If Research B fails:

```text
Research B
    │
    ▼
 Failure
    │
    ▼
Supervisor
    │
    ▼
 Retry / Replan
    │
    ▼
Continue
```

If the execution is interrupted:

```text
Running Execution
       │
       ▼
   Checkpoint
       │
       ▼
   Process Restart
       │
       ▼
     Resume
       │
       ▼
Continue from durable state
```

---

# Web UI

A Next.js 16 (App Router) + TypeScript + Tailwind v4 app under [`frontend/`](frontend/) -- a real, working dashboard, not a mockup. It talks to the same HTTP API the CLI uses; there is no separate write path or database of its own, and the orchestrator API key stays server-side (Server Components + a same-origin SSE proxy), never reaching the browser bundle.

| Page | Shows |
|---|---|
| `/` | Recent executions -- status, cost, created time |
| `/executions/[id]` | Nodes, budget usage, live SSE event stream while running, a play/step/scrub replay once finished, and the recorded agent/tool invocations |
| `/executions/new` | Start an execution (task, optional success criteria, optional static workflow) |
| `/agents`, `/agents/[id]` | The registry every workflow node and the supervisor reference -- capabilities, deny-by-default tool allowlist, system prompt |
| `/approvals` | Pending human-in-the-loop approve/reject requests |
| `/benchmarks`, `/benchmarks/[id]` | Recent `orchestrator benchmark` reports and their ablation comparison |
| `/observability` | Durable execution history -- not live counters |
| `/providers` | Configured LLM providers and active-provider selection |

```bash
cd frontend
cp .env.local.example .env.local   # point at the API above; set the same key
npm install
npm run dev
```

Requires the API from the Quickstart below to already be running. When no real LLM provider is configured (the default), the UI shows a **demo mode** banner rather than staying silent about it -- `GET /health`'s `demo_mode` field drives it.

---

# Technology Stack

| Layer                | Technology                                    |
| -------------------- | --------------------------------------------- |
| Language             | Python                                        |
| API                  | FastAPI                                       |
| Frontend             | Next.js 16 (App Router), TypeScript, Tailwind v4 |
| Database             | PostgreSQL (+ pgvector)                       |
| Runtime Coordination | Redis                                         |
| Validation           | Pydantic v2                                   |
| LLM Providers        | OpenAI / Anthropic / Gemini / Groq / Ollama   |
| Streaming            | Server-Sent Events                            |
| Observability        | OpenTelemetry / Prometheus-compatible metrics |
| Testing              | Pytest                                        |
| Type Checking        | Mypy                                          |
| Linting              | Ruff                                          |
| Containerization     | Docker                                        |
| Configuration        | Pydantic Settings                             |

---

# Project Structure

```text
Multi-Agent-Orchestration/
│
├── src/
│   └── orchestration/
│       ├── domain/        Pydantic v2 models: execution, workflow, routing, budget, checkpoint, approval
│       ├── agents/        Agent registry, reference agents, the reason/act runtime
│       ├── supervisor/    LLM-driven routing (3-layer validated) + heuristic fallback
│       ├── runtime/       ExecutionOrchestrator -- dynamic, workflow-less execution
│       ├── workflow/      Static DAG executor, graph validation, condition evaluation
│       ├── checkpoint/    Checkpoint writing and resume
│       ├── policies/      Tool permission engine and the approval service
│       ├── budget/        Budget meter and enforcement
│       ├── events/        Event bus and sinks (in-memory, PostgreSQL, Redis)
│       ├── observability/ Structured logging, OpenTelemetry tracing, Prometheus metrics
│       ├── llm/           Provider-agnostic client, real provider adapters, the mock provider
│       ├── tools/         Built-in tools (calculator, web_search, file I/O, python_exec, ...)
│       ├── persistence/   SQLAlchemy tables and repositories
│       ├── coordination/  Redis locks, semaphores, event streaming
│       ├── evaluation/    The benchmark: scenarios, judge, harness, report
│       ├── api/           FastAPI application
│       └── cli/           The `orchestrator` console script
│
├── tests/           unit/ (no network) and integration/ (real PostgreSQL + Redis)
├── examples/        Runnable, narrated demos (research, data analysis, human approval)
├── benchmarks/      Standalone benchmark entry point and results
├── docs/            Design docs -- see Documentation below
├── frontend/        Next.js web UI
├── migrations/      Alembic migrations
├── scripts/         seed_demo_data.py and other one-off ops scripts
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

---

# Testing

The repository contains an extensive test suite covering:

* supervisor routing
* routing validation
* agent execution
* parallel execution
* retries
* checkpointing
* resume
* human approval
* budgets
* policy enforcement
* persistence
* API behavior
* SSE streaming
* evaluation
* provider adapters
* MCP integration
* failure handling

### Current verification

```text
943 tests collected
941 passed
0 genuine failures
```

Two integration tests are order-dependent when the full suite runs together (an MCP-startup-wiring test and a CLI polling test) and pass cleanly in isolation -- a known suite-ordering flake, not a product bug. The integration suite runs against real PostgreSQL and Redis rather than replacing the infrastructure with mocks.

---

# Security & Safety Model

The runtime treats autonomous execution as a controlled system.

Important safeguards include:

### Deny-by-default tools

Sensitive tools require explicit authorization, scoped per agent.

### Budget enforcement

Execution cannot consume unlimited resources.

### Approval gates

Sensitive operations can require human approval, durable across a restart.

### Input validation

Model-generated execution decisions are validated (schema, then semantic) before execution.

### Path protection

Filesystem-related operations are guarded against traversal.

### Read-only database tooling

Database query tools reject mutating statements.

### Explicit dangerous capabilities

Shell execution is disabled by default and clearly documented as a high-risk capability. Where it or `python_exec` is enabled, isolation is process-level (subprocess, timeout, output cap) -- not a sandboxing security boundary. Per-agent permission scoping is the real control.

---

# What This Project Is NOT

This project is intentionally focused. It is not:

* a workflow automation SaaS
* an n8n replacement
* a no-code agent builder
* a generic chatbot
* a simple LangChain demo
* a collection of independent AI agents
* a multi-tenant enterprise platform
* a distributed global scheduler
* a demonstration of a real LLM's routing *quality* -- every test, demo, and benchmark in this repository runs against the deterministic mock provider; the engine mechanics are real and fully exercised, model judgement is not, because no live API key is configured for this project's own CI/tests

The focus is the **orchestration and execution runtime underneath autonomous AI systems**.

---

# Engineering Trade-offs

### LLMs do not directly control infrastructure

Model output is validated before execution.

### PostgreSQL is the durable source of truth

Execution state should survive process restarts.

### Redis handles runtime coordination

Short-lived coordination and event streaming are separated from durable state.

### Deterministic evaluation

Benchmarks use controlled model behavior so orchestration changes can be measured reproducibly.

### Bounded autonomy

Retries, budgets, steps, and tools are explicitly constrained.

### Honest failure states

Failures are represented as execution states rather than silently swallowed.

---

# Current Limitations

This repository is production-shaped, but not presented as a fully production SaaS platform.

Known limitations include:

* Execution ownership is currently single-process; horizontal scaling requires distributed execution ownership/claiming.
* Tool-call idempotency across a crash-resume is built and tested in isolation but not yet wired into the live execution path (see Durable Checkpointing above).
* Live cloud-provider smoke tests require credentials.
* Live Ollama execution depends on an installed and running Ollama instance.
* Docker end-to-end verification depends on the host Docker environment.
* The system currently follows a single-tenant reference deployment model.
* The web UI is deliberately read-mostly, and a few nav sections (Tools, Knowledge, Settings) are placeholders shown for the product's intended shape but not yet wired to real pages.

These limitations are intentionally documented rather than hidden.

---

# Why This Architecture Matters

The interesting problem is not:

> "Can an LLM call another LLM?"

The interesting problem is:

> **"How do you build reliable infrastructure around autonomous decisions?"**

That requires solving:

```text
                Autonomous AI
                     │
                     ▼
              ┌──────────────┐
              │   Supervisor │
              └──────┬───────┘
                     │
                     ▼
              Decision Validation
                     │
                     ▼
              Execution Runtime
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
    Policies       Budgets      Scheduling
       │             │             │
       └─────────────┼─────────────┘
                     ▼
              Agent Execution
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
    Retry        Checkpoint       Events
       │             │             │
       └─────────────┼─────────────┘
                     ▼
              Durable Result
```

The project therefore treats **agent orchestration as an infrastructure problem**, not simply a prompting problem.

---

# Future Directions

Potential extensions include:

* distributed execution ownership
* multi-tenant isolation
* live tool-call idempotency on the crash-resume path
* advanced model routing / cost-aware model selection
* adaptive scheduling
* richer agent capability discovery
* a Tools page and a live MCP-tool inspector in the web UI
* workflow versioning
* event-sourced execution
* advanced policy engines
* cross-agent protocol interoperability

---

# Portfolio Highlights

This project demonstrates experience across multiple engineering layers:

```text
AI Engineering
    │
    ├── LLM supervisor
    ├── Agent routing
    └── Multi-agent execution

Backend Engineering
    │
    ├── FastAPI
    ├── PostgreSQL
    └── Redis

Frontend Engineering
    │
    ├── Next.js (App Router)
    ├── Server Components + Server Actions
    └── Real-time SSE consumption

Distributed Systems Concepts
    │
    ├── Concurrency
    ├── Coordination
    ├── Streaming
    └── Durable state

Reliability Engineering
    │
    ├── Retry
    ├── Checkpoint
    ├── Resume
    └── Failure handling

AI Safety / Control
    │
    ├── Tool policies
    ├── Budgets
    ├── Approval gates
    └── Validation

Observability
    │
    ├── Structured events
    ├── Metrics
    └── Tracing

Evaluation
    │
    ├── Deterministic benchmarks
    └── Strategy comparison
```

---

# Quick Start

## 1. Clone

```bash
git clone https://github.com/Vinay-veeragani/Multi-Agent-Orchestration.git
cd Multi-Agent-Orchestration
```

## 2. Bare metal

Requires PostgreSQL 16+ with the `vector` extension available, and Redis.

```bash
pip install -e ".[dev]"
cp .env.example .env               # edit ORCH_PG_DSN / ORCH_REDIS_URL if needed
alembic upgrade head
uvicorn orchestration.api.app:create_app --factory --reload
curl http://127.0.0.1:8000/health
```

## 2 (alt). Docker

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
```

This starts PostgreSQL+pgvector, Redis, runs migrations once, then the API. See [`docs/deployment.md`](docs/deployment.md) for configuration.

## 3. Try it

```bash
orchestrator agents list
orchestrator run "compare CRM vendors on pricing" --wait
orchestrator benchmark --category simple --test-db
```

Three narrated, runnable demos live under [`examples/`](examples/): competitive intelligence (parallel research fan-out), data analysis (a real tool call mid-run), and human approval (a durable pause across a simulated process restart). `python scripts/seed_demo_data.py` runs all three plus a benchmark slice in one go, so a freshly migrated database has something to look at in the [web UI](#web-ui) instead of empty lists.

## 4. Web UI (optional)

```bash
cd frontend
cp .env.local.example .env.local   # point at the API above; set the same key
npm install
npm run dev
```

## 5. Run Tests

```bash
pytest
```

---

# Documentation

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
| [`docs/mcp-tools.md`](docs/mcp-tools.md) | Connecting an MCP server: discovery, the two gates that keep it deny-by-default |
| [`docs/evaluation-benchmark.md`](docs/evaluation-benchmark.md) | Benchmark methodology, scenarios, and how to reproduce the numbers above |
| [`docs/deployment.md`](docs/deployment.md) | Docker/compose and production configuration notes |
| [`frontend/README.md`](frontend/README.md) | The web UI: pages, how it talks to the API, running it |

---

# Author

**Vinay Veeragani**

AI Engineer focused on:

* Agentic AI
* LLM Systems
* RAG
* Multi-Agent Systems
* AI Infrastructure
* Computer Vision
* AI Evaluation
* Production-oriented AI Engineering
