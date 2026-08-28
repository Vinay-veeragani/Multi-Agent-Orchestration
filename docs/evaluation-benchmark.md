# Evaluation benchmark

54 deterministic scenarios, each run under four configurations ("arms"),
grading an observed execution against declared expectations. Every scenario
scripts the exact LLM replies (and, for retry scenarios, the exact
failures) via a `MockProvider` -- the same scenario produces the same
trajectory on every run, which is what makes a change in a benchmark number
a real regression rather than model variance.

## The four arms: an ablation, not four independent products

| Arm | Supervisor? | Retry | Parallelism |
|---|---|---|---|
| `baseline` | No -- `HeuristicRouter` only | disabled (1 attempt) | disabled (1 node at a time) |
| `supervisor` | Yes | disabled | disabled |
| `supervisor-retry` | Yes | engine default | disabled |
| `supervisor-parallel` | Yes | engine default | engine default (the full engine) |

`baseline` is a genuine floor, not a strawman: it uses the engine's own
deterministic keyword/capability matcher (`HeuristicRouter`), the same
fallback the LLM-driven path uses when a model reply can't be validated (see
[`supervisor-and-routing.md`](supervisor-and-routing.md)). Retry and
parallelism are toggled through two real configuration knobs
(`ExecutionOrchestrator`'s `node_retry_policy` override and
`max_concurrent_nodes`), not through separate code paths -- see
[`dynamic-orchestration.md`](dynamic-orchestration.md).

## Scenario categories

| Category | Count | What it isolates |
|---|--:|---|
| `simple` | 8 | One agent, one round -- the floor every arm should clear |
| `parallel` | 6 | Several agents fanned out in one decision |
| `chain` | 6 | Two sequential delegations, second informed by the first |
| `retry` | 6 | First attempt faults (exhausting the agent's own LLM-level retry budget); only node-level retry recovers it |
| `tool` | 6 | A required tool call that must actually succeed |
| `deny` | 5 | A tool outside the agent's allowlist must be refused, not run |
| `approval` | 7 | A supervisor-requested human decision, auto-approved or auto-rejected |
| `budget` | 4 | A ceiling too tight to complete; must stop cleanly, not exhaust silently |
| `fail` | 3 | A task nothing can serve |
| `respond` | 3 | No delegation needed -- the supervisor answers directly |

## Results (commit `aed23dd`, report `eval_c760d1ff9d464cbfa89e`)

| arm | passed | completion | routing accuracy | avg latency* | p95 latency* | tokens |
|---|--:|--:|--:|--:|--:|--:|
| baseline | 11/54 | 20.4% | 56.7% | 3.8ms | 8.7ms | 14,669 |
| supervisor | 48/54 | 88.9% | 100.0% | 345.2ms | 643.9ms | 23,784 |
| supervisor-retry | 54/54 | 100.0% | 100.0% | 374.4ms | 796.6ms | 25,995 |
| supervisor-parallel | 54/54 | 100.0% | 100.0% | 355.8ms | 702.0ms | 25,995 |

\* **Mock-provider engine wall-clock, not real LLM latency.** Every call in
every scenario goes through `MockProvider` with zero synthetic delay,
recorded plainly in every generated report's `provider_note` field. These
numbers measure routing, scheduling, and checkpoint plumbing -- genuinely
useful for spotting an engine-side regression, not a stand-in for real
provider response time.

`supervisor` fails exactly the 6 `retry` scenarios and nothing else -- by
design, since those scenarios script a fault that only node-level retry can
recover from. `baseline`'s 43 failures are each attributable to a real,
specific structural limitation: it delegates to exactly one agent (never
satisfies a `parallel`/`chain` scenario's required-agent set), has no
approval capability at all, and its keyword-based routing occasionally picks
the wrong agent for an ambiguous task description (`routing accuracy` is
56.7%, not 100%, precisely because of this).

## Reproducing this

```bash
orchestrator benchmark --test-db                    # everything, against the disposable test DB
orchestrator benchmark --category retry --test-db    # one category
python benchmarks/run_benchmark.py --test-db --no-persist
```

Every run writes a full JSON report (`BenchmarkReport`, including
`git_sha`, environment, and every per-scenario `ScenarioResult`) to
`benchmarks/results/` and, unless `--no-persist`, to the `benchmark_runs`
table -- an operator can audit exactly what happened on any prior run, not
just the printed summary.

## See also

- [`../src/orchestration/evaluation/scenarios.py`](../src/orchestration/evaluation/scenarios.py)
  -- every scenario, in full.
- [`../src/orchestration/evaluation/judge.py`](../src/orchestration/evaluation/judge.py)
  -- exactly how an assertion is graded.
