"""Runs the full benchmark: every scenario, under every arm, aggregated.

:func:`run_benchmark` is the single entry point the CLI script
(``benchmarks/run_benchmark.py``) and the tests call. It fans scenario/arm
pairs out with bounded concurrency, grades each with
:func:`~orchestration.evaluation.judge.judge` via
:func:`~orchestration.evaluation.harness.run_scenario`, and folds the results
into a :class:`~orchestration.domain.evaluation.BenchmarkReport` -- optionally
persisted via :class:`~orchestration.persistence.repositories.
BenchmarkRepository` so a run is auditable later, not just printed once.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.base import utc_now
from orchestration.domain.evaluation import (
    ArmMetrics,
    BenchmarkReport,
    BenchmarkScenario,
    ScenarioResult,
    summarise_arm,
)
from orchestration.evaluation.arms import ARMS, Arm
from orchestration.evaluation.harness import run_scenario
from orchestration.evaluation.scenarios import ALL_SCENARIOS
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import BenchmarkRepository

#: States plainly, in every report, that latency figures are not real
#: provider latency -- the mock provider's synthetic delay defaults to zero.
PROVIDER_NOTE = (
    "All scenarios run against MockProvider with zero synthetic latency. "
    "Timing figures measure engine wall-clock (routing, scheduling, "
    "checkpoint plumbing), not real LLM provider latency."
)


def _git_sha() -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, resolved executable, no shell
            [git, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _environment() -> dict[str, str]:
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
    }


async def run_benchmark(
    *,
    scenarios: Sequence[BenchmarkScenario] = ALL_SCENARIOS,
    arms: Sequence[Arm] = ARMS,
    database: Database,
    redis: RedisCoordinator,
    sandbox_root: Path | None = None,
    concurrency: int = 8,
    persist: bool = True,
) -> BenchmarkReport:
    """Run every ``(scenario, arm)`` pair and return the aggregate report."""
    started_at = utc_now()
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(scenario: BenchmarkScenario, arm: Arm) -> ScenarioResult:
        async with semaphore:
            return await run_scenario(
                scenario, arm, database=database, redis=redis, sandbox_root=sandbox_root
            )

    pairs = [(scenario, arm) for scenario in scenarios for arm in arms]
    results = await asyncio.gather(*(_run_one(scenario, arm) for scenario, arm in pairs))
    completed_at = utc_now()

    arm_metrics: tuple[ArmMetrics, ...] = tuple(
        summarise_arm(arm.name, [r for r in results if r.arm == arm.name]) for arm in arms
    )

    report = BenchmarkReport(
        started_at=started_at,
        completed_at=completed_at,
        git_sha=_git_sha(),
        environment=_environment(),
        provider_note=PROVIDER_NOTE,
        scenario_count=len(scenarios),
        arms=arm_metrics,
        results=tuple(results),
    )

    if persist:
        async with database.session() as session:
            await BenchmarkRepository(session).save(report)

    return report
