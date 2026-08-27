"""HTTP API: the operator-facing surface over the orchestration engine.

Every route is a thin translation layer -- it validates a request, calls into
the same registries, repositories and execution machinery the CLI and the test
suite use, and translates the result (or an :class:`~orchestration.errors.
OrchestrationError`) into an HTTP response. No engine behaviour is duplicated
here.
"""

from __future__ import annotations

from orchestration.api.app import create_app

__all__ = ["create_app"]
