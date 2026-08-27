"""Shared agent runtime.

One reason/act loop drives every agent. An :class:`AgentDefinition` supplies the
prompt, the tool allowlist, the model criteria and the limits; this module
interprets them. There is no per-agent subclass, which is what makes an agent
registered over the HTTP API behave exactly like a built-in one.

The loop:

1. Build the conversation from the system prompt, the task, and prior context.
2. Ask the model for either a tool call or a final structured answer.
3. If it asked for tools: authorise each one through the policy gate, execute the
   permitted ones concurrently, feed the results back, and continue.
4. If it answered: validate against :class:`AgentOutput` and return.
5. Stop at ``max_iterations`` and return the best available answer rather than
   looping until the budget dies.

Three properties are load-bearing:

**Tool authorisation is a callback, not a policy decision made here.**
    The runtime is handed an authoriser. It cannot accidentally implement a
    weaker rule than the policy engine, because it does not implement one at all.

**A denied or failed tool is a value, not an exception.**
    The agent is told "that tool is not available to you" and can adapt. Aborting
    its whole turn over one bad tool call would waste the work already done.

**Budget is consulted before every model and tool call.**
    Not after, when the tokens are already spent.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from orchestration.domain.agent import AgentDefinition, AgentInvocation, AgentOutput
from orchestration.domain.base import JsonDict, utc_now
from orchestration.domain.enums import InvocationStatus, PolicyEffect, TaskComplexity
from orchestration.domain.model import LLMRequest, Message, ModelConfig, ToolCallRequest
from orchestration.domain.tool import SENSITIVE_ARGUMENT_KEYS, ToolResult
from orchestration.errors import (
    ApprovalRequired,
    BudgetExceededError,
    ConfigurationError,
    OrchestrationError,
    PermissionDeniedError,
    SchemaViolationError,
    to_error_dict,
)
from orchestration.llm.base import extract_json_object
from orchestration.llm.factory import LLMClient
from orchestration.observability.metrics import (
    record_agent_invocation,
    record_llm_call,
    record_tool_invocation,
)
from orchestration.observability.tracing import (
    agent_span,
    annotate_llm_usage,
    llm_call_span,
    tool_span,
)
from orchestration.routing.model_router import ModelRouter
from orchestration.tools.base import ToolContext
from orchestration.tools.registry import ToolRegistry

#: Verdict returned by the policy gate for one requested tool call.
ToolDecision = tuple[PolicyEffect, str]

#: Authorises a tool call. Returns the effect plus a human-readable reason.
ToolAuthoriser = Callable[[str, str, JsonDict], Awaitable[ToolDecision]]

#: Consulted before each model and tool call. Raises to stop the agent.
BudgetCheck = Callable[[str], Awaitable[None]]

#: Notified of each completed tool invocation, for events and usage accounting.
ToolObserver = Callable[[str, ToolResult], Awaitable[None]]


async def _allow_all(agent_id: str, tool: str, arguments: JsonDict) -> ToolDecision:
    """Default authoriser used only in unit tests of the loop itself."""
    return PolicyEffect.ALLOW, "no policy engine attached"


async def _no_budget_limit(reason: str) -> None:
    """Default budget check: unmetered."""


@dataclass(slots=True)
class AgentRunContext:
    """Everything an agent run needs beyond its definition."""

    execution_id: str
    node_id: str | None = None
    #: Rendered instruction for this specific run, narrower than the whole task.
    instruction: str = ""
    #: Outputs of upstream nodes, injected as context.
    prior_outputs: dict[str, JsonDict] = field(default_factory=dict)
    #: Execution variables available for templating.
    variables: JsonDict = field(default_factory=dict)
    sandbox_root: Any = None
    deadline_seconds: float = 120.0
    attempt: int = 1
    trace_id: str | None = None
    #: Seed making the mock provider and any sampling reproducible.
    seed: str = "agent"


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of one agent run."""

    invocation: AgentInvocation
    output: AgentOutput | None
    tool_results: tuple[ToolResult, ...]
    #: Approval that suspended the run, if any.
    pending_approval: ApprovalRequired | None = None

    @property
    def succeeded(self) -> bool:
        return self.invocation.status is InvocationStatus.SUCCEEDED


