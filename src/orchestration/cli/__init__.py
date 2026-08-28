"""The ``orchestrator`` command-line tool.

Most subcommands are a thin HTTP client over the running API
(:mod:`orchestration.api`) -- the same way an operator would otherwise use
``curl``, just with readable output and sensible defaults. ``benchmark`` is
the one exception: it drives :mod:`orchestration.evaluation` directly, since
running the benchmark is a database-and-engine operation with no need for a
live API process in between.
"""

from __future__ import annotations
