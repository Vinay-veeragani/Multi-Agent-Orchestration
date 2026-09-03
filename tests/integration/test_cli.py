"""Integration tests for the ``orchestrator`` CLI, over a real network socket.

A CLI that shells out over HTTP is only faithfully tested by actually letting
it do that: these tests run a real FastAPI app on a real (ephemeral) port via
uvicorn, and drive the installed console script's Typer app against it with
:class:`typer.testing.CliRunner`, exactly as an operator's shell would.

Every ``CliRunner.invoke`` call runs inside ``asyncio.to_thread`` because the
CLI itself calls ``asyncio.run()`` per command (see ``cli/main.py``'s
``_run``) -- which cannot nest inside the event loop this test file's async
fixtures and the uvicorn server already share. A worker thread gives the
command's own ``asyncio.run()`` a loop of its own, while the server keeps
running concurrently in the original one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import uvicorn
from typer.testing import CliRunner, Result

from orchestration.api.app import create_app
from orchestration.cli.main import app as cli_app
from orchestration.config import Settings
from orchestration.coordination.redis import RedisCoordinator
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import MockProvider, MockRule, agent_output, routing_decision
from orchestration.persistence.database import Database

pytestmark = pytest.mark.integration

runner = CliRunner()


def _invoke_sync(args: list[str]) -> Result:
    return runner.invoke(cli_app, args)


async def _invoke(*args: str) -> Result:
    return await asyncio.to_thread(_invoke_sync, list(args))


async def _no_sleep(delay: float) -> None:
    return None


def _finishing_provider() -> MockProvider:
    return MockProvider(
        [
            MockRule(
                name="supervisor",
                match_system="supervisor",
                priority=10,
                responses=(
                    routing_decision("delegate", agents=["research_agent"]),
                    routing_decision("finalize", answer="five vendors found"),
                ),
            ),
            MockRule(name="agent", responses=(agent_output("five vendors found"),)),
        ]
    )


def _approval_provider() -> MockProvider:
    return MockProvider(
        [
            MockRule(
                name="supervisor",
                match_system="supervisor",
                priority=10,
                responses=(
                    routing_decision(
                        "request_human_approval",
                        approval_action="publish the report externally",
                        approval_risk_reason="visible to customers",
                    ),
                    routing_decision("finalize", answer="published"),
                ),
            )
        ]
    )


async def _serve(
    database: Database, redis_coordinator: RedisCoordinator, provider: MockProvider
) -> AsyncIterator[str]:
    # `_env_file=None`: keep this isolated from a developer's real `.env`
    # (see the same note in tests/integration/test_api.py's `_client`).
    settings = Settings(_env_file=None, api_require_auth=False)
    app = create_app(
        settings,
        llm=LLMClient.mock(provider, sleep=_no_sleep),
        database=database,
        redis=redis_coordinator,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes no event for this
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest_asyncio.fixture
async def live_api(
    database: Database, redis_coordinator: RedisCoordinator
) -> AsyncIterator[str]:
    """A real ``orchestrator`` API listening on a real ephemeral port."""
    async for url in _serve(database, redis_coordinator, _finishing_provider()):
        yield url


class TestAgentsList:
    async def test_lists_the_reference_agents(self, live_api: str) -> None:
        result = await _invoke("agents", "list", "--api-url", live_api)
        assert result.exit_code == 0
        assert "research_agent" in result.stdout
        assert "critic_agent" in result.stdout


class TestRunAndStatus:
    async def test_run_without_wait_returns_immediately(self, live_api: str) -> None:
        result = await _invoke("run", "compare CRM vendors", "--api-url", live_api)
        assert result.exit_code == 0
        assert "execution_id:" in result.stdout
        assert "status: pending" in result.stdout

    async def test_run_with_wait_polls_to_completion(self, live_api: str) -> None:
        result = await _invoke("run", "compare CRM vendors", "--api-url", live_api, "--wait")
        assert result.exit_code == 0
        assert "status: succeeded" in result.stdout
        assert "five vendors found" in result.stdout

    async def test_status_reports_an_unknown_execution_as_an_error(self, live_api: str) -> None:
        result = await _invoke("status", "exec_nonexistent", "--api-url", live_api)
        assert result.exit_code == 1
        assert "not_found" in result.output


class TestCancel:
    async def test_cancelling_an_unknown_execution_is_a_clean_error(self, live_api: str) -> None:
        result = await _invoke("cancel", "exec_nonexistent", "--api-url", live_api)
        assert result.exit_code == 1
        assert "not_found" in result.output


class TestResume:
    async def test_resuming_an_unknown_execution_is_a_clean_error(self, live_api: str) -> None:
        result = await _invoke("resume", "exec_nonexistent", "--api-url", live_api)
        assert result.exit_code == 1
        assert "not_found" in result.output


class TestApproveAndReject:
    async def test_approve_then_status_shows_the_completed_run(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async for api_url in _serve(database, redis_coordinator, _approval_provider()):
            created = await _invoke("run", "publish a report", "--api-url", api_url)
            execution_id = _extract_execution_id(created.stdout)

            status_result = created
            for _ in range(100):
                status_result = await _invoke("status", execution_id, "--api-url", api_url)
                if "waiting_for_approval" in status_result.stdout:
                    break
                await asyncio.sleep(0.05)
            assert "waiting_for_approval" in status_result.stdout

            approved = await _invoke(
                "approve", execution_id, "--by", "ops@example.test", "--api-url", api_url
            )
            assert approved.exit_code == 0

            final_status = status_result
            for _ in range(100):
                final_status = await _invoke("status", execution_id, "--api-url", api_url)
                if "succeeded" in final_status.stdout or "failed" in final_status.stdout:
                    break
                await asyncio.sleep(0.05)
            assert "status: succeeded" in final_status.stdout


def _extract_execution_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("execution_id:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no execution_id in output:\n{stdout}")
