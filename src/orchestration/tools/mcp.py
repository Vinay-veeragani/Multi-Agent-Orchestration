"""MCP (Model Context Protocol) client: stdio transport, tool discovery, and
translation into this engine's own :class:`Tool` abstraction.

A discovered MCP tool is wrapped as a :class:`~orchestration.tools.base.
FunctionTool` and registered like any other tool -- it is authorised by
:class:`~orchestration.policies.engine.PolicyEngine` before it ever runs,
exactly like a built-in tool. There is no separate, less-checked code path
for a remote tool call.

The wire format below (message shapes, field names, the newline-delimited
JSON framing) was captured against a real server -- the official
``@modelcontextprotocol/server-filesystem`` reference implementation, run
via ``npx`` -- not written from the spec text alone; see
``tests/integration/test_mcp.py``.
"""

from __future__ import annotations

import asyncio
import itertools
import json

from orchestration.domain.base import JsonDict
from orchestration.domain.enums import RiskLevel
from orchestration.domain.retry import NO_RETRY_POLICY
from orchestration.domain.tool import ToolSpec
from orchestration.errors import ConfigurationError, InputValidationError, ProviderUnavailableError
from orchestration.tools.base import FunctionTool, Tool, ToolContext

_PROTOCOL_VERSION = "2024-11-05"

#: MCP tool names are free-form; ToolSpec/Slug requires lowercase
#: [a-z0-9_-], max 64 chars. Anything else is folded to "_".
_SLUG_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789_-"


class MCPTransportError(ProviderUnavailableError):
    """The MCP server process died, sent garbage, or timed out.

    A ``ProviderUnavailableError`` subclass (retryable) rather than a
    terminal error: an MCP server hiccup is the same kind of transient
    external failure an LLM provider outage is.
    """

    code = "mcp_transport_error"


