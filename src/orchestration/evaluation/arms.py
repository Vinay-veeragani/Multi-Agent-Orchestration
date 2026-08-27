"""The four benchmark arms: an ablation over the engine's own capabilities.

Each arm answers one question about what a capability is actually worth,
holding everything else fixed:

``baseline``
    No supervisor at all. The single best-matching agent (by the same
    deterministic keyword/capability scoring :class:`~orchestration.
    supervisor.heuristic.HeuristicRouter` uses for its fallback) runs once,
    with no retry and no parallelism. This is "what routing intelligence,
    retries, and fan-out are worth *relative to*" -- not a strawman, since the
    matching itself is the engine's own tested logic, just without an LLM
    supervisor deciding what to do with it.

``supervisor``
    The full LLM-driven :class:`~orchestration.runtime.orchestrator.
    ExecutionOrchestrator`, but with retry disabled (``max_attempts=1``) and
    parallelism disabled (``max_concurrent_nodes=1``, so even a
    ``parallel_delegate`` decision's nodes run one at a time).

``supervisor-retry``
    As ``supervisor``, with the engine's default retry policy restored.

``supervisor-parallel``
    As ``supervisor-retry``, with parallelism restored too -- the full engine,
    every capability enabled. This is the configuration a real deployment
    actually runs.

Comparing a scenario's pass/fail across these four is the point: a
retry-recovery scenario is *expected* to fail under ``baseline``/``supervisor``
and pass under the two retry-enabled arms, and that gap is exactly what the
benchmark is measuring.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestration.domain.retry import DEFAULT_RETRY_POLICY, RetryPolicy


@dataclass(frozen=True, slots=True)
class Arm:
    """One benchmark configuration."""

    name: str
    #: ``False`` for the baseline, which never reaches the LLM-driven
    #: orchestrator at all.
    uses_supervisor: bool
    retry_policy: RetryPolicy
    max_concurrent_nodes: int


#: The single-attempt policy every non-retry arm forces onto every node.
NO_RETRY = RetryPolicy(max_attempts=1)

#: Names use ``-`` rather than ``+``: both ``ScenarioResult.arm`` and
#: ``ArmMetrics.arm`` are domain ``Slug`` fields (``^[a-z0-9][a-z0-9_-]*$``),
#: which forbids ``+``.
ARMS: tuple[Arm, ...] = (
    Arm(name="baseline", uses_supervisor=False, retry_policy=NO_RETRY, max_concurrent_nodes=1),
    Arm(name="supervisor", uses_supervisor=True, retry_policy=NO_RETRY, max_concurrent_nodes=1),
    Arm(
        name="supervisor-retry",
        uses_supervisor=True,
        retry_policy=DEFAULT_RETRY_POLICY,
        max_concurrent_nodes=1,
    ),
    Arm(
        name="supervisor-parallel",
        uses_supervisor=True,
        retry_policy=DEFAULT_RETRY_POLICY,
        max_concurrent_nodes=8,
    ),
)
