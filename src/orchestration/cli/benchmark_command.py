"""Shared implementation behind ``orchestrator benchmark`` and the standalone
``benchmarks/run_benchmark.py`` script.

One function, :func:`run_benchmark_command`, so the two entry points can never
drift: the CLI calls it directly, and the standalone script (kept for anyone
who wants to run a benchmark without installing the package as a console
script) is a thin argument-parsing wrapper around the same call.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from orchestration.config import get_settings
from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.evaluation import BenchmarkReport, BenchmarkScenario
from orchestration.evaluation.arms import ARMS
from orchestration.evaluation.report import run_benchmark
from orchestration.evaluation.scenarios import ALL_SCENARIOS
from orchestration.persistence.database import Database

#: Default output directory, so a run always leaves a trace even when no
#: output path is given. Relative to the current working directory rather
#: than this file's location: a non-editable install (a container image,
#: notably) puts this module under site-packages, nowhere near a
#: `benchmarks/` tree, whereas `orchestrator benchmark` is always invoked
#: from somewhere that has (or can create) one -- the repo root in
#: development, `/app` in the reference Docker image.
DEFAULT_RESULTS_DIR = Path.cwd() / "benchmarks" / "results"


def select_scenarios(
    *, categories: Sequence[str], scenario_ids: Sequence[str]
) -> tuple[BenchmarkScenario, ...]:
    scenarios: tuple[BenchmarkScenario, ...] = ALL_SCENARIOS
    if categories:
        wanted = set(categories)
        scenarios = tuple(s for s in scenarios if s.category in wanted)
    if scenario_ids:
        wanted_ids = set(scenario_ids)
        scenarios = tuple(s for s in scenarios if s.id in wanted_ids)
    return scenarios


def print_report(console: Console, report: BenchmarkReport) -> None:
    console.print(f"\n{report.provider_note}\n")
    table = Table("arm", "passed", "completion", "routing acc.", "avg latency", "tokens", "cost")
    for arm in report.arms:
        table.add_row(
            arm.arm,
            f"{arm.scenarios_passed}/{arm.scenarios_run}",
            f"{arm.task_completion_rate:.1%}",
            f"{arm.routing_accuracy:.1%}" if arm.routing_accuracy is not None else "-",
            f"{arm.avg_latency_seconds * 1000:.1f}ms",
            str(arm.total_tokens),
            f"${arm.total_cost_usd:.4f}",
        )
    console.print(table)

    failed = [r for r in report.results if not r.passed]
    if failed:
        console.print(f"\n[bold red]{len(failed)} scenario/arm failures:[/bold red]")
        for r in failed:
            detail = "; ".join(r.failures) or r.error or ""
            console.print(f"  {r.scenario_id:24s} {r.arm:20s} {escape(detail)}")


async def run_benchmark_command(
    *,
    categories: Sequence[str],
    scenario_ids: Sequence[str],
    test_db: bool,
    concurrency: int,
    output: Path | None,
    console: Console,
    persist: bool = True,
) -> None:
    scenarios = select_scenarios(categories=categories, scenario_ids=scenario_ids)
    if not scenarios:
        console.print("[bold red]No scenarios matched the given filters.[/bold red]")
        sys.exit(1)

    settings = get_settings()
    pg_dsn = settings.pg_test_dsn if test_db else settings.pg_dsn
    redis_url = settings.redis_test_url if test_db else settings.redis_url

    database = Database(pg_dsn, settings=settings)
    redis = RedisCoordinator(redis_url, settings=settings)

    console.print(f"Running {len(scenarios)} scenario(s) across {len(ARMS)} arms...")
    try:
        report = await run_benchmark(
            scenarios=scenarios,
            arms=ARMS,
            database=database,
            redis=redis,
            concurrency=concurrency,
            persist=persist,
        )
    finally:
        await database.aclose()
        await redis.aclose()

    print_report(console, report)

    destination = output
    if destination is None:
        DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        destination = DEFAULT_RESULTS_DIR / f"{report.id}.json"
    destination.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    console.print(f"\nFull report written to {destination}")
