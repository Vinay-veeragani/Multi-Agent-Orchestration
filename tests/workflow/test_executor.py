"""Integration tests for the workflow executor.

These drive the real scheduler with real registries, a real policy engine and a
real budget meter -- only the LLM is mocked. What is being verified is the
behaviour the design claims: implicit parallelism, joins that tolerate partial
results, conditional branches that skip rather than stall, retries that recover,
cancellation that interrupts, and budgets that stop a run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from orchestration.domain.budget import Budget
from orchestration.domain.enums import (
    CheckpointReason,
    EventType,
    ExecutionStatus,
    JoinPolicy,
    NodeKind,
    NodeStatus,
)
from orchestration.domain.workflow import (
    Condition,
    NodeCondition,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)
from orchestration.llm.mock import Fault, MockProvider, MockRule, agent_output
from tests.workflow.conftest import Harness

pytestmark = pytest.mark.integration


HarnessFactory = Callable[..., Harness]


def _agent_node(node_id: str, agent_id: str, **kwargs: object) -> WorkflowNode:
    return WorkflowNode.model_validate(
        {
            "id": node_id,
            "kind": NodeKind.AGENT,
            "agent_id": agent_id,
            "output_key": node_id,
            **kwargs,
        }
    )


# ---------------------------------------------------------------------------
# Sequential
# ---------------------------------------------------------------------------


class TestSequentialExecution:
    async def test_linear_chain_runs_in_order(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    match_request_key=":research_agent:",
                    responses=(agent_output("found"),),
                ),
                MockRule(
                    name="analyst",
                    match_request_key=":analyst_agent:",
                    responses=(agent_output("analysed"),),
                ),
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="linear",
            nodes=(
                _agent_node("a", "research_agent"),
                _agent_node("b", "analyst_agent"),
                WorkflowNode(id="end", kind=NodeKind.TERMINAL),
            ),
            edges=(
                WorkflowEdge(source="a", target="b"),
                WorkflowEdge(source="b", target="end"),
            ),
        )
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.succeeded
        assert result.step_groups == [("a",), ("b",), ("end",)]
        assert result.max_parallelism == 1
        assert state.succeeded_node_ids() == {"a", "b", "end"}

    async def test_downstream_receives_upstream_output(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="research",
                    match_system="research",
                    responses=(agent_output("Salesforce and HubSpot lead"),),
                ),
                MockRule(
                    name="analyst",
                    match_request_key=":analyst_agent:",
                    responses=(agent_output("done"),),
                ),
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="chain",
            nodes=(_agent_node("a", "research_agent"), _agent_node("b", "analyst_agent")),
            edges=(WorkflowEdge(source="a", target="b"),),
        )
        await harness.executor(workflow).run(harness.state(workflow))
        analyst_call = provider.calls_for_rule("analyst")[0]
        assert "Salesforce and HubSpot lead" in analyst_call.user_preview

    async def test_final_output_is_recorded(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("the answer"),))])
        harness = harness_factory(provider)
        workflow = Workflow(
            name="single",
            nodes=(
                _agent_node("a", "analyst_agent"),
                WorkflowNode(id="end", kind=NodeKind.TERMINAL),
            ),
            edges=(WorkflowEdge(source="a", target="end"),),
        )
        state = harness.state(workflow)
        await harness.executor(workflow).run(state)
        assert state.final_output == "the answer"

    async def test_input_template_is_rendered(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("ok"),))])
        harness = harness_factory(provider)
        workflow = Workflow(
            name="templated",
            nodes=(
                _agent_node(
                    "a",
                    "analyst_agent",
                    input_template="Analyse this task: {task.description}",
                ),
            ),
            variables={},
        )
        await harness.executor(workflow).run(harness.state(workflow, "compare CRM vendors"))
        assert "compare CRM vendors" in provider.calls[0].user_preview


# ---------------------------------------------------------------------------
# Parallel
# ---------------------------------------------------------------------------


class TestParallelExecution:
    async def test_fan_out_runs_concurrently(self, harness_factory: HarnessFactory) -> None:
        """Parallelism is implicit: three ready nodes means three run."""
        provider = MockProvider(
            [
                MockRule(
                    name="branch",
                    responses=(agent_output("branch result"),),
                    latency_seconds=0.10,
                )
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="fanout",
            nodes=(
                _agent_node("research", "research_agent"),
                _agent_node("pricing", "pricing_agent"),
                _agent_node("features", "feature_agent"),
                WorkflowNode(id="join", kind=NodeKind.JOIN, join_policy=JoinPolicy.ALL),
            ),
            edges=(
                WorkflowEdge(source="research", target="join"),
                WorkflowEdge(source="pricing", target="join"),
                WorkflowEdge(source="features", target="join"),
            ),
            entry_nodes=("research", "pricing", "features"),
        )
        started = time.perf_counter()
        result = await harness.executor(workflow).run(harness.state(workflow))
        elapsed = time.perf_counter() - started

        assert result.succeeded
        assert result.max_parallelism == 3
        # Three 100ms branches serially would be ~0.30s; concurrently ~0.10s.
        assert elapsed < 0.25, f"branches did not overlap (took {elapsed:.3f}s)"

    async def test_join_aggregates_all_branches(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider([MockRule(name="b", responses=(agent_output("result"),))])
        harness = harness_factory(provider)
        workflow = _diamond(JoinPolicy.ALL)
        state = harness.state(workflow)
        await harness.executor(workflow).run(state)

        joined = state.agent_outputs["join"]["joined"]
        assert set(joined) == {"left", "right"}
        assert state.agent_outputs["join"]["partial"] is False

    async def test_concurrency_limit_is_respected(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider(
            [MockRule(name="b", responses=(agent_output("r"),), latency_seconds=0.05)]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="wide",
            nodes=tuple(_agent_node(f"n{i}", "research_agent") for i in range(6)),
            entry_nodes=tuple(f"n{i}" for i in range(6)),
        )
        started = time.perf_counter()
        result = await harness.executor(workflow, max_concurrent_nodes=2).run(
            harness.state(workflow)
        )
        elapsed = time.perf_counter() - started

        assert result.succeeded
        # Six 50ms nodes at concurrency 2 is three waves: ~0.15s, not ~0.05s.
        assert elapsed >= 0.12, f"the concurrency cap was not applied ({elapsed:.3f}s)"

    async def test_ready_set_ordering_is_deterministic(
        self, harness_factory: HarnessFactory
    ) -> None:
        """Completion order varies with concurrency; scheduling order must not."""
        workflow = Workflow(
            name="wide",
            nodes=tuple(_agent_node(f"n{i}", "research_agent") for i in range(4)),
            entry_nodes=tuple(f"n{i}" for i in range(4)),
        )
        groups = []
        for _ in range(3):
            provider = MockProvider([MockRule(name="b", responses=(agent_output("r"),))])
            harness = harness_factory(provider)
            result = await harness.executor(workflow).run(harness.state(workflow))
            groups.append(result.step_groups)
        assert groups[0] == groups[1] == groups[2]


def _diamond(policy: JoinPolicy, *, quorum: int | None = None) -> Workflow:
    join: dict[str, object] = {"id": "join", "kind": NodeKind.JOIN, "join_policy": policy}
    if quorum is not None:
        join["quorum"] = quorum
    return Workflow(
        name="diamond",
        nodes=(
            _agent_node("left", "research_agent"),
            _agent_node("right", "pricing_agent"),
            WorkflowNode.model_validate(join),
            _agent_node("analyst", "analyst_agent"),
        ),
        edges=(
            WorkflowEdge(source="left", target="join"),
            WorkflowEdge(source="right", target="join"),
            WorkflowEdge(source="join", target="analyst"),
        ),
        entry_nodes=("left", "right"),
    )


# ---------------------------------------------------------------------------
# Join policies and partial results
# ---------------------------------------------------------------------------


class TestJoinPolicies:
    async def test_all_settled_tolerates_a_failed_branch(
        self, harness_factory: HarnessFactory
    ) -> None:
        """Partial results must reach the analyst rather than being lost."""
        provider = MockProvider(
            [
                MockRule(
                    name="left",
                    match_request_key=":research_agent:",
                    fault=Fault("timeout", attempts=tuple(range(1, 100))),
                ),
                MockRule(name="other", responses=(agent_output("survived"),)),
            ]
        )
        harness = harness_factory(provider)
        workflow = _diamond(JoinPolicy.ALL_SETTLED)
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert state.node_states["left"].status is NodeStatus.FAILED
        assert state.node_states["right"].status is NodeStatus.SUCCEEDED
        assert state.node_states["join"].status is NodeStatus.SUCCEEDED
        assert state.agent_outputs["join"]["partial"] is True
        assert "right" in state.agent_outputs["join"]["joined"]
        assert result.status is ExecutionStatus.SUCCEEDED, (
            "a tolerant join must not fail the execution"
        )

    async def test_all_join_blocks_when_a_branch_fails(
        self, harness_factory: HarnessFactory
    ) -> None:
        """A strict join must not fire on partial results."""
        provider = MockProvider(
            [
                MockRule(
                    name="left",
                    match_request_key=":research_agent:",
                    fault=Fault("timeout", attempts=tuple(range(1, 100))),
                ),
                MockRule(name="other", responses=(agent_output("ok"),)),
            ]
        )
        harness = harness_factory(provider)
        workflow = _diamond(JoinPolicy.ALL)
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.status is ExecutionStatus.FAILED
        # A strict join that never became ready has no state record at all.
        assert "join" not in state.node_states

    async def test_quorum_join_fires_on_enough_successes(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="left",
                    match_request_key=":research_agent:",
                    fault=Fault("timeout", attempts=tuple(range(1, 100))),
                ),
                MockRule(name="other", responses=(agent_output("ok"),)),
            ]
        )
        harness = harness_factory(provider)
        workflow = _diamond(JoinPolicy.QUORUM, quorum=1)
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert state.node_states["join"].status is NodeStatus.SUCCEEDED
        assert result.status is ExecutionStatus.SUCCEEDED

    async def test_any_join_cancels_a_not_yet_started_branch(
        self, harness_factory: HarnessFactory
    ) -> None:
        """A losing branch that has not started is cancelled, not run.

        The engine cancels *pending* siblings only. A branch already in flight
        runs to completion, because the scheduler settles a whole ready set
        before evaluating joins -- so the losing branch here sits behind another
        node and is therefore still pending when the join fires.
        """
        provider = MockProvider([MockRule(name="any", responses=(agent_output("done"),))])
        harness = harness_factory(provider)
        workflow = Workflow(
            name="race",
            nodes=(
                _agent_node("fast", "research_agent"),
                _agent_node("gate", "critic_agent"),
                _agent_node("mid", "data_agent"),
                _agent_node("slow", "pricing_agent"),
                WorkflowNode(id="join", kind=NodeKind.JOIN, join_policy=JoinPolicy.ANY),
            ),
            edges=(
                WorkflowEdge(source="fast", target="join"),
                # Two hops behind, so `slow` is still PENDING in the step where
                # the join becomes satisfiable. One hop would place it in the
                # same ready set as the join and it would already be running.
                WorkflowEdge(source="gate", target="mid"),
                WorkflowEdge(source="mid", target="slow"),
                WorkflowEdge(source="slow", target="join"),
            ),
            entry_nodes=("fast", "gate"),
        )
        state = harness.state(workflow)
        await harness.executor(workflow).run(state)
        assert state.node_states["join"].status is NodeStatus.SUCCEEDED
        assert state.node_states["slow"].status is NodeStatus.CANCELLED

    async def test_any_join_does_not_interrupt_an_in_flight_branch(
        self, harness_factory: HarnessFactory
    ) -> None:
        """Documented limitation, asserted so it cannot regress silently."""
        provider = MockProvider(
            [
                MockRule(
                    name="fast",
                    match_request_key=":research_agent:",
                    responses=(agent_output("fast"),),
                ),
                MockRule(name="slow", responses=(agent_output("slow"),), latency_seconds=0.1),
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="race",
            nodes=(
                _agent_node("fast", "research_agent"),
                _agent_node("slow", "pricing_agent"),
                WorkflowNode(id="join", kind=NodeKind.JOIN, join_policy=JoinPolicy.ANY),
            ),
            edges=(
                WorkflowEdge(source="fast", target="join"),
                WorkflowEdge(source="slow", target="join"),
            ),
            entry_nodes=("fast", "slow"),
        )
        state = harness.state(workflow)
        await harness.executor(workflow).run(state)
        assert state.node_states["join"].status is NodeStatus.SUCCEEDED
        assert state.node_states["slow"].status is NodeStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------


class TestConditionalRouting:
    def _conditional(self) -> Workflow:
        low = NodeCondition(
            conditions=(Condition(path="outputs.gate.confidence", operator="lt", value=0.6),)
        )
        high = NodeCondition(
            conditions=(Condition(path="outputs.gate.confidence", operator="gte", value=0.6),)
        )
        return Workflow(
            name="conditional",
            nodes=(
                _agent_node("gate", "research_agent"),
                _agent_node("more_research", "pricing_agent"),
                _agent_node("finalize", "finalizer_agent"),
            ),
            edges=(
                WorkflowEdge(source="gate", target="more_research", condition=low, label="low"),
                WorkflowEdge(source="gate", target="finalize", condition=high, label="high"),
            ),
        )

    async def test_high_confidence_takes_the_finalize_branch(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="gate",
                    match_request_key=":research_agent:",
                    responses=(agent_output("confident", confidence=0.9),),
                ),
                MockRule(name="other", responses=(agent_output("done"),)),
            ]
        )
        harness = harness_factory(provider)
        workflow = self._conditional()
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.succeeded
        assert state.node_states["finalize"].status is NodeStatus.SUCCEEDED
        assert state.node_states["more_research"].status is NodeStatus.SKIPPED

    async def test_low_confidence_takes_the_research_branch(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="gate",
                    match_request_key=":research_agent:",
                    responses=(agent_output("unsure", confidence=0.2),),
                ),
                MockRule(name="other", responses=(agent_output("done"),)),
            ]
        )
        harness = harness_factory(provider)
        workflow = self._conditional()
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.succeeded
        assert state.node_states["more_research"].status is NodeStatus.SUCCEEDED
        assert state.node_states["finalize"].status is NodeStatus.SKIPPED

    async def test_an_untaken_branch_does_not_stall_a_join(
        self, harness_factory: HarnessFactory
    ) -> None:
        """The property SKIPPED-counts-as-complete exists for exactly this."""
        never = NodeCondition(
            conditions=(Condition(path="outputs.gate.confidence", operator="lt", value=0.1),)
        )
        workflow = Workflow(
            name="skip-join",
            nodes=(
                _agent_node("gate", "research_agent"),
                _agent_node("maybe", "pricing_agent"),
                WorkflowNode(id="join", kind=NodeKind.JOIN, join_policy=JoinPolicy.ALL_SETTLED),
                _agent_node("after", "analyst_agent"),
            ),
            edges=(
                WorkflowEdge(source="gate", target="maybe", condition=never),
                WorkflowEdge(source="gate", target="join"),
                WorkflowEdge(source="maybe", target="join"),
                WorkflowEdge(source="join", target="after"),
            ),
        )
        provider = MockProvider(
            [
                MockRule(
                    name="gate",
                    match_request_key=":research_agent:",
                    responses=(agent_output("sure", confidence=0.95),),
                ),
                MockRule(name="other", responses=(agent_output("done"),)),
            ]
        )
        harness = harness_factory(provider)
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert state.node_states["maybe"].status is NodeStatus.SKIPPED
        assert state.node_states["join"].status is NodeStatus.SUCCEEDED
        assert state.node_states["after"].status is NodeStatus.SUCCEEDED
        assert result.succeeded

    async def test_branch_node_records_its_reasoning(self, harness_factory: HarnessFactory) -> None:
        """ "Why did it take that path" must be answerable from the event log."""
        workflow = Workflow(
            name="branch",
            nodes=(
                _agent_node("gate", "research_agent"),
                WorkflowNode(id="decide", kind=NodeKind.BRANCH),
                _agent_node("high", "analyst_agent"),
            ),
            edges=(
                WorkflowEdge(source="gate", target="decide"),
                WorkflowEdge(
                    source="decide",
                    target="high",
                    condition=NodeCondition(
                        conditions=(
                            Condition(path="outputs.gate.confidence", operator="gte", value=0.5),
                        )
                    ),
                ),
            ),
        )
        provider = MockProvider(
            [MockRule(name="r", responses=(agent_output("x", confidence=0.8),))]
        )
        harness = harness_factory(provider)
        await harness.executor(workflow).run(harness.state(workflow))

        branch_events = [
            e for e in harness.events.of_type(EventType.NODE_COMPLETED) if e.node_id == "decide"
        ]
        assert branch_events
        evaluations = branch_events[0].payload["evaluations"]
        assert "0.8" in str(evaluations)


# ---------------------------------------------------------------------------
# Retries and recovery
# ---------------------------------------------------------------------------


class TestRetryAndRecovery:
    async def test_transient_failure_is_retried_then_succeeds(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="flaky",
                    responses=(agent_output("recovered"),),
                    # The LLM client retries transient faults itself (3 attempts
                    # by default), so a fault must outlast that to surface as a
                    # node failure the *executor* then retries.
                    fault=Fault("timeout", attempts=(1, 2, 3)),
                )
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(name="retry", nodes=(_agent_node("a", "analyst_agent"),))
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.succeeded
        assert state.node_states["a"].status is NodeStatus.SUCCEEDED
        assert harness.events.count(EventType.RETRY_STARTED) >= 1

    async def test_recovery_is_recorded_explicitly(self, harness_factory: HarnessFactory) -> None:
        """Recovery rate is a headline metric, so it is recorded not inferred."""
        provider = MockProvider(
            [
                MockRule(
                    name="flaky",
                    responses=(agent_output("recovered"),),
                    # The LLM client retries transient faults itself (3 attempts
                    # by default), so a fault must outlast that to surface as a
                    # node failure the *executor* then retries.
                    fault=Fault("timeout", attempts=(1, 2, 3)),
                )
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(name="retry", nodes=(_agent_node("a", "analyst_agent"),))
        state = harness.state(workflow)
        await harness.executor(workflow).run(state)

        assert state.recovered_error_count == 1
        assert len(state.errors) == 1, "the error history must survive recovery"
        assert state.errors[0].recovered is True

    async def test_terminal_failure_is_not_retried(self, harness_factory: HarnessFactory) -> None:
        """A missing agent cannot be fixed by trying again."""
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(provider)
        harness.agents.remove("analyst_agent")
        workflow = Workflow(name="missing", nodes=(_agent_node("a", "analyst_agent"),))
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.status is ExecutionStatus.FAILED
        assert harness.events.count(EventType.RETRY_STARTED) == 0

    async def test_retries_are_exhausted_then_the_node_fails(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [MockRule(name="always", fault=Fault("timeout", attempts=tuple(range(1, 100))))]
        )
        harness = harness_factory(provider)
        node = _agent_node("a", "analyst_agent")
        workflow = Workflow(
            name="doomed",
            nodes=(node.model_copy(update={"retry_policy": node.retry_policy}),),
        )
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.status is ExecutionStatus.FAILED
        assert state.node_states["a"].status is NodeStatus.FAILED
        assert harness.events.count(EventType.RETRY_EXHAUSTED) >= 1

    async def test_attempt_counts_accumulate_on_the_node(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="flaky",
                    responses=(agent_output("ok"),),
                    # Two node attempts' worth of client retries (3 each).
                    fault=Fault("timeout", attempts=tuple(range(1, 7))),
                )
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(name="retry", nodes=(_agent_node("a", "analyst_agent"),))
        state = harness.state(workflow)
        await harness.executor(workflow).run(state)
        assert state.node_states["a"].attempts == 3

    async def test_optional_node_failure_does_not_fail_the_run(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="opt",
                    match_request_key=":pricing_agent:",
                    fault=Fault("timeout", attempts=tuple(range(1, 100))),
                ),
                MockRule(name="other", responses=(agent_output("fine"),)),
            ]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="optional",
            nodes=(
                _agent_node("main", "research_agent"),
                _agent_node("extra", "pricing_agent", optional=True),
            ),
            entry_nodes=("main", "extra"),
        )
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert state.node_states["extra"].status is NodeStatus.FAILED
        assert result.status is ExecutionStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


class TestTimeouts:
    async def test_node_timeout_is_enforced(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider(
            [MockRule(name="slow", responses=(agent_output("late"),), latency_seconds=2.0)]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="timeout",
            nodes=(_agent_node("a", "analyst_agent", timeout_seconds=0.1),),
        )
        state = harness.state(workflow)
        result = await harness.executor(workflow).run(state)

        assert result.status is ExecutionStatus.FAILED
        assert state.node_states["a"].error is not None
        assert state.node_states["a"].error["code"] == "timeout"

    async def test_a_timeout_is_retried_because_it_is_transient(
        self, harness_factory: HarnessFactory
    ) -> None:
        harness = harness_factory(
            MockProvider(
                [MockRule(name="slow", responses=(agent_output("x"),), latency_seconds=2.0)]
            )
        )
        workflow = Workflow(
            name="timeout",
            nodes=(_agent_node("a", "analyst_agent", timeout_seconds=0.05),),
        )
        state = harness.state(workflow)
        await harness.executor(workflow).run(state)
        assert state.node_states["a"].attempts > 1


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_cancellation_stops_a_running_execution(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [MockRule(name="slow", responses=(agent_output("x"),), latency_seconds=0.5)]
        )
        harness = harness_factory(provider)
        workflow = Workflow(
            name="cancellable",
            nodes=(
                _agent_node("a", "research_agent"),
                _agent_node("b", "analyst_agent"),
            ),
            edges=(WorkflowEdge(source="a", target="b"),),
        )
        state = harness.state(workflow)
        executor = harness.executor(workflow)

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            executor.cancel_token.cancel("operator requested stop")

        _, result = await asyncio.gather(cancel_soon(), executor.run(state))

        assert result.status is ExecutionStatus.CANCELLED
        assert state.failure_reason == "operator requested stop"
        assert harness.events.count(EventType.EXECUTION_CANCELLED) == 1

    async def test_cancellation_before_any_step_still_terminates_cleanly(
        self, harness_factory: HarnessFactory
    ) -> None:
        harness = harness_factory(
            MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        )
        workflow = Workflow(name="c", nodes=(_agent_node("a", "analyst_agent"),))
        state = harness.state(workflow)
        executor = harness.executor(workflow)
        executor.cancel_token.cancel("cancelled before start")
        result = await executor.run(state)

        assert result.status is ExecutionStatus.CANCELLED
        assert state.node_states.get("a") is None or (
            state.node_states["a"].status is not NodeStatus.SUCCEEDED
        )

    async def test_cancelled_state_is_checkpointed(self, harness_factory: HarnessFactory) -> None:
        harness = harness_factory(
            MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        )
        workflow = Workflow(name="c", nodes=(_agent_node("a", "analyst_agent"),))
        executor = harness.executor(workflow)
        executor.cancel_token.cancel("stop")
        await executor.run(harness.state(workflow))
        assert CheckpointReason.ON_CANCELLATION in harness.reasons()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    async def test_agent_step_limit_stops_the_run(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(
            provider,
            budget=Budget(
                max_agent_steps=2,
                max_cost_usd=None,
                max_tokens=None,
                max_duration_seconds=None,
                max_tool_calls=None,
                max_retries=None,
            ),
        )
        workflow = Workflow(
            name="long",
            nodes=tuple(_agent_node(f"n{i}", "analyst_agent") for i in range(6)),
            entry_nodes=tuple(f"n{i}" for i in range(6)),
        )
        state = harness.state(workflow)
        result = await harness.executor(workflow, max_concurrent_nodes=1).run(state)

        assert result.status is ExecutionStatus.BUDGET_EXCEEDED
        assert state.budget_usage.agent_steps <= 3
        assert harness.events.count(EventType.BUDGET_EXCEEDED) == 1

    async def test_budget_exceeded_reason_names_the_dimension(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider(
            [MockRule(name="r", responses=(agent_output("x"),), usage=(5000, 500))]
        )
        harness = harness_factory(
            provider,
            budget=Budget(
                max_tokens=1_000,
                max_cost_usd=None,
                max_duration_seconds=None,
                max_agent_steps=None,
                max_tool_calls=None,
                max_retries=None,
            ),
        )
        workflow = Workflow(
            name="tokens",
            nodes=tuple(_agent_node(f"n{i}", "analyst_agent") for i in range(4)),
            entry_nodes=tuple(f"n{i}" for i in range(4)),
        )
        state = harness.state(workflow)
        await harness.executor(workflow, max_concurrent_nodes=1).run(state)
        assert state.status is ExecutionStatus.BUDGET_EXCEEDED
        assert "tokens" in (state.failure_reason or "")

    async def test_budget_exhaustion_is_checkpointed(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(
            provider,
            budget=Budget(
                max_agent_steps=1,
                max_cost_usd=None,
                max_tokens=None,
                max_duration_seconds=None,
                max_tool_calls=None,
                max_retries=None,
            ),
        )
        workflow = Workflow(
            name="b",
            nodes=(_agent_node("a", "analyst_agent"), _agent_node("b", "critic_agent")),
            edges=(WorkflowEdge(source="a", target="b"),),
        )
        await harness.executor(workflow).run(harness.state(workflow))
        assert CheckpointReason.ON_BUDGET_EXCEEDED in harness.reasons()


# ---------------------------------------------------------------------------
# Checkpointing points
# ---------------------------------------------------------------------------


class TestCheckpointPoints:
    async def test_checkpoints_bracket_every_node(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(provider)
        workflow = Workflow(
            name="cp",
            nodes=(_agent_node("a", "research_agent"), _agent_node("b", "analyst_agent")),
            edges=(WorkflowEdge(source="a", target="b"),),
        )
        await harness.executor(workflow).run(harness.state(workflow))

        reasons = harness.reasons()
        assert reasons[0] is CheckpointReason.EXECUTION_STARTED
        assert reasons.count(CheckpointReason.BEFORE_NODE) == 2
        assert reasons.count(CheckpointReason.AFTER_NODE_SUCCESS) == 2
        assert CheckpointReason.BEFORE_FINALIZATION in reasons

    async def test_failed_node_is_checkpointed(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider(
            [MockRule(name="fail", fault=Fault("timeout", attempts=tuple(range(1, 100))))]
        )
        harness = harness_factory(provider)
        workflow = Workflow(name="f", nodes=(_agent_node("a", "analyst_agent"),))
        await harness.executor(workflow).run(harness.state(workflow))
        assert CheckpointReason.AFTER_NODE_FAILURE in harness.reasons()

    async def test_every_checkpoint_is_serialisable_and_hashed(
        self, harness_factory: HarnessFactory
    ) -> None:
        """Resume depends on this at every single checkpoint point."""
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(provider)
        workflow = _diamond(JoinPolicy.ALL)
        await harness.executor(workflow).run(harness.state(workflow))

        assert harness.checkpoints
        for captured in harness.checkpoints:
            blob = captured.checkpoint.model_dump(mode="json")
            assert blob["content_hash"]
            from orchestration.domain.checkpoint import Checkpoint

            restored = Checkpoint.model_validate(blob)
            assert restored.compute_hash() == captured.checkpoint.compute_hash()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:
    async def test_lifecycle_events_are_emitted_in_order(
        self, harness_factory: HarnessFactory
    ) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(provider)
        workflow = Workflow(name="e", nodes=(_agent_node("a", "analyst_agent"),))
        await harness.executor(workflow).run(harness.state(workflow))

        types = [e.type for e in harness.events.events]
        assert types[0] is EventType.EXECUTION_STARTED
        assert EventType.NODE_STARTED in types
        assert EventType.AGENT_INVOKED in types
        assert EventType.AGENT_COMPLETED in types
        assert EventType.NODE_COMPLETED in types
        assert types[-1] is EventType.EXECUTION_COMPLETED

    async def test_sequence_numbers_are_monotonic_under_concurrency(
        self, harness_factory: HarnessFactory
    ) -> None:
        """Parallel branches emit within the same microsecond; order must hold."""
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(provider)
        workflow = Workflow(
            name="wide",
            nodes=tuple(_agent_node(f"n{i}", "research_agent") for i in range(5)),
            entry_nodes=tuple(f"n{i}" for i in range(5)),
        )
        await harness.executor(workflow).run(harness.state(workflow))

        sequences = [e.sequence for e in harness.events.events]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences), "duplicate sequence numbers"

    async def test_no_sink_failures_occurred(self, harness_factory: HarnessFactory) -> None:
        provider = MockProvider([MockRule(name="r", responses=(agent_output("x"),))])
        harness = harness_factory(provider)
        workflow = Workflow(name="e", nodes=(_agent_node("a", "analyst_agent"),))
        await harness.executor(workflow).run(harness.state(workflow))
        assert harness.bus.failures == ()
