"""Integration tests for the MCP (Model Context Protocol) client.

Runs the real, official ``@modelcontextprotocol/server-filesystem``
reference server via ``npx`` and talks real JSON-RPC-over-stdio to it --
nothing about the protocol exchange is mocked. Skipped (not failed) when
``npx`` is not available, since Node.js is an external toolchain this
project does not otherwise depend on.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orchestration.agents.registry import AgentRegistry
from orchestration.domain.agent import AgentDefinition
from orchestration.domain.enums import PolicyEffect
from orchestration.domain.tool import ToolPermission
from orchestration.errors import InputValidationError
from orchestration.policies.engine import build_default_policy_engine
from orchestration.tools.base import ToolContext
from orchestration.tools.mcp import MCPClient, discover_mcp_tools
from orchestration.tools.registry import ToolRegistry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("npx") is None, reason="npx is not available"),
]


@pytest.fixture
def sandbox_dir(tmp_path: Path) -> Path:
    (tmp_path / "greeting.txt").write_text("hello mcp\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
async def mcp_client(sandbox_dir: Path) -> AsyncIterator[MCPClient]:
    client = MCPClient(f"npx -y @modelcontextprotocol/server-filesystem {sandbox_dir}")
    await client.start()
    yield client
    await client.aclose()


class TestMCPClient:
    async def test_initialize_and_list_tools_against_the_real_reference_server(
        self, mcp_client: MCPClient
    ) -> None:
        tools = await mcp_client.list_tools()
        names = {t["name"] for t in tools}
        assert "read_text_file" in names
        assert "write_file" in names

    async def test_call_tool_reads_a_real_file_through_the_real_server(
        self, mcp_client: MCPClient
    ) -> None:
        result = await mcp_client.call_tool("read_text_file", {"path": "greeting.txt"})
        text_blocks = [b["text"] for b in result["content"] if b["type"] == "text"]
        assert "hello mcp" in "\n".join(text_blocks)

    async def test_calling_an_unknown_path_returns_an_mcp_level_error(
        self, mcp_client: MCPClient
    ) -> None:
        result = await mcp_client.call_tool("read_text_file", {"path": "does-not-exist.txt"})
        assert result.get("isError") is True


class TestDiscoveredToolAdapter:
    async def test_a_discovered_tool_is_invocable_through_the_engines_tool_interface(
        self, mcp_client: MCPClient
    ) -> None:
        """The point of wrapping an MCP tool as a `Tool` is that it goes
        through the exact same `invoke()` (argument validation, timeout
        enforcement) as a built-in tool -- this calls that real path, not a
        shortcut to the raw MCP client.
        """
        tools = await discover_mcp_tools(mcp_client, server_name="fs")
        read_tool = next(t for t in tools if t.spec.name == "mcp_fs_read_text_file")

        result = await read_tool.invoke(
            {"path": "greeting.txt"},
            ToolContext(execution_id="exec_mcp_test", deadline_seconds=15.0),
        )
        assert "hello mcp" in result["content"]

    async def test_invalid_arguments_are_rejected_before_the_server_is_ever_called(
        self, mcp_client: MCPClient
    ) -> None:
        """Argument validation happens in `Tool.invoke()` regardless of what
        kind of tool it wraps -- an MCP tool does not get a pass on this.
        """
        tools = await discover_mcp_tools(mcp_client, server_name="fs")
        read_tool = next(t for t in tools if t.spec.name == "mcp_fs_read_text_file")

        with pytest.raises(InputValidationError, match="invalid arguments"):
            await read_tool.invoke(
                {},  # "path" is required
                ToolContext(execution_id="exec_mcp_test", deadline_seconds=15.0),
            )

    async def test_a_server_side_error_becomes_a_real_engine_error(
        self, mcp_client: MCPClient
    ) -> None:
        tools = await discover_mcp_tools(mcp_client, server_name="fs")
        read_tool = next(t for t in tools if t.spec.name == "mcp_fs_read_text_file")

        with pytest.raises(InputValidationError, match="reported an error"):
            await read_tool.invoke(
                {"path": "does-not-exist.txt"},
                ToolContext(execution_id="exec_mcp_test", deadline_seconds=15.0),
            )


class TestPolicyGating:
    async def test_an_mcp_tool_is_denied_by_default_and_allowed_once_granted(
        self, mcp_client: MCPClient
    ) -> None:
        """The whole point of routing an MCP tool through the same
        registration path as a built-in one: PolicyEngine's deny-by-default
        allowlist must gate it exactly the same way, with no bypass for
        "it came from MCP".
        """
        tools = ToolRegistry()
        for tool in await discover_mcp_tools(mcp_client, server_name="fs"):
            tools.register(tool)

        no_access_agents = AgentRegistry()
        no_access_agents.register(
            AgentDefinition(
                id="no_mcp_agent",
                name="No MCP Agent",
                description="Has no MCP permissions.",
                system_prompt="test",
            )
        )
        granted_agents = AgentRegistry()
        granted_agents.register(
            AgentDefinition(
                id="mcp_agent",
                name="MCP Agent",
                description="Explicitly granted the MCP read tool.",
                system_prompt="test",
                allowed_tools=(ToolPermission(tool="mcp_fs_read_text_file"),),
            )
        )

        denied_policy = build_default_policy_engine(agents=no_access_agents, tools=tools)
        denied = denied_policy.evaluate("no_mcp_agent", "mcp_fs_read_text_file", {"path": "x"})
        assert denied.effect is PolicyEffect.DENY

        # Not ALLOW: MCP tools register at RiskLevel.HIGH (see mcp.py), and
        # the engine's default rules require human approval for high-risk
        # calls regardless of allowlist membership -- a second, independent
        # layer of protection on top of deny-by-default, not a bypass of it.
        granted_policy = build_default_policy_engine(agents=granted_agents, tools=tools)
        granted = granted_policy.evaluate("mcp_agent", "mcp_fs_read_text_file", {"path": "x"})
        assert granted.effect is PolicyEffect.REQUIRE_APPROVAL