class AgentRuntime:
    """Executes agents defined by :class:`AgentDefinition`."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        router: ModelRouter,
        authoriser: ToolAuthoriser | None = None,
        budget_check: BudgetCheck | None = None,
        tool_observer: ToolObserver | None = None,
        max_concurrent_tools: int = 8,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._router = router
        self._authorise = authoriser or _allow_all
        self._check_budget = budget_check or _no_budget_limit
        self._observe_tool = tool_observer
        self._tool_semaphore = asyncio.Semaphore(max_concurrent_tools)

    # -- public API --------------------------------------------------------

    async def run(self, definition: AgentDefinition, context: AgentRunContext) -> AgentRunResult:
        """Execute one agent to completion, a hard stop, or a suspension."""
        with agent_span(
            context.execution_id, definition.id, node_id=context.node_id, attempt=context.attempt
        ):
            return await self._run_traced(definition, context)

    async def _run_traced(
        self, definition: AgentDefinition, context: AgentRunContext
    ) -> AgentRunResult:
        if not definition.enabled:
            raise ConfigurationError(f"agent {definition.id!r} is disabled", agent=definition.id)

        started = time.perf_counter()
        invocation = AgentInvocation(
            execution_id=context.execution_id,
            node_id=context.node_id,
            agent_id=definition.id,
            attempt=context.attempt,
            task_input=context.instruction[:4_000],
            trace_id=context.trace_id,
        )

        model = self._select_model(definition, context)
        invocation.model_key = model.key

        messages = list(self._build_initial_messages(definition, context))
        tool_schemas = self._tools.llm_schemas_for_agent(definition.tool_names)
        collected: list[ToolResult] = []

        try:
            for iteration in range(1, definition.max_iterations + 1):
                invocation.iterations = iteration
                await self._check_budget(f"agent:{definition.id}:iteration:{iteration}")

                llm_started = time.perf_counter()
                with llm_call_span(
                    context.execution_id, model.key, model.provider.value, purpose="agent_turn"
                ) as span:
                    response = await self._llm.complete(
                        LLMRequest(
                            messages=tuple(messages),
                            model=model,
                            tools=tool_schemas,
                            timeout_seconds=context.deadline_seconds,
                            request_key=f"{context.seed}:{definition.id}:{iteration}",
                            response_schema=None if tool_schemas else _AGENT_OUTPUT_SCHEMA,
                        ),
                        retry_policy=definition.retry_policy,
                    )
                    llm_elapsed = time.perf_counter() - llm_started
                    annotate_llm_usage(
                        span,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        cost_usd=response.cost_usd,
                        latency_seconds=llm_elapsed,
                    )
                record_llm_call(
                    model.provider.value,
                    model.key,
                    "succeeded",
                    duration_seconds=llm_elapsed,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cost_usd=response.cost_usd,
                )
                invocation.input_tokens += response.usage.input_tokens
                invocation.output_tokens += response.usage.output_tokens
                invocation.cost_usd = round(invocation.cost_usd + response.cost_usd, 8)

                requested = self._requested_tool_calls(response.content, response.tool_calls)
                if not requested:
                    output = self._parse_output(response.content, definition)
                    return self._finish(
                        invocation, output, collected, started, InvocationStatus.SUCCEEDED
                    )

                messages.append(Message.assistant(response.content or "[tool calls]"))
                results = await self._run_tool_calls(definition, context, requested)
                collected.extend(results)
                invocation.tool_calls += len(results)
                messages.append(Message.user(self._render_tool_results(results)))

            # Iteration ceiling reached. Ask once for a final answer with tools
            # withheld, so the agent produces its best summary of what it found
            # rather than the run simply being discarded.
            output = await self._force_final_answer(
                definition, model, messages, context, invocation
            )
            return self._finish(invocation, output, collected, started, InvocationStatus.SUCCEEDED)

        except ApprovalRequired as pending:
            invocation.status = InvocationStatus.RUNNING
            invocation.completed_at = None
            return AgentRunResult(
                invocation=invocation,
                output=None,
                tool_results=tuple(collected),
                pending_approval=pending,
            )
        except BudgetExceededError as exc:
            invocation.error = to_error_dict(exc)
            return self._finish(invocation, None, collected, started, InvocationStatus.FAILED)
        except OrchestrationError as exc:
            invocation.error = to_error_dict(exc)
            status = (
                InvocationStatus.TIMED_OUT
                if exc.code == "timeout"
                else InvocationStatus.DENIED
                if exc.code in {"permission_denied", "policy_violation"}
                else InvocationStatus.FAILED
            )
            self._finish(invocation, None, collected, started, status)
            raise

    # -- model selection ---------------------------------------------------

    def _select_model(self, definition: AgentDefinition, context: AgentRunContext) -> ModelConfig:
        criteria = definition.routing_criteria
        if definition.model_key:
            criteria = criteria.model_copy(update={"pinned_model": definition.model_key})
        complexity = self._estimate_complexity(definition, context)
        selection = self._router.select(criteria, complexity=complexity)
        return selection.model

    @staticmethod
    def _estimate_complexity(
        definition: AgentDefinition, context: AgentRunContext
    ) -> TaskComplexity:
        """Coarse complexity signal for model routing.

        Deliberately crude -- length of instruction plus amount of upstream
        context. A learned estimator would need production traffic to beat this,
        and a wrong guess only changes which capable model is used.
        """
        size = len(context.instruction) + sum(
            len(json.dumps(v, default=str)) for v in context.prior_outputs.values()
        )
        if definition.kind in {"analyst", "critic", "finalizer"}:
            return TaskComplexity.COMPLEX
        if size > 8_000:
            return TaskComplexity.COMPLEX
        if size > 2_000:
            return TaskComplexity.MODERATE
        return TaskComplexity.SIMPLE

    # -- prompt construction -----------------------------------------------

    def _build_initial_messages(
        self, definition: AgentDefinition, context: AgentRunContext
    ) -> Sequence[Message]:
        messages = [Message.system(definition.system_prompt)]

        parts: list[str] = []
        if context.instruction:
            parts.append(f"Task:\n{context.instruction}")

        if context.prior_outputs:
            rendered = self._render_prior_outputs(context.prior_outputs)
            parts.append(f"Findings from earlier agents:\n{rendered}")

        if context.variables:
            # Only scalars: dumping whole nested structures would blow the prompt
            # budget, and anything larger belongs in prior_outputs anyway.
            scalars = {
                k: v
                for k, v in context.variables.items()
                if isinstance(v, str | int | float | bool)
            }
            if scalars:
                parts.append(f"Parameters:\n{json.dumps(scalars, indent=2)}")

        if definition.allowed_tools:
            available = self._tools.specs_for_agent(definition.tool_names)
            names = ", ".join(spec.name for spec in available) or "none"
            parts.append(
                f"Tools available to you: {names}. "
                "You may only use these. Requesting anything else will be denied."
            )

        messages.append(Message.user("\n\n".join(parts) or "Proceed with the task."))
        return messages

    @staticmethod
    def _render_prior_outputs(outputs: dict[str, JsonDict], *, per_output: int = 3_000) -> str:
        """Render upstream results compactly.

        Truncated per output rather than in aggregate, so one verbose upstream
        agent cannot crowd out every other finding.
        """
        chunks: list[str] = []
        for key, payload in outputs.items():
            if isinstance(payload, dict) and "content" in payload:
                body = str(payload.get("content", ""))[:per_output]
                confidence = payload.get("confidence")
                evidence = payload.get("evidence") or []
                header = f"--- {key}"
                if confidence is not None:
                    header += f" (confidence {confidence})"
                header += " ---"
                chunk = f"{header}\n{body}"
                if evidence:
                    chunk += "\nSources: " + "; ".join(str(e) for e in list(evidence)[:8])
                chunks.append(chunk)
            else:
                chunks.append(f"--- {key} ---\n{json.dumps(payload, default=str)[:per_output]}")
        return "\n\n".join(chunks)

    # -- tool handling -----------------------------------------------------

    def _requested_tool_calls(
        self, content: str, native_calls: tuple[ToolCallRequest, ...]
    ) -> tuple[ToolCallRequest, ...]:
        """Determine which tools the model asked for.

        Prefers native tool calls. Falls back to a ``tool_calls`` array inside a
        JSON reply, because local models frequently emit that shape instead of
        using the provider's tool protocol -- and refusing to understand them
        would make the local path far less useful than it needs to be.
        """
        if native_calls:
            return native_calls
        if not content.strip():
            return ()
        try:
            payload = extract_json_object(content)
        except SchemaViolationError:
            return ()
        raw = payload.get("tool_calls") or payload.get("tools")
        if not isinstance(raw, list):
            return ()
        calls: list[ToolCallRequest] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("tool")
            if not name:
                continue
            arguments = item.get("arguments") or item.get("args") or {}
            calls.append(
                ToolCallRequest(
                    id=str(item.get("id") or f"inline_{index}"),
                    name=str(name),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return tuple(calls)

    async def _run_tool_calls(
        self,
        definition: AgentDefinition,
        context: AgentRunContext,
        calls: tuple[ToolCallRequest, ...],
    ) -> tuple[ToolResult, ...]:
        """Authorise and execute the requested tool calls concurrently.

        Authorisation happens for every call before any of them run, so a batch
        containing one forbidden call does not get to execute the rest under the
        cover of concurrency before the denial surfaces.
        """
        authorised: list[tuple[ToolCallRequest, PolicyEffect, str]] = []
        for call in calls:
            effect, reason = await self._authorise(definition.id, call.name, call.arguments)
            authorised.append((call, effect, reason))

        async def _execute(call: ToolCallRequest, effect: PolicyEffect, reason: str) -> ToolResult:
            if effect is PolicyEffect.DENY:
                return ToolResult.failure(
                    call.name, error_code="permission_denied", error_message=reason
                )
            if effect is PolicyEffect.REQUIRE_APPROVAL:
                # Propagates out of the whole run: the execution must suspend
                # durably, which the runtime cannot do on its own.
                raise ApprovalRequired(
                    f"tool {call.name!r} requires human approval",
                    approval_id="",
                    agent=definition.id,
                    tool=call.name,
                    arguments=_redact(call.arguments),
                    risk_reason=reason,
                )
            return await self._invoke_tool(definition, context, call)

        results = await asyncio.gather(
            *(_execute(c, e, r) for c, e, r in authorised),
            return_exceptions=True,
        )

        final: list[ToolResult] = []
        for (call, _, _), outcome in zip(authorised, results, strict=True):
            if isinstance(outcome, ApprovalRequired):
                raise outcome
            if isinstance(outcome, BaseException):
                # A tool failure is reported to the agent as a value so it can
                # adapt, rather than destroying the turn's accumulated work.
                error = to_error_dict(outcome)
                final.append(
                    ToolResult.failure(
                        call.name,
                        error_code=str(error["code"]),
                        error_message=str(error["message"]),
                    )
                )
            else:
                final.append(outcome)

        if self._observe_tool is not None:
            for result in final:
                await self._observe_tool(definition.id, result)
        return tuple(final)

    async def _invoke_tool(
        self,
        definition: AgentDefinition,
        context: AgentRunContext,
        call: ToolCallRequest,
    ) -> ToolResult:
        await self._check_budget(f"tool:{call.name}")

        permission = definition.permission_for(call.name)
        if permission is None:
            # Defence in depth: the authoriser should already have denied this.
            raise PermissionDeniedError(
                f"agent {definition.id!r} may not use tool {call.name!r}",
                agent=definition.id,
                tool=call.name,
            )

        tool = self._tools.get(call.name)
        tool_context = ToolContext(
            execution_id=context.execution_id,
            agent_id=definition.id,
            node_id=context.node_id,
            sandbox_root=context.sandbox_root or ToolContext(execution_id="x").sandbox_root,
            deadline_seconds=context.deadline_seconds,
            attempt=context.attempt,
            constraints=permission.constraints,
        )

        started = time.perf_counter()
        attempts = 0
        policy = tool.spec.retry_policy
        with tool_span(
            context.execution_id, call.name, agent_id=definition.id, risk=tool.spec.risk.value
        ):
            while True:
                attempts += 1
                async with self._tool_semaphore:
                    try:
                        output = await tool.invoke(call.arguments, tool_context)
                    except Exception as exc:
                        if not policy.should_retry(attempts, exc):
                            record_tool_invocation(
                                call.name, "failed", duration_seconds=time.perf_counter() - started
                            )
                            raise
                        delay = policy.backoff_for(attempts, exc)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        continue
                record_tool_invocation(
                    call.name, "succeeded", duration_seconds=time.perf_counter() - started
                )
                return ToolResult.success(
                    call.name,
                    output,
                    duration_seconds=round(time.perf_counter() - started, 6),
                    attempts=attempts,
                )

    @staticmethod
    def _render_tool_results(results: Sequence[ToolResult]) -> str:
        """Feed tool outcomes back to the model, successes and failures alike."""
        lines = ["Tool results:"]
        for result in results:
            if result.ok:
                payload = json.dumps(result.output, default=str)
                lines.append(f"[{result.tool}] ok -> {payload[:4_000]}")
            else:
                lines.append(
                    f"[{result.tool}] FAILED ({result.error_code}): {result.error_message}"
                )
        lines.append(
            "Use these results. If a tool failed or was denied, do not retry it -- "
            "work with what you have and record the shortfall in gaps."
        )
        return "\n".join(lines)

    # -- output handling ---------------------------------------------------

    def _parse_output(self, content: str, definition: AgentDefinition) -> AgentOutput:
        """Validate the agent's reply into :class:`AgentOutput`.

        A reply that is not the agreed JSON becomes a low-confidence output
        carrying the raw text, rather than an error. The text may still be
        useful, and flagging low confidence lets the supervisor decide -- which is
        strictly better than discarding work over a formatting mistake.
        """
        try:
            payload = extract_json_object(content)
        except SchemaViolationError:
            return AgentOutput(
                content=content.strip()[:200_000],
                confidence=0.3,
                gaps=("agent did not return the agreed JSON structure",),
            )
        try:
            return AgentOutput.model_validate(_coerce_output_payload(payload))
        except Exception:
            return AgentOutput(
                content=json.dumps(payload, default=str)[:200_000],
                confidence=0.3,
                gaps=("agent output failed schema validation",),
            )

    async def _force_final_answer(
        self,
        definition: AgentDefinition,
        model: ModelConfig,
        messages: list[Message],
        context: AgentRunContext,
        invocation: AgentInvocation,
    ) -> AgentOutput:
        """Request a final answer with no tools offered."""
        await self._check_budget(f"agent:{definition.id}:final")
        messages.append(
            Message.user(
                "You have reached your tool-use limit. Produce your final answer now "
                "from what you have already gathered, and record anything you could "
                "not establish in gaps."
            )
        )
        final_started = time.perf_counter()
        with llm_call_span(
            context.execution_id, model.key, model.provider.value, purpose="final_answer"
        ) as span:
            response = await self._llm.complete(
                LLMRequest(
                    messages=tuple(messages),
                    model=model,
                    response_schema=_AGENT_OUTPUT_SCHEMA,
                    timeout_seconds=context.deadline_seconds,
                    request_key=f"{context.seed}:{definition.id}:final",
                ),
                retry_policy=definition.retry_policy,
            )
            final_elapsed = time.perf_counter() - final_started
            annotate_llm_usage(
                span,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=response.cost_usd,
                latency_seconds=final_elapsed,
            )
        record_llm_call(
            model.provider.value,
            model.key,
            "succeeded",
            duration_seconds=final_elapsed,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.cost_usd,
        )
        invocation.input_tokens += response.usage.input_tokens
        invocation.output_tokens += response.usage.output_tokens
        invocation.cost_usd = round(invocation.cost_usd + response.cost_usd, 8)
        return self._parse_output(response.content, definition)

    @staticmethod
    def _finish(
        invocation: AgentInvocation,
        output: AgentOutput | None,
        tool_results: list[ToolResult],
        started: float,
        status: InvocationStatus,
    ) -> AgentRunResult:
        invocation.status = status
        invocation.output = output
        invocation.completed_at = utc_now()
        invocation.duration_seconds = round(time.perf_counter() - started, 6)
        record_agent_invocation(
            invocation.agent_id, status.value, duration_seconds=invocation.duration_seconds
        )
        return AgentRunResult(
            invocation=invocation, output=output, tool_results=tuple(tool_results)
        )


def _coerce_output_payload(payload: JsonDict) -> JsonDict:
    """Normalise near-miss agent output into the expected shape.

    Models return ``claims`` as a string, omit ``content`` in favour of
    ``answer``, or send ``confidence`` as a percentage. Coercing these costs a
    few lines and converts a large class of spurious validation failures into
    usable output. Anything genuinely unrecognised still fails validation.
    """
    result = dict(payload)

    for alias in ("answer", "result", "summary", "text", "findings"):
        if "content" not in result and alias in result:
            result["content"] = result.pop(alias)
    if "content" in result and not isinstance(result["content"], str):
        result["content"] = json.dumps(result["content"], default=str)

    confidence = result.get("confidence")
    if isinstance(confidence, str):
        mapping = {"high": 0.9, "medium": 0.6, "moderate": 0.6, "low": 0.3}
        result["confidence"] = mapping.get(confidence.strip().lower(), 0.5)
    elif isinstance(confidence, int | float) and confidence > 1:
        # A model reporting 85 rather than 0.85 meant 85 percent.
        result["confidence"] = min(1.0, float(confidence) / 100.0)

    for key in ("claims", "evidence", "gaps", "artifacts"):
        value = result.get(key)
        if isinstance(value, str):
            result[key] = [value] if value.strip() else []
        elif value is None:
            result.pop(key, None)

    data = result.get("data")
    if data is not None and not isinstance(data, dict):
        result["data"] = {"value": data}

    # Drop anything the schema forbids rather than failing: extra keys are a
    # model quirk, not information worth aborting the turn over.
    allowed = {"content", "confidence", "claims", "evidence", "gaps", "data", "artifacts"}
    return {k: v for k, v in result.items() if k in allowed}


def _redact(arguments: JsonDict) -> JsonDict:
    """Mask sensitive argument values before they reach an approval record."""
    return {k: ("***" if k.lower() in SENSITIVE_ARGUMENT_KEYS else v) for k, v in arguments.items()}


#: Schema requested when the agent has no tools to call, so its reply is
#: constrained to the output contract by the provider where supported.
_AGENT_OUTPUT_SCHEMA: JsonDict = AgentOutput.model_json_schema(mode="validation")
