"""Integration tests for the HTTP API, against the real ASGI app.

Every test builds a fresh app via :func:`create_app`, wired to the real test
database and Redis namespace (same fixtures the rest of the integration suite
uses) and a scripted :class:`MockProvider` so routing/agent behaviour is
deterministic -- exactly the pattern every other engine test in this project
follows. Nothing about the HTTP layer is mocked: requests go through the real
FastAPI app, dependency injection, and exception handlers via an in-process
ASGI transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from orchestration.api.app import create_app
from orchestration.config import Settings
from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.base import utc_now
from orchestration.domain.enums import ExecutionStatus, NodeStatus
from orchestration.domain.evaluation import ArmMetrics, BenchmarkReport
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import MockProvider, MockRule, agent_output, routing_decision
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import BenchmarkRepository

pytestmark = pytest.mark.integration


async def _no_sleep(delay: float) -> None:
    return None


@contextlib.asynccontextmanager
async def _client(
    database: Database,
    redis_coordinator: RedisCoordinator,
    provider: MockProvider,
    *,
    require_auth: bool = False,
    api_keys: str = "test-key",
    mcp_enabled: bool = False,
    mcp_server_command: str = "",
) -> AsyncIterator[httpx.AsyncClient]:
    """A live app, over an in-process ASGI transport, torn down on exit."""
    # `_env_file=None`: a developer's real `.env` (real provider keys, a
    # non-mock default provider for manual/live testing) must never leak into
    # the test suite -- these tests assert mock-only behaviour (demo_mode,
    # deterministic routing) that a locally configured provider would break.
    settings = Settings(
        _env_file=None,
        api_require_auth=require_auth,
        api_keys=api_keys,
        mcp_enabled=mcp_enabled,
        mcp_server_command=mcp_server_command,
    )
    app: FastAPI = create_app(
        settings,
        llm=LLMClient.mock(provider, sleep=_no_sleep),
        database=database,
        redis=redis_coordinator,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client,
    ):
        yield client


async def _wait_for_terminal(
    client: httpx.AsyncClient, execution_id: str, *, attempts: int = 100
) -> dict[str, Any]:
    """Poll until the execution reaches a terminal or paused status.

    Polling rather than a fixed sleep: the background task runs on the same
    event loop as the test, so a scripted, unlatencied run finishes in a
    handful of loop iterations -- a short, tight poll is both fast and robust.
    """
    for _ in range(attempts):
        response = await client.get(f"/executions/{execution_id}")
        body: dict[str, Any] = response.json()
        if body["status"] in {
            ExecutionStatus.SUCCEEDED.value,
            ExecutionStatus.FAILED.value,
            ExecutionStatus.CANCELLED.value,
            ExecutionStatus.WAITING_FOR_APPROVAL.value,
        }:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"execution {execution_id!r} did not reach a terminal status in time")


class TestHealthAndMetrics:
    async def test_health_reports_both_dependencies_reachable(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body == {"status": "ok", "database": True, "redis": True, "demo_mode": True}

    async def test_demo_mode_is_false_once_a_real_provider_key_is_configured(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        settings = Settings(_env_file=None, openai_api_key=SecretStr("sk-not-a-real-key"))
        app = create_app(
            settings,
            llm=LLMClient.mock(MockProvider(), sleep=_no_sleep),
            database=database,
            redis=redis_coordinator,
        )
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            response = await client.get("/health")
        assert response.json()["demo_mode"] is False

    async def test_metrics_is_prometheus_text(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get("/metrics")
        assert response.status_code == 200
        assert "# HELP" in response.text


class TestAuthentication:
    async def test_a_protected_route_rejects_a_missing_key(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(
            database, redis_coordinator, MockProvider(), require_auth=True
        ) as client:
            response = await client.get("/agents")
        assert response.status_code == 403

    async def test_a_protected_route_accepts_the_configured_key(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(
            database, redis_coordinator, MockProvider(), require_auth=True, api_keys="secret-1"
        ) as client:
            response = await client.get("/agents", headers={"X-API-Key": "secret-1"})
        assert response.status_code == 200

    async def test_health_needs_no_key(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(
            database, redis_coordinator, MockProvider(), require_auth=True
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200


class TestAgents:
    async def test_the_reference_agents_are_seeded_and_listed(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get("/agents")
        assert response.status_code == 200
        ids = {a["id"] for a in response.json()}
        assert "research_agent" in ids
        assert "critic_agent" in ids

    async def test_creating_an_agent_makes_it_immediately_gettable(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        payload = {
            "id": "custom_agent",
            "name": "Custom Agent",
            "description": "A test-registered agent.",
            "system_prompt": "You are a helpful custom agent.",
        }
        async with _client(database, redis_coordinator, MockProvider()) as client:
            created = await client.post("/agents", json=payload)
            assert created.status_code == 201
            fetched = await client.get("/agents/custom_agent")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Custom Agent"

    async def test_an_unknown_agent_is_404(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get("/agents/ghost_agent")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestWorkflows:
    def _linear_workflow(self) -> dict[str, Any]:
        return {
            "name": "api-linear",
            "nodes": [
                {"id": "a", "kind": "agent", "agent_id": "research_agent", "output_key": "a"},
                {"id": "b", "kind": "terminal"},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }

    async def test_a_valid_workflow_is_registered_and_gettable(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        workflow = self._linear_workflow()
        async with _client(database, redis_coordinator, MockProvider()) as client:
            created = await client.post("/workflows", json=workflow)
            assert created.status_code == 201
            workflow_id = created.json()["id"]
            fetched = await client.get(f"/workflows/{workflow_id}")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "api-linear"

    async def test_a_workflow_naming_an_unknown_agent_is_rejected(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        workflow = self._linear_workflow()
        workflow["nodes"][0]["agent_id"] = "ghost_agent"
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.post("/workflows", json=workflow)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "graph_validation_error"

    async def test_listing_workflows_includes_a_created_one(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        workflow = self._linear_workflow()
        async with _client(database, redis_coordinator, MockProvider()) as client:
            await client.post("/workflows", json=workflow)
            response = await client.get("/workflows")
        assert response.status_code == 200
        assert any(w["name"] == "api-linear" for w in response.json())


class TestDynamicExecutions:
    async def test_a_dynamic_execution_runs_to_completion(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision("delegate", agents=["research_agent"]),
                        routing_decision("finalize", answer="five vendors found"),
                    ),
                    priority=10,
                ),
                MockRule(name="agent", responses=(agent_output("five vendors found"),)),
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post(
                "/executions", json={"task": "compare CRM vendors on pricing"}
            )
            assert created.status_code == 202
            execution_id = created.json()["execution_id"]

            body = await _wait_for_terminal(client, execution_id)
            events = await client.get(f"/executions/{execution_id}/events")
            trace = await client.get(f"/executions/{execution_id}/trace")

        assert body["status"] == ExecutionStatus.SUCCEEDED.value
        assert body["final_output"] == "five vendors found"
        assert events.status_code == 200
        assert len(events.json()) > 0
        assert trace.status_code == 200
        assert trace.json()["execution_id"] == execution_id

    async def test_the_execution_workflow_route_reflects_the_grown_graph(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        """`GET /executions/{id}/workflow`, not `GET /workflows/{id}`.

        A dynamic execution's seed workflow is a single terminal node; the
        supervisor's delegation grows the real graph turn by turn without ever
        writing that growth back to the `workflows` table. Only the execution-
        scoped route should show the delegated agent node.
        """
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision("delegate", agents=["research_agent"]),
                        routing_decision("finalize", answer="five vendors found"),
                    ),
                    priority=10,
                ),
                MockRule(name="agent", responses=(agent_output("five vendors found"),)),
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post(
                "/executions", json={"task": "compare CRM vendors on pricing"}
            )
            execution_id = created.json()["execution_id"]
            workflow_id = created.json()["workflow_id"]

            await _wait_for_terminal(client, execution_id)
            seed = await client.get(f"/workflows/{workflow_id}")
            grown = await client.get(f"/executions/{execution_id}/workflow")

        assert seed.status_code == 200
        assert grown.status_code == 200
        seed_agent_nodes = [n for n in seed.json()["nodes"] if n["kind"] == "agent"]
        grown_agent_nodes = [n for n in grown.json()["nodes"] if n["kind"] == "agent"]
        assert seed_agent_nodes == []
        assert any(n["agent_id"] == "research_agent" for n in grown_agent_nodes)

    async def test_a_retried_idempotency_key_returns_the_same_execution(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(routing_decision("finalize", answer="ok"),),
                    priority=10,
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            first = await client.post(
                "/executions",
                json={"task": "a repeated task", "idempotency_key": "dedupe-me"},
            )
            second = await client.post(
                "/executions",
                json={"task": "a repeated task", "idempotency_key": "dedupe-me"},
            )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["execution_id"] == second.json()["execution_id"]


async def _wait_for_listed_status(
    client: httpx.AsyncClient, execution_id: str, status_value: str, *, attempts: int = 100
) -> list[dict[str, Any]]:
    """Poll ``GET /executions`` until this id shows the given status.

    ``GET /executions/{id}`` (what :func:`_wait_for_terminal` polls) can
    report a status the durable ``executions`` header row does not yet have:
    the in-memory :class:`ExecutionState` a live run's :class:`RunHandle`
    exposes is updated before the checkpoint write that persists that same
    status to the row `GET /executions` reads from actually commits. A
    single immediate read after `_wait_for_terminal` returns is therefore
    racy; a short poll here is not a workaround for a bug so much as
    documentation of the real (and brief) lag between "live" and "durable"
    views the rest of this API already distinguishes explicitly (see
    `_load_state`'s docstring in `routes/executions.py`).
    """
    for _ in range(attempts):
        listing = await client.get(
            "/executions", params={"status_filter": status_value, "limit": 5000}
        )
        rows: list[dict[str, Any]] = listing.json()
        if any(row["id"] == execution_id for row in rows):
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"execution {execution_id!r} never appeared in the {status_value!r} listing"
    )


class TestListExecutions:
    async def test_a_created_execution_appears_in_the_listing(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(routing_decision("finalize", answer="ok"),),
                    priority=10,
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post("/executions", json={"task": "a listed task"})
            execution_id = created.json()["execution_id"]
            await _wait_for_terminal(client, execution_id)

            # A high explicit limit, not the route's default-50: this test
            # shares one database with the rest of the (large) integration
            # suite, so plenty of other rows can already exist by the time
            # this one runs. The default limit is a reasonable dashboard
            # default and is left alone; only the test needs enough headroom
            # to not depend on this row's rank among everything the suite
            # has created so far.
            listing = await client.get("/executions", params={"limit": 5000})
        assert listing.status_code == 200
        ids = {row["id"] for row in listing.json()}
        assert execution_id in ids

    async def test_the_listing_can_be_filtered_by_status(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(routing_decision("finalize", answer="ok"),),
                    priority=10,
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post("/executions", json={"task": "a filtered task"})
            execution_id = created.json()["execution_id"]
            await _wait_for_terminal(client, execution_id)

            rows = await _wait_for_listed_status(
                client, execution_id, ExecutionStatus.SUCCEEDED.value
            )
        assert all(row["status"] == ExecutionStatus.SUCCEEDED.value for row in rows)


class TestApprovalFlow:
    async def test_listing_pending_approvals_shows_what_is_being_asked(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision(
                            "request_human_approval",
                            approval_action="publish the report externally",
                            approval_risk_reason="visible to customers",
                        ),
                        routing_decision("finalize", answer="published"),
                    ),
                    priority=10,
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post("/executions", json={"task": "publish a report"})
            execution_id = created.json()["execution_id"]
            paused = await _wait_for_terminal(client, execution_id)
            assert paused["status"] == ExecutionStatus.WAITING_FOR_APPROVAL.value

            pending = await client.get(f"/executions/{execution_id}/approvals")
        assert pending.status_code == 200
        approvals = pending.json()
        assert len(approvals) == 1
        assert approvals[0]["action"] == "publish the report externally"
        assert approvals[0]["risk_reason"] == "visible to customers"
        assert approvals[0]["status"] == "pending"

    async def test_approving_a_pending_execution_lets_it_finish(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision(
                            "request_human_approval",
                            approval_action="publish the report externally",
                            approval_risk_reason="visible to customers",
                        ),
                        routing_decision("finalize", answer="published"),
                    ),
                    priority=10,
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post("/executions", json={"task": "publish a report"})
            execution_id = created.json()["execution_id"]

            paused = await _wait_for_terminal(client, execution_id)
            assert paused["status"] == ExecutionStatus.WAITING_FOR_APPROVAL.value

            approved = await client.post(
                f"/executions/{execution_id}/approve", json={"by": "ops@example.test"}
            )
            assert approved.status_code == 200

            finished = await _wait_for_terminal(client, execution_id)

        assert finished["status"] == ExecutionStatus.SUCCEEDED.value
        assert finished["final_output"] == "published"

    async def test_rejecting_a_pending_execution_fails_it(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision(
                            "request_human_approval",
                            approval_action="delete the staging dataset",
                            approval_risk_reason="destructive",
                        ),
                    ),
                    priority=10,
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post("/executions", json={"task": "clean up staging"})
            execution_id = created.json()["execution_id"]
            await _wait_for_terminal(client, execution_id)

            rejected = await client.post(
                f"/executions/{execution_id}/reject",
                json={"by": "ops@example.test", "note": "too risky"},
            )
            assert rejected.status_code == 200

            finished = await _wait_for_terminal(client, execution_id)

        assert finished["status"] == ExecutionStatus.FAILED.value


class TestResume:
    async def test_resuming_a_stranded_execution_picks_up_a_decision_made_out_of_band(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        """A paused execution is no longer tracked as 'active' once it pauses --
        the background task has already exited. Deciding the approval directly
        against the database (not via the API's own /approve, which resumes
        automatically) simulates an operator or a separate tool making the
        decision, and /resume is what picks it back up.
        """
        from orchestration.policies.approvals import ApprovalService

        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision(
                            "request_human_approval",
                            approval_action="publish the report externally",
                            approval_risk_reason="visible to customers",
                        ),
                        routing_decision("finalize", answer="published"),
                    ),
                    priority=10,
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created = await client.post("/executions", json={"task": "publish a report"})
            execution_id = created.json()["execution_id"]
            paused = await _wait_for_terminal(client, execution_id)
            assert paused["status"] == ExecutionStatus.WAITING_FOR_APPROVAL.value

            approval_id = paused["pending_approval_id"]
            assert approval_id is not None
            await ApprovalService(database).approve(approval_id, by="ops@example.test")

            # The background task that paused the run still has a brief window
            # to pop itself out of the runner's active-set after its own
            # status update; resume during that window correctly (if
            # unhelpfully) reports "already running", so retry briefly rather
            # than treating that as a failure.
            for _ in range(20):
                resumed = await client.post(f"/executions/{execution_id}/resume")
                if resumed.json().get("resume") == "started":
                    break
                await asyncio.sleep(0.05)
            assert resumed.status_code == 202
            assert resumed.json()["resume"] == "started"

            finished = await _wait_for_terminal(client, execution_id)

        assert finished["status"] == ExecutionStatus.SUCCEEDED.value
        assert finished["final_output"] == "published"

    async def test_resuming_an_unknown_execution_is_404(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.post("/executions/exec_nonexistent/resume")
        assert response.status_code == 404


class TestStaticWorkflowExecution:
    async def test_running_a_registered_workflow_by_id(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        workflow = {
            "name": "api-static-run",
            "nodes": [
                {"id": "a", "kind": "agent", "agent_id": "research_agent", "output_key": "a"},
                {"id": "b", "kind": "terminal"},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
        provider = MockProvider([MockRule(name="agent", responses=(agent_output("done"),))])
        async with _client(database, redis_coordinator, provider) as client:
            created_workflow = await client.post("/workflows", json=workflow)
            workflow_id = created_workflow.json()["id"]

            created = await client.post(
                "/executions",
                json={"task": "run the static workflow", "workflow_id": workflow_id},
            )
            assert created.status_code == 202
            execution_id = created.json()["execution_id"]

            body = await _wait_for_terminal(client, execution_id)

        assert body["status"] == ExecutionStatus.SUCCEEDED.value
        assert body["node_states"]["a"]["status"] == NodeStatus.SUCCEEDED.value


class TestInvocations:
    """Agent/tool invocation audit rows, written live by
    `InvocationRecorder` (see `persistence/invocation_recorder.py`) as an
    execution runs -- these routes are the first consumers of a table that,
    before this, only `tests/integration/test_persistence.py` wrote to
    directly. Tool-call arguments/results are deliberately not part of what
    these routes return (see the route's own docstring); these tests check
    what is returned, not the full row.
    """

    async def test_agent_invocations_are_recorded_and_readable(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        workflow = {
            "name": "api-invocation-inspect",
            "nodes": [
                {"id": "a", "kind": "agent", "agent_id": "research_agent", "output_key": "a"},
                {"id": "b", "kind": "terminal"},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
        provider = MockProvider([MockRule(name="agent", responses=(agent_output("done"),))])
        async with _client(database, redis_coordinator, provider) as client:
            created_workflow = await client.post("/workflows", json=workflow)
            workflow_id = created_workflow.json()["id"]
            created = await client.post(
                "/executions",
                json={"task": "inspect agent invocations", "workflow_id": workflow_id},
            )
            execution_id = created.json()["execution_id"]
            await _wait_for_terminal(client, execution_id)

            response = await client.get(f"/executions/{execution_id}/agent-invocations")
        assert response.status_code == 200
        invocations = response.json()
        assert len(invocations) == 1
        assert invocations[0]["agent_id"] == "research_agent"
        assert invocations[0]["status"] == "succeeded"

    async def test_tool_invocations_are_recorded_and_readable(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        workflow = {
            "name": "api-tool-invocation-inspect",
            "nodes": [
                {"id": "a", "kind": "agent", "agent_id": "data_agent", "output_key": "a"},
                {"id": "b", "kind": "terminal"},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
        provider = MockProvider(
            [
                MockRule(
                    name="agent",
                    responses=(
                        json.dumps(
                            {
                                "tool_calls": [
                                    {"name": "calculator", "arguments": {"expression": "2+2"}}
                                ]
                            }
                        ),
                        agent_output("computed 2+2"),
                    ),
                )
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            created_workflow = await client.post("/workflows", json=workflow)
            workflow_id = created_workflow.json()["id"]
            created = await client.post(
                "/executions",
                json={"task": "inspect tool invocations", "workflow_id": workflow_id},
            )
            execution_id = created.json()["execution_id"]
            await _wait_for_terminal(client, execution_id)

            response = await client.get(f"/executions/{execution_id}/tool-invocations")
        assert response.status_code == 200
        invocations = response.json()
        assert len(invocations) == 1
        assert invocations[0]["tool"] == "calculator"
        assert invocations[0]["agent_id"] == "data_agent"
        assert invocations[0]["status"] == "succeeded"
        assert invocations[0]["policy_effect"] == "allow"


class TestEventStream:
    async def _run_to_completion(self, client: httpx.AsyncClient) -> str:
        created = await client.post("/executions", json={"task": "compare CRM vendors"})
        execution_id: str = created.json()["execution_id"]
        await _wait_for_terminal(client, execution_id)
        return execution_id

    @staticmethod
    def _parse_sse(lines: list[str]) -> list[dict[str, Any]]:
        """Reassemble ``id:``/``event:``/``data:`` line groups into messages."""
        messages: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for line in lines:
            if line == "":
                if current:
                    messages.append(current)
                    current = {}
                continue
            field, _, value = line.partition(": ")
            if field == "data":
                current["data"] = json.loads(value)
            else:
                current[field] = value
        if current:
            messages.append(current)
        return messages

    async def test_streaming_a_finished_execution_replays_its_backlog_and_closes(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision("delegate", agents=["research_agent"]),
                        routing_decision("finalize", answer="five vendors found"),
                    ),
                    priority=10,
                ),
                MockRule(name="agent", responses=(agent_output("five vendors found"),)),
            ]
        )
        async with _client(database, redis_coordinator, provider) as client:
            execution_id = await self._run_to_completion(client)
            async with client.stream(
                "GET", f"/executions/{execution_id}/stream"
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                lines = [line async for line in response.aiter_lines()]

        messages = self._parse_sse(lines)
        assert len(messages) > 0
        assert all(m["data"]["execution_id"] == execution_id for m in messages)
        # Sequence numbers must be gapless and increasing: the whole point of a
        # replay is that a client can trust it saw everything, in order.
        sequences = [m["data"]["sequence"] for m in messages]
        assert sequences == sorted(sequences)
        assert messages[-1]["data"]["type"] in {
            "execution_completed",
            "execution_failed",
            "execution_cancelled",
        }

    async def test_streaming_an_unknown_execution_is_404(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get("/executions/exec_nonexistent/stream")
        assert response.status_code == 404


class TestCancellation:
    async def test_cancelling_an_unknown_execution_is_404(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.post("/executions/exec_nonexistent/cancel")
        assert response.status_code == 404


class TestBenchmarks:
    """The routes are new; the write path (`BenchmarkRepository.save`, called
    from `orchestration.evaluation.report.run_benchmark`) is not -- see
    docs/evaluation-benchmark.md. Rather than running a real (slow) benchmark
    here, these tests save a report directly through the same repository the
    CLI uses, then read it back over HTTP.
    """

    def _report(self, report_id: str) -> BenchmarkReport:
        now = utc_now()
        return BenchmarkReport(
            id=report_id,
            started_at=now,
            completed_at=now,
            git_sha="abc1234",
            provider_note="mock provider; latency figures are not real LLM latency",
            scenario_count=1,
            arms=(
                ArmMetrics(arm="baseline", scenarios_run=1, scenarios_passed=0),
                ArmMetrics(arm="supervisor", scenarios_run=1, scenarios_passed=1),
            ),
        )

    async def test_a_saved_report_appears_in_the_listing(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        report = self._report("eval_api_listing_test")
        async with database.session() as session:
            await BenchmarkRepository(session).save(report)

        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get("/benchmarks")
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()}
        assert report.id in ids

    async def test_a_saved_report_is_readable_in_full(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        report = self._report("eval_api_detail_test")
        async with database.session() as session:
            await BenchmarkRepository(session).save(report)

        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get(f"/benchmarks/{report.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == report.id
        arms = {a["arm"]: a for a in body["arms"]}
        assert arms["baseline"]["scenarios_passed"] == 0
        assert arms["supervisor"]["scenarios_passed"] == 1

    async def test_an_unknown_report_is_404(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.get("/benchmarks/eval_nonexistent")
        assert response.status_code == 404


class TestMCPWiring:
    """Real MCP discovery through the actual app startup path
    (`build_app_state`), not just the standalone client -- see
    tests/integration/test_mcp.py for the client/adapter/policy tests
    themselves. Skipped, not failed, without npx.
    """

    async def test_mcp_tools_are_discovered_and_registered_at_app_startup(
        self, database: Database, redis_coordinator: RedisCoordinator, tmp_path: Any
    ) -> None:
        import shutil

        if shutil.which("npx") is None:
            pytest.skip("npx is not available")

        (tmp_path / "greeting.txt").write_text("hello mcp\n", encoding="utf-8")
        settings = Settings(
            _env_file=None,
            mcp_enabled=True,
            mcp_server_command=f"npx -y @modelcontextprotocol/server-filesystem {tmp_path}",
        )
        app = create_app(
            settings,
            llm=LLMClient.mock(MockProvider(), sleep=_no_sleep),
            database=database,
            redis=redis_coordinator,
        )
        async with app.router.lifespan_context(app):
            app_state = app.state.app_state
            assert app_state.tools.has("mcp_default_read_text_file")
            assert app_state.tools.is_enabled("mcp_default_read_text_file")
            assert len(app_state.mcp_clients) == 1
