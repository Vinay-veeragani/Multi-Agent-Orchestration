# Budget enforcement and tool permissions

## Budget

A `Budget` is a set of independent, optional hard ceilings: cost (USD),
tokens, wall-clock duration, agent steps, tool calls, retries. `None` means
unmetered on that dimension. Every ceiling is hard -- crossing it raises
`BudgetExceededError`, naming the dimension, the limit, and what was
consumed, which the executor re-raises past its own node-retry handling (see
[`workflow-engine.md`](workflow-engine.md)) rather than treating as an
ordinary node failure.

`BudgetMeter` wraps a `Budget` and the running `BudgetUsage`, checked at
defined points (before an agent's reasoning iteration, before a tool call,
before finalising) -- not continuously, which is why a single very large call
can, in principle, land the actual spend over a very tight limit before the
next check catches it. That is the correct trade-off for where checks
naturally occur; it is not a token-by-token metering system.

`Budget.tightened_to(other)` merges two budgets element-wise to the
*stricter* value in each dimension -- how a per-request override is combined
with a deployment's configured default: a request may narrow the ceiling,
never widen it. The same rule governs how an enclosing execution's budget is
reconciled with an agent-level one.

A request too tight to complete anything meaningful is exactly what the
benchmark's `budget` scenario category tests -- see
[`evaluation-benchmark.md`](evaluation-benchmark.md).

## Deny-by-default tool permissions

An agent's `allowed_tools` is a strict allowlist (`ToolPermission` entries,
each with an optional `max_calls` and path/argument constraints). A tool
absent from it cannot be called, regardless of what the model asks for --
there is no default-allow path. `PolicyEngine.evaluate()` is a six-layer
authorisation check (tool registered and enabled, on the agent's allowlist,
call-count budget not exceeded, argument constraints satisfied, risk-level
gating, explicit deny rules) producing one of `ALLOW` / `DENY` /
`REQUIRE_APPROVAL`.

`REQUIRE_APPROVAL` is where policy and human-in-the-loop meet:
`ApprovalService.tool_authoriser()` wraps the policy authoriser so a
previously-granted approval lets a matching call through without asking
again -- see [`human-in-the-loop.md`](human-in-the-loop.md).

## Built-in tools and their risk levels

| Tool | Risk | Notes |
|---|---|---|
| `calculator` | SAFE | AST-walk over an operator allowlist, never `eval` |
| `web_search` | SAFE | Deterministic offline corpus |
| `read_file` / `write_file` | LOW / MEDIUM | Confined to a sandbox root; path escapes rejected |
| `http_request` | MEDIUM | SSRF denylist; mutating methods opt-in only |
| `db_query` | MEDIUM | Read-only transaction, enforced at the transaction level |
| `python_exec` | HIGH | Isolated subprocess (`-I`), timeout, output cap -- **not a security sandbox**, see the README's "What this project is NOT" |
| `send_email` | HIGH | `requires_approval=True`; records to an outbox, never actually sends |
| `exec_shell` | CRITICAL | Not registered unless explicitly enabled -- arbitrary command execution |

## See also

- [`observability.md`](observability.md) -- every policy decision and budget
  check is also a metric and a log line.
- [`human-in-the-loop.md`](human-in-the-loop.md) -- what `REQUIRE_APPROVAL`
  actually leads to.
