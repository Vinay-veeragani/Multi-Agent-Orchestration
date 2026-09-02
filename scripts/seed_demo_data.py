#!/usr/bin/env python
"""Populate the database with real demo data for a fresh setup.

Runs the three narrated demos under ``examples/`` (each a real execution
against real PostgreSQL/Redis, driven by ``MockProvider`` -- see this
project's "no LLM key required" design) plus a small benchmark slice, so a
freshly migrated database has something to look at in the web UI
(``/``, ``/workflows``, ``/benchmarks``) instead of three empty pages.

This does not create anything the individual scripts don't already create
on their own -- it just runs all of them in one command for convenience.

Usage::

    python scripts/seed_demo_data.py
    python scripts/seed_demo_data.py --test-db
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(description: str, argv: list[str]) -> None:
    print(f"\n=== {description} ===")
    # Fixed argv (sys.executable + this file's own hardcoded arguments), not
    # user input -- same justification as report.py's git subprocess call.
    result = subprocess.run([sys.executable, *argv], cwd=ROOT, check=False)  # noqa: S603
    if result.returncode != 0:
        raise SystemExit(f"{description} failed (exit code {result.returncode})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-db",
        action="store_true",
        help="Seed the disposable test database/Redis namespace instead of the configured ones.",
    )
    args = parser.parse_args()
    db_flag = ["--test-db"] if args.test_db else []

    # Benchmark first: it alone creates 60-80 execution rows (every scenario
    # x arm is its own execution). Run after the narrated demos, those rows
    # would be the most recent thing in the database and bury the three
    # actual demo executions below the dashboard's default view -- the
    # opposite of what "give the UI something to look at" means here.
    _run(
        "Benchmark slice (simple, retry, approval)",
        [
            "-m",
            "orchestration.cli.main",
            "benchmark",
            "--category",
            "simple",
            "--category",
            "retry",
            "--category",
            "approval",
            *db_flag,
        ],
    )
    _run("Demo: competitive intelligence", ["examples/research/run.py", *db_flag])
    _run("Demo: data analysis", ["examples/data_analysis/run.py", *db_flag])
    _run("Demo: human approval", ["examples/human_approval/run.py", *db_flag])

    print("\nSeeded. Start the API and the web UI to look at the result:")
    print("  uvicorn orchestration.api.app:create_app --factory --reload")
    print("  cd frontend && npm run dev")


if __name__ == "__main__":
    main()
