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
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from orchestration.api.app import create_app
from orchestration.config import Settings
from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.enums import ExecutionStatus, NodeStatus
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import MockProvider, MockRule, agent_output, routing_decision
from orchestration.persistence.database import Database

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
) -> AsyncIterator[httpx.AsyncClient]:
    """A live app, over an in-process ASGI transport, torn down on exit."""
    settings = Settings(api_require_auth=require_auth, api_keys=api_keys)
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
        assert body == {"status": "ok", "database": True, "redis": True}

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


class TestApprovalFlow:
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


class TestCancellation:
    async def test_cancelling_an_unknown_execution_is_404(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        async with _client(database, redis_coordinator, MockProvider()) as client:
            response = await client.post("/executions/exec_nonexistent/cancel")
        assert response.status_code == 404
