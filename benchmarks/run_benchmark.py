#!/usr/bin/env python
"""Run the evaluation benchmark and print (and persist) the results.

Usage::

    python benchmarks/run_benchmark.py
    python benchmarks/run_benchmark.py --category retry --category approval
    python benchmarks/run_benchmark.py --test-db --no-persist
    python benchmarks/run_benchmark.py --output benchmarks/results/run.json

Connects to PostgreSQL and Redis using the same ``Settings`` (and therefore
the same ``ORCH_*`` environment variables) the rest of the engine uses.
``--test-db`` points at the test database/namespace instead, for a quick
local run that does not touch anything a real deployment might read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tabulate import tabulate

from orchestration.config import get_settings
from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.evaluation import BenchmarkScenario
from orchestration.evaluation.arms import ARMS
from orchestration.evaluation.report import run_benchmark
from orchestration.evaluation.scenarios import ALL_SCENARIOS
from orchestration.persistence.database import Database

#: Repo-relative default output directory, so a run always leaves a trace
#: even when --output is omitted.
_DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        help="Only run scenarios in this category (repeatable).",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenario_ids",
        help="Only run this specific scenario id (repeatable).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=8, help="Max scenario/arm pairs run at once."
    )
    parser.add_argument(
        "--test-db",
        action="store_true",
        help="Use the test database/Redis namespace instead of the configured production ones.",
    )
    parser.add_argument(
        "--no-persist", action="store_true", help="Do not write the report to the database."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Where to write the JSON report (default: a file under {_DEFAULT_RESULTS_DIR}).",
    )
    return parser.parse_args()


def _select_scenarios(args: argparse.Namespace) -> tuple[BenchmarkScenario, ...]:
    scenarios = ALL_SCENARIOS
    if args.categories:
        wanted = set(args.categories)
        scenarios = tuple(s for s in scenarios if s.category in wanted)
    if args.scenario_ids:
        wanted_ids = set(args.scenario_ids)
        scenarios = tuple(s for s in scenarios if s.id in wanted_ids)
    if not scenarios:
        print("No scenarios matched the given filters.", file=sys.stderr)
        sys.exit(1)
    return scenarios


def _print_summary(report) -> None:  # type: ignore[no-untyped-def]
    print(f"\n{report.provider_note}\n")
    rows = [
        [
            arm.arm,
            f"{arm.scenarios_passed}/{arm.scenarios_run}",
            f"{arm.task_completion_rate:.1%}",
            f"{arm.routing_accuracy:.1%}" if arm.routing_accuracy is not None else "-",
            f"{arm.avg_latency_seconds * 1000:.1f}ms",
            f"{arm.p95_latency_seconds * 1000:.1f}ms",
            arm.total_tokens,
            f"${arm.total_cost_usd:.4f}",
        ]
        for arm in report.arms
    ]
    print(
        tabulate(
            rows,
            headers=[
                "arm",
                "passed",
                "completion",
                "routing acc.",
                "avg latency",
                "p95 latency",
                "tokens",
                "cost",
            ],
            tablefmt="github",
        )
    )
    failed = [r for r in report.results if not r.passed]
    if failed:
        print(f"\n{len(failed)} scenario/arm failures:")
        for r in failed:
            print(f"  {r.scenario_id:24s} {r.arm:20s} {'; '.join(r.failures) or r.error or ''}")


async def _main() -> None:
    args = _parse_args()
    scenarios = _select_scenarios(args)
    settings = get_settings()

    pg_dsn = settings.pg_test_dsn if args.test_db else settings.pg_dsn
    redis_url = settings.redis_test_url if args.test_db else settings.redis_url

    database = Database(pg_dsn, settings=settings)
    redis = RedisCoordinator(redis_url, settings=settings)

    print(f"Running {len(scenarios)} scenario(s) across {len(ARMS)} arms...")
    try:
        report = await run_benchmark(
            scenarios=scenarios,
            arms=ARMS,
            database=database,
            redis=redis,
            concurrency=args.concurrency,
            persist=not args.no_persist,
        )
    finally:
        await database.aclose()
        await redis.aclose()

    _print_summary(report)

    output = args.output
    if output is None:
        _DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        output = _DEFAULT_RESULTS_DIR / f"{report.id}.json"
    output.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    print(f"\nFull report written to {output}")


if __name__ == "__main__":
    asyncio.run(_main())
