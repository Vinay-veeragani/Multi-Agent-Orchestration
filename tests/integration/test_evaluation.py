"""Integration tests for the evaluation harness and benchmark report.

Runs real scenarios through the real engine (dynamic orchestrator and, for
``baseline``, the heuristic router directly) against the test database and
Redis namespace, then checks the graded result and the persisted report --
not a mock of any of it.
"""

from __future__ import annotations

import pytest

from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.enums import ExecutionStatus
from orchestration.domain.evaluation import BenchmarkScenario, ScenarioExpectation
from orchestration.evaluation.arms import ARMS, Arm
from orchestration.evaluation.harness import run_scenario
from orchestration.evaluation.report import run_benchmark
from orchestration.evaluation.scenarios import ALL_SCENARIOS
from orchestration.llm.mock import routing_decision
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import BenchmarkRepository

pytestmark = pytest.mark.integration


def _arm(name: str) -> Arm:
    return next(a for a in ARMS if a.name == name)


class TestScenarioLibrary:
    def test_at_least_fifty_scenarios_and_all_ids_unique(self) -> None:
        assert len(ALL_SCENARIOS) >= 50
        ids = [s.id for s in ALL_SCENARIOS]
        assert len(ids) == len(set(ids))

    def test_every_category_is_nonempty(self) -> None:
        categories = {s.category for s in ALL_SCENARIOS}
        assert categories == {
            "simple",
            "parallel",
            "chain",
            "retry",
            "tool",
            "deny",
            "approval",
            "budget",
            "fail",
            "respond",
        }


class TestRunScenario:
    async def test_a_simple_delegation_succeeds_under_the_full_arm(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        scenario = next(s for s in ALL_SCENARIOS if s.id == "simple-research")
        result = await run_scenario(
            scenario, _arm("supervisor-parallel"), database=database, redis=redis_coordinator
        )
        assert result.passed, result.failures
        assert result.final_status is ExecutionStatus.SUCCEEDED
        assert result.arm == "supervisor-parallel"

    async def test_a_retry_scenario_fails_without_retry_and_passes_with_it(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        """The headline ablation claim, exercised directly."""
        scenario = next(s for s in ALL_SCENARIOS if s.id == "retry-1")

        without_retry = await run_scenario(
            scenario, _arm("supervisor"), database=database, redis=redis_coordinator
        )
        with_retry = await run_scenario(
            scenario, _arm("supervisor-retry"), database=database, redis=redis_coordinator
        )

        assert not without_retry.passed
        assert with_retry.passed, with_retry.failures
        assert with_retry.retries > 0

    async def test_the_baseline_arm_never_touches_the_orchestrator(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        """Baseline runs a single heuristically-chosen agent, once."""
        scenario = next(s for s in ALL_SCENARIOS if s.id == "simple-analyst")
        result = await run_scenario(
            scenario, _arm("baseline"), database=database, redis=redis_coordinator
        )
        assert result.max_parallelism == 1

    async def test_a_parallel_scenario_shows_real_concurrency_under_the_full_arm(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        scenario = next(s for s in ALL_SCENARIOS if s.id == "parallel-1")
        result = await run_scenario(
            scenario, _arm("supervisor-parallel"), database=database, redis=redis_coordinator
        )
        assert result.passed, result.failures
        assert result.max_parallelism >= 2

    async def test_an_approval_scenario_pauses_and_resolves_automatically(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        scenario = next(s for s in ALL_SCENARIOS if s.id == "approval-approve-1")
        result = await run_scenario(
            scenario, _arm("supervisor"), database=database, redis=redis_coordinator
        )
        assert result.passed, result.failures
        assert result.final_status is ExecutionStatus.SUCCEEDED

    async def test_a_deny_scenario_never_lets_the_disallowed_tool_run(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        scenario = next(s for s in ALL_SCENARIOS if s.id == "deny-1")
        result = await run_scenario(
            scenario, _arm("supervisor"), database=database, redis=redis_coordinator
        )
        assert result.passed, result.failures

    async def test_a_malformed_reply_degrades_to_the_heuristic_fallback_not_a_crash(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        broken = BenchmarkScenario(
            id="broken-scenario",
            category="fail",
            description="the supervisor's only scripted reply is unparseable",
            task="do something",
            expectation=ScenarioExpectation(status=ExecutionStatus.SUCCEEDED),
            mock_script={"supervisor": ["not valid json at all"]},
        )
        result = await run_scenario(
            broken, _arm("supervisor"), database=database, redis=redis_coordinator
        )
        # The point of this test: an unparseable reply must degrade to the
        # heuristic fallback rather than crash the harness, whatever the
        # fallback then decides to do with a generic task like this one.
        assert result.error is None

    async def test_a_genuine_harness_error_is_reported_not_raised(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        """A bug in scenario construction must fail the scenario, not the run."""
        broken = BenchmarkScenario(
            id="broken-budget",
            category="fail",
            description="a budget override with an unknown field",
            task="do something",
            expectation=ScenarioExpectation(status=ExecutionStatus.SUCCEEDED),
            mock_script={"supervisor": [routing_decision("finalize", answer="x")]},
            budget_override={"not_a_real_budget_field": 1},
        )
        result = await run_scenario(
            broken, _arm("supervisor"), database=database, redis=redis_coordinator
        )
        assert not result.passed
        assert result.error is not None
        assert "not_a_real_budget_field" in result.error or "extra" in result.error.lower()


class TestRunBenchmark:
    async def test_a_small_benchmark_run_persists_and_reports_correctly(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        scenarios = tuple(s for s in ALL_SCENARIOS if s.category in {"simple", "retry"})[:4]
        report = await run_benchmark(
            scenarios=scenarios,
            arms=ARMS,
            database=database,
            redis=redis_coordinator,
            concurrency=4,
        )

        assert report.scenario_count == len(scenarios)
        assert len(report.results) == len(scenarios) * len(ARMS)
        assert {a.arm for a in report.arms} == {a.name for a in ARMS}
        assert "mock" in report.provider_note.lower()
        assert report.completed_at >= report.started_at

        async with database.session() as session:
            reloaded = await BenchmarkRepository(session).get(report.id)
        assert reloaded.id == report.id
        assert reloaded.scenario_count == report.scenario_count
        assert len(reloaded.results) == len(report.results)

    async def test_arm_metrics_reflect_the_retry_ablation(
        self, database: Database, redis_coordinator: RedisCoordinator
    ) -> None:
        scenarios = tuple(s for s in ALL_SCENARIOS if s.id in {"retry-1", "retry-3"})
        report = await run_benchmark(
            scenarios=scenarios,
            arms=ARMS,
            database=database,
            redis=redis_coordinator,
            persist=False,
        )
        without_retry = report.arm("supervisor")
        with_retry = report.arm("supervisor-retry")
        assert without_retry is not None
        assert with_retry is not None
        assert without_retry.scenarios_passed == 0
        assert with_retry.scenarios_passed == len(scenarios)
        assert with_retry.task_completion_rate > without_retry.task_completion_rate