def _slugify(name: str, *, max_length: int = 64) -> str:
    folded = "".join(c if c in _SLUG_SAFE else "_" for c in name.lower())
    if not folded or folded[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        folded = f"t_{folded}"
    return folded[:max_length]


class MCPClient:
    """One connection to one MCP server over stdio.

    Messages are JSON-RPC 2.0, newline-delimited (the stdio transport's
    framing) -- one JSON object per line, matched by ``id``. Calls are
    serialised through a lock: this client is not meant to pipeline
    concurrent requests, which the reference servers do not reliably
    support either.
    """

    def __init__(self, command: str, *, timeout_seconds: float = 30.0) -> None:
        if not command.strip():
            raise ConfigurationError("MCP server command is empty", command=command)
        self._command = command
        self._timeout = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    async def start(self) -> JsonDict:
        """Spawn the server and perform the ``initialize`` handshake.

        Returns the server's ``serverInfo``/``capabilities``, for logging --
        nothing here branches on capabilities today.

        Launched through a shell (``create_subprocess_shell``, not
        ``_exec``) rather than a hand-split argv: on Windows, a command like
        ``npx ...`` is a ``.cmd`` shim that the OS can only resolve via a
        shell, not via direct ``CreateProcess``, and a shell also parses
        quoting the way an operator typing the command expects. The command
        is deployment configuration (``ORCH_MCP_SERVER_COMMAND``), set by
        whoever operates this deployment -- the same trust level as a
        database DSN or any other operator-supplied setting, not
        attacker-controlled input, so shell interpretation of it is an
        accepted, deliberate trust boundary rather than an injection risk.
        """
        self._process = await asyncio.create_subprocess_shell(
            self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        result = await self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-orchestration-engine", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[JsonDict]:
        result = await self._request("tools/list", {})
        return list(result.get("tools", []))

    async def call_tool(self, name: str, arguments: JsonDict) -> JsonDict:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def aclose(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError):
            process.kill()

    # -- wire protocol -------------------------------------------------

    async def _request(self, method: str, params: JsonDict) -> JsonDict:
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        async with self._lock:
            await self._write(payload)
            return await self._read_matching(request_id)

    async def _notify(self, method: str) -> None:
        async with self._lock:
            await self._write({"jsonrpc": "2.0", "method": method})

    async def _write(self, payload: JsonDict) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPTransportError("MCP server is not running", provider="mcp")
        self._process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_matching(self, request_id: int) -> JsonDict:
        if self._process is None or self._process.stdout is None:
            raise MCPTransportError("MCP server is not running", provider="mcp")
        while True:
            try:
                raw = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout=self._timeout
                )
            except TimeoutError as exc:
                raise MCPTransportError(
                    f"MCP server did not respond within {self._timeout}s", provider="mcp"
                ) from exc
            if not raw:
                stderr = b""
                if self._process.stderr is not None:
                    stderr = await self._process.stderr.read(4_096)
                raise MCPTransportError(
                    "MCP server closed its stdout unexpectedly",
                    provider="mcp",
                    stderr=stderr.decode("utf-8", errors="replace")[-2_000:],
                )
            try:
                message: JsonDict = json.loads(raw)
            except json.JSONDecodeError:
                continue  # a non-JSON-RPC line (server logs on stdout) -- ignore it
            if message.get("id") != request_id:
                continue  # a notification, or a response to a call we've stopped waiting on
            if "error" in message:
                error = message["error"]
                raise MCPTransportError(
                    f"MCP server error: {error.get('message', 'unknown error')}",
                    provider="mcp",
                    code=error.get("code"),
                )
            result: JsonDict = message.get("result", {})
            return result


def _extract_text(result: JsonDict) -> str:
    """Flatten an MCP ``content`` array (the common case: one or more text
    blocks) into a single string for a JSON-serialisable ToolResult.
    """
    parts = [
        str(block.get("text", ""))
        for block in result.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(parts)


def _translate_schema(schema: JsonDict) -> JsonDict:
    """MCP's ``inputSchema`` is already JSON Schema; strip the ``$schema``
    dialect marker (harmless either way, but this project's own schemas
    never carry one) and default a missing ``type`` to ``object``.
    """
    translated = {k: v for k, v in schema.items() if k != "$schema"}
    translated.setdefault("type", "object")
    return translated


def _make_tool(client: MCPClient, definition: JsonDict, *, server_name: str) -> Tool:
    mcp_name = str(definition["name"])
    tool_name = _slugify(f"mcp_{server_name}_{mcp_name}")
    description = str(definition.get("description") or f"MCP tool {mcp_name!r}")

    async def handler(arguments: JsonDict, context: ToolContext) -> JsonDict:
        result = await client.call_tool(mcp_name, arguments)
        if result.get("isError"):
            raise InputValidationError(
                f"MCP tool {mcp_name!r} reported an error: {_extract_text(result)}",
                tool=tool_name,
            )
        return {"content": _extract_text(result)}

    spec = ToolSpec(
        name=tool_name,
        description=description[:2_000],
        input_schema=_translate_schema(definition.get("inputSchema") or {}),
        # External and dynamically discovered: HIGH by default regardless of
        # what the server's own annotations claim, and never auto-retried.
        # Two independent gates then apply before this ever runs: PolicyEngine's
        # deny-by-default allowlist (an agent needs an explicit ToolPermission
        # naming it), and its default rules requiring human approval for any
        # HIGH-risk call -- neither is bypassed just because a tool came from MCP.
        risk=RiskLevel.HIGH,
        idempotent=False,
        retry_policy=NO_RETRY_POLICY,
        tags=frozenset({"mcp", server_name}),
    )
    return FunctionTool(spec, handler)


async def discover_mcp_tools(client: MCPClient, *, server_name: str) -> list[Tool]:
    """List an MCP server's tools and wrap each as an engine :class:`Tool`."""
    definitions = await client.list_tools()
    return [_make_tool(client, definition, server_name=server_name) for definition in definitions]
