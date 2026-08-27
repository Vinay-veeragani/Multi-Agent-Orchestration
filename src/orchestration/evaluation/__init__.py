"""Deterministic evaluation: scenarios, judging, and the 4-arm benchmark.

See :mod:`orchestration.domain.evaluation` for the result types this package
produces, and :mod:`orchestration.evaluation.report` for the entry point that
runs the whole benchmark and returns a :class:`~orchestration.domain.
evaluation.BenchmarkReport`.
"""

from __future__ import annotations

from orchestration.evaluation.arms import ARMS, Arm
from orchestration.evaluation.harness import run_scenario
from orchestration.evaluation.judge import judge
from orchestration.evaluation.report import run_benchmark
from orchestration.evaluation.scenarios import ALL_SCENARIOS

__all__ = [
    "ALL_SCENARIOS",
    "ARMS",
    "Arm",
    "judge",
    "run_benchmark",
    "run_scenario",
]
