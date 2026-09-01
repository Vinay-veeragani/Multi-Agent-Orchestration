# MCP (Model Context Protocol) tools

An MCP server's tools are discovered at startup and registered exactly like
a built-in tool -- there is no separate, less-checked code path for a
remote tool call. `src/orchestration/tools/mcp.py`'s wire-format handling
was captured against a real server (the official
`@modelcontextprotocol/server-filesystem` reference implementation, run via
`npx`), not written from the spec text alone -- see
[`../tests/integration/test_mcp.py`](../tests/integration/test_mcp.py).

## Enabling it

```bash
ORCH_MCP_ENABLED=true
ORCH_MCP_SERVER_COMMAND="npx -y @modelcontextprotocol/server-filesystem /path/to/allowed/dir"
ORCH_MCP_SERVER_NAME=fs   # optional, used only as the tool-name prefix
```

Off by default: connecting to an external tool server is a new trust
boundary, not something a deployment should acquire silently. If enabled
with a command configured and that command fails to start, `build_app_state`
lets the error propagate -- an operator who turned this on should learn
immediately that the server is unreachable, not silently run with fewer
tools than they think they have.

`ORCH_MCP_SERVER_COMMAND` is launched through a shell
(`asyncio.create_subprocess_shell`), not a hand-split argv: on Windows, a
command like `npx ...` is a `.cmd` shim only a shell can resolve. This is
deployment configuration set by whoever operates the deployment -- the same
trust level as a database DSN -- not attacker-controlled input.

## What gets registered

Each discovered tool becomes `mcp_{server_name}_{tool_name}` (e.g.
`mcp_fs_read_text_file`), wrapped as a
[`FunctionTool`](../src/orchestration/tools/base.py) whose handler calls
`MCPClient.call_tool()`. Its `ToolSpec`:

- `input_schema` is the MCP server's own `inputSchema`, passed through
  (draft-07 JSON Schema from the reference server validates fine under this
  engine's Draft 2020-12 validator for the common keywords every MCP tool
  uses).
- `risk=RiskLevel.HIGH` and `idempotent=False` regardless of what the
  server's own tool annotations claim -- a conservative floor, not a
  measurement of the specific tool.

## Two independent gates, neither bypassed

1. **Deny-by-default allowlist.** `PolicyEngine` denies any tool call from
   an agent whose `allowed_tools` doesn't name it -- an MCP tool is invisible
   to every agent until an operator adds a `ToolPermission` for its exact
   registered name to a specific agent's definition. Nothing does this
   automatically.
2. **HIGH-risk approval gate.** Even once granted, the engine's default
   policy rules require human approval for any HIGH-risk call -- which
   every MCP tool is, by the point above. Granting an agent access to an
   MCP tool does not mean it can call it unattended.

A tool-level error (MCP's `isError: true` result, e.g. "file not found") is
raised as a real `InputValidationError`, not swallowed -- the agent sees a
real failure it can adapt to, same as a built-in tool.

## What this is not

- Not a marketplace or dynamic per-execution server selection: one server,
  configured once at startup, for the life of the process.
- Not a way around the sandbox model described in the root README's "What
  this project is NOT" -- an MCP server runs as its own OS process with
  whatever access its own command line grants it (e.g. the filesystem
  server's allowed-directory argument); this engine does not additionally
  sandbox it.
- No tool-call arguments/results from an MCP tool are treated specially by
  the invocation-inspection routes (`GET /executions/{id}/tool-invocations`)
  -- they're recorded and read back exactly like any other tool call's.

## See also

- [`budget-and-policies.md`](budget-and-policies.md) -- the policy engine
  and approval gating this relies on.
- [`../src/orchestration/tools/mcp.py`](../src/orchestration/tools/mcp.py)
  -- the client and adapter, in full.
