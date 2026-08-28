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

A thin argument-parsing wrapper around
:func:`orchestration.cli.benchmark_command.run_benchmark_command` -- the same
function ``orchestrator benchmark`` calls -- kept as a standalone entry point
for anyone who wants to run a benchmark without going through the installed
console script.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console

from orchestration.cli.benchmark_command import run_benchmark_command


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        default=[],
        help="Only run scenarios in this category (repeatable).",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenario_ids",
        default=[],
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
        "--output", type=Path, default=None, help="Where to write the JSON report."
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(
        run_benchmark_command(
            categories=args.categories,
            scenario_ids=args.scenario_ids,
            test_db=args.test_db,
            concurrency=args.concurrency,
            output=args.output,
            console=Console(),
            persist=not args.no_persist,
        )
    )


if __name__ == "__main__":
    main()
