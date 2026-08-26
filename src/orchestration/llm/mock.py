"""Deterministic mock LLM provider with fault injection.

This is a first-class subsystem, not a stub. It is what makes three otherwise
impossible things possible:

**Deterministic benchmarks.**
    Given the same scenario, the same 50+ scenarios produce the same trajectory
    every run, so a change in measured behaviour is a real regression rather than
    model variance.

**Failure-injection tests.**
    Timeouts, rate limits, malformed JSON, truncation, schema violations and
    provider outages are *requested*, at a chosen attempt number, rather than
    waited for.

**Honest latency figures.**
    Synthetic per-call latency is configurable and defaults to zero. Every
    benchmark figure derived from it is labelled as mock-provider latency, so
    engine wall-clock is never presented as real provider latency.

Response selection is by *rule*, matched against the request. Rules are matched
most-specific-first and each carries an optional response sequence, so a
scenario can say "the research agent's first call fails, its second succeeds".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from orchestration.domain.base import JsonDict
from orchestration.domain.enums import MessageRole, Provider
from orchestration.domain.model import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMRequest,
    LLMResponse,
    TokenUsage,
)
from orchestration.errors import (
    ConfigurationError,
    EngineTimeoutError,
    NetworkError,
    ProviderUnavailableError,
    RateLimitError,
)
from orchestration.llm.base import LLMProvider, estimate_tokens

#: The fault kinds a scenario can request.
FaultKind = Literal[
    "timeout",
    "rate_limit",
    "provider_unavailable",
    "network",
    "malformed_json",
    "truncated",
    "empty",
    "wrong_schema",
    "prose_instead_of_json",
]


@dataclass(slots=True)
class Fault:
    """A failure to inject on specific attempts.

    Attributes:
        kind: What goes wrong.
        attempts: 1-based attempt numbers to fail on. ``(1,)`` means "fail the
            first call, succeed on the retry", which is the shape most recovery
            tests need.
        retry_after: For ``rate_limit``, the hint to return.
    """

    kind: FaultKind
    attempts: tuple[int, ...] = (1,)
    retry_after: float | None = None

    def applies_to(self, attempt: int) -> bool:
        return attempt in self.attempts


@dataclass(slots=True)
class MockRule:
    """A response rule matched against an incoming request.

    Attributes:
        name: Identifies the rule in assertions and failure messages.
        match_system: Substring that must appear in the system message. This is
            how a rule targets one agent -- each agent's system prompt is
            distinctive.
        match_user: Substring that must appear in the latest user message.
        match_pattern: Regex applied to the whole rendered conversation.
        match_request_key: Substring of the request key, which the agent
            runtime sets to ``{execution}:{node}:{agent}:{iteration}``. This is
            the only reliable way to target one specific agent: derived agents
            share almost identical system prompts, so matching on prompt text
            cannot tell ``pricing_agent`` from ``research_agent``.
        responses: Content to return, one per call. The final entry repeats once
            exhausted, so a rule does not have to enumerate every possible call.
        fault: Failure to inject.
        latency_seconds: Synthetic delay per call.
        usage: Token counts to report; estimated from the text when omitted.
    """

    name: str
    match_system: str | None = None
    match_user: str | None = None
    match_pattern: str | None = None
    match_request_key: str | None = None
    responses: tuple[str, ...] = ()
    fault: Fault | None = None
    latency_seconds: float = 0.0
    usage: tuple[int, int] | None = None
    #: Higher wins when several rules match.
    priority: int = 0

    def specificity(self) -> int:
        """How many independent conditions this rule constrains.

        Used to order matches so a rule targeting one agent beats a catch-all,
        without the author having to set priorities by hand.
        """
        return sum(
            1
            for condition in (
                self.match_system,
                self.match_user,
                self.match_pattern,
                self.match_request_key,
            )
            if condition
        )

    def matches(
        self,
        system_text: str,
        user_text: str,
        full_text: str,
        request_key: str = "",
    ) -> bool:
        if self.match_system and self.match_system.lower() not in system_text.lower():
            return False
        if self.match_user and self.match_user.lower() not in user_text.lower():
            return False
        if self.match_request_key and self.match_request_key.lower() not in request_key.lower():
            return False
        return not (
            self.match_pattern and not re.search(self.match_pattern, full_text, re.IGNORECASE)
        )


@dataclass(slots=True)
class MockCall:
    """Record of one call, for test assertions."""

    index: int
    model_key: str
    rule: str | None
    system_preview: str
    user_preview: str
    response_preview: str
    fault: str | None
    input_tokens: int
    output_tokens: int
    requested_schema: str | None
    started_at: float
    latency_seconds: float


class MockProvider(LLMProvider):
    """Scripted, deterministic provider.

    Args:
        rules: Response rules, matched most-specific-first.
        default_response: Used when no rule matches. When ``None``, a
            deterministic reply is synthesised from the request -- so an
            unscripted call produces stable, schema-shaped output rather than
            an error, which keeps a scenario author from having to enumerate
            every incidental call.
        default_latency_seconds: Synthetic delay applied when a rule sets none.
        strict: When true, an unmatched request raises instead of being
            synthesised. Used by tests that assert exhaustive scripting.
    """

    provider = Provider.MOCK

    def __init__(
        self,
        rules: Sequence[MockRule] = (),
        *,
        default_response: str | None = None,
        default_latency_seconds: float = 0.0,
        strict: bool = False,
        seed: str = "mock",
    ) -> None:
        self._rules = sorted(rules, key=lambda r: (-r.priority, -r.specificity(), r.name))
        self._default_response = default_response
        self._default_latency = default_latency_seconds
        self._strict = strict
        self._seed = seed
        self._calls: list[MockCall] = []
        #: Per-rule call counters, so ``attempts`` in a Fault means what it says.
        self._rule_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    # -- inspection --------------------------------------------------------

    @property
    def calls(self) -> tuple[MockCall, ...]:
        return tuple(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def calls_for_rule(self, name: str) -> tuple[MockCall, ...]:
        return tuple(c for c in self._calls if c.rule == name)

    def reset(self) -> None:
        self._calls.clear()
        self._rule_counts.clear()

    def add_rule(self, rule: MockRule) -> None:
        self._rules = sorted(
            [*self._rules, rule], key=lambda r: (-r.priority, -r.specificity(), r.name)
        )

    # -- provider interface ------------------------------------------------

    async def complete(self, request: LLMRequest) -> LLMResponse:
        system_text = "\n".join(m.content for m in request.messages if m.role is MessageRole.SYSTEM)
        user_messages = [m.content for m in request.messages if m.role is MessageRole.USER]
        user_text = user_messages[-1] if user_messages else ""
        full_text = "\n".join(m.content for m in request.messages)

        rule = self._select_rule(system_text, user_text, full_text, request.request_key or "")

        async with self._lock:
            attempt = self._rule_counts.get(rule.name if rule else "__default__", 0) + 1
            self._rule_counts[rule.name if rule else "__default__"] = attempt

        latency = rule.latency_seconds if rule else self._default_latency
        started = time.perf_counter()
        if latency > 0:
            await asyncio.sleep(latency)

        fault = rule.fault if rule and rule.fault and rule.fault.applies_to(attempt) else None
        if fault is not None:
            self._record(request, rule, "", fault.kind, attempt, started, latency, faulted=True)
            self._raise_fault(fault, request)

        content = self._content_for(rule, attempt, request, fault)
        response = self._build_response(request, content, rule, started, latency)
        self._record(
            request,
            rule,
            content,
            None,
            attempt,
            started,
            latency,
            faulted=False,
            usage=(response.usage.input_tokens, response.usage.output_tokens),
        )
        return response

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Deterministic hash-based embeddings.

        Not semantically meaningful, but stable and unit-norm, which is exactly
        what a pgvector round-trip test and a reproducible benchmark need. Real
        semantic quality requires a real embedding model; pretending otherwise
        would make the retrieval numbers meaningless.
        """
        vectors = tuple(self._hash_vector(text, request.dimensions) for text in request.texts)
        return EmbeddingResponse(
            vectors=vectors,
            model_key=request.model.key,
            provider=Provider.MOCK,
            usage=TokenUsage(input_tokens=sum(estimate_tokens(t) for t in request.texts)),
            cost_usd=0.0,
        )

    def _hash_vector(self, text: str, dimensions: int) -> tuple[float, ...]:
        """Derive a unit-norm vector from a text digest."""
        raw: list[float] = []
        counter = 0
        while len(raw) < dimensions:
            digest = hashlib.sha256(f"{self._seed}:{counter}:{text}".encode()).digest()
            raw.extend(b / 255.0 - 0.5 for b in digest)
            counter += 1
        trimmed = raw[:dimensions]
        norm = math.sqrt(sum(v * v for v in trimmed)) or 1.0
        return tuple(round(v / norm, 8) for v in trimmed)

    # -- internals ---------------------------------------------------------

    def _select_rule(
        self, system_text: str, user_text: str, full_text: str, request_key: str = ""
    ) -> MockRule | None:
        for rule in self._rules:
            if rule.matches(system_text, user_text, full_text, request_key):
                return rule
        if self._strict:
            raise ConfigurationError(
                "mock provider is strict and no rule matched the request",
                system_preview=system_text[:120],
                user_preview=user_text[:120],
                known_rules=[r.name for r in self._rules],
            )
        return None

    def _content_for(
        self,
        rule: MockRule | None,
        attempt: int,
        request: LLMRequest,
        fault: Fault | None,
    ) -> str:
        if rule and rule.responses:
            # The last entry repeats, so a rule need not enumerate every call.
            index = min(attempt - 1, len(rule.responses) - 1)
            return rule.responses[index]
        if self._default_response is not None:
            return self._default_response
        return self._synthesise(request)

    def _synthesise(self, request: LLMRequest) -> str:
        """Build a deterministic reply shaped like whatever was asked for.

        When a schema is requested, the reply is generated *from that schema*, so
        an unscripted supervisor or agent call still yields something that
        validates. This keeps scenario authoring focused on the behaviour under
        test instead of every incidental call along the way.
        """
        if request.response_schema:
            return json.dumps(_synthesise_from_schema(request.response_schema, self._seed))
        digest = hashlib.sha256(
            f"{self._seed}:{request.messages[-1].content}".encode()
        ).hexdigest()[:8]
        return f"mock response {digest}"

    def _build_response(
        self,
        request: LLMRequest,
        content: str,
        rule: MockRule | None,
        started: float,
        latency: float,
    ) -> LLMResponse:
        if rule and rule.usage:
            input_tokens, output_tokens = rule.usage
        else:
            input_tokens = sum(estimate_tokens(m.content) for m in request.messages)
            output_tokens = estimate_tokens(content)
        return LLMResponse(
            content=content,
            model_key=request.model.key,
            provider=Provider.MOCK,
            usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            cost_usd=request.model.estimate_cost(input_tokens, output_tokens),
            finish_reason="stop",
            latency_seconds=round(time.perf_counter() - started, 6),
            raw_metadata={"mock_rule": rule.name if rule else None},
        )

    def _raise_fault(self, fault: Fault, request: LLMRequest) -> None:
        """Raise the requested failure, or return corrupt content for soft faults."""
        match fault.kind:
            case "timeout":
                raise EngineTimeoutError(
                    "mock provider timed out", provider="mock", model=request.model.key
                )
            case "rate_limit":
                raise RateLimitError(
                    "mock provider rate limited",
                    retry_after=fault.retry_after,
                    provider="mock",
                )
            case "provider_unavailable":
                raise ProviderUnavailableError("mock provider returned 503", provider="mock")
            case "network":
                raise NetworkError("mock provider connection reset", provider="mock")
            case _:
                # Content-level faults are not exceptions: the provider returns
                # successfully and the *caller* must cope with bad output. That
                # is a different failure mode from an outage and is tested as one.
                return

    def _record(
        self,
        request: LLMRequest,
        rule: MockRule | None,
        content: str,
        fault: str | None,
        attempt: int,
        started: float,
        latency: float,
        *,
        faulted: bool,
        usage: tuple[int, int] = (0, 0),
    ) -> None:
        schema_name = None
        if request.response_schema:
            schema_name = str(request.response_schema.get("title", "schema"))
        system = next((m.content for m in request.messages if m.role is MessageRole.SYSTEM), "")
        users = [m.content for m in request.messages if m.role is MessageRole.USER]
        self._calls.append(
            MockCall(
                index=len(self._calls) + 1,
                model_key=request.model.key,
                rule=rule.name if rule else None,
                system_preview=system[:160],
                user_preview=(users[-1] if users else "")[:160],
                response_preview=content[:160],
                fault=fault,
                input_tokens=usage[0],
                output_tokens=usage[1],
                requested_schema=schema_name,
                started_at=started,
                latency_seconds=round(time.perf_counter() - started, 6),
            )
        )


# ---------------------------------------------------------------------------
# Content-fault helpers
# ---------------------------------------------------------------------------

#: Canned bad outputs, so a scenario can name a corruption instead of writing it.
CONTENT_FAULT_PAYLOADS: dict[str, str] = {
    "malformed_json": '{"action": "delegate", "reason": "unterminated',
    "empty": "",
    "prose_instead_of_json": (
        "I think the best approach here would be to delegate this to the research "
        "agent, because it has the strongest search capabilities."
    ),
    "wrong_schema": '{"decision": "go", "who": ["research_agent"], "certainty": "high"}',
    "truncated": '{"action": "parallel_delegate", "reason": "fan out across three dim',
}


def content_fault_rule(
    name: str,
    kind: FaultKind,
    *,
    match_system: str | None = None,
    match_user: str | None = None,
    recovery_response: str | None = None,
) -> MockRule:
    """Build a rule that returns corrupt content once, then a good response.

    This is the shape needed to test the schema-repair path: the first reply
    cannot be validated, the second can.
    """
    bad = CONTENT_FAULT_PAYLOADS.get(kind, "")
    responses = (bad, recovery_response) if recovery_response else (bad,)
    return MockRule(
        name=name,
        match_system=match_system,
        match_user=match_user,
        responses=tuple(r for r in responses if r is not None),
    )


def _synthesise_from_schema(schema: JsonDict, seed: str, *, depth: int = 0) -> Any:
    """Generate a minimal value satisfying ``schema``.

    Handles the JSON Schema subset Pydantic emits for our models: objects with
    required properties, arrays, enums, ``anyOf`` unions, ``$ref`` into
    ``$defs``, and the scalar types. Anything unrecognised becomes ``None``,
    which will fail validation loudly rather than silently producing something
    plausible-but-wrong.
    """
    if depth > 6:
        return None

    defs = schema.get("$defs", {})
    return _synthesise_node(schema, defs, seed, depth)


def _synthesise_node(node: JsonDict, defs: JsonDict, seed: str, depth: int) -> Any:
    if depth > 6:
        return None

    if "$ref" in node:
        ref = str(node["$ref"]).rsplit("/", 1)[-1]
        target = defs.get(ref)
        if isinstance(target, dict):
            return _synthesise_node(target, defs, seed, depth + 1)
        return None

    if "const" in node:
        return node["const"]
    if "enum" in node and isinstance(node["enum"], list) and node["enum"]:
        return node["enum"][0]
    if "default" in node:
        return node["default"]

    for union_key in ("anyOf", "oneOf"):
        options = node.get(union_key)
        if isinstance(options, list):
            # Prefer a non-null branch: a union of "T or null" defaulting to null
            # would leave required content empty and fail the caller's validator.
            for option in options:
                if isinstance(option, dict) and option.get("type") != "null":
                    return _synthesise_node(option, defs, seed, depth + 1)
            return None

    node_type = node.get("type")
    if isinstance(node_type, list):
        node_type = next((t for t in node_type if t != "null"), None)

    match node_type:
        case "object":
            properties = node.get("properties", {})
            required = node.get("required", []) or list(properties)[:1]
            return {
                key: _synthesise_node(properties[key], defs, seed, depth + 1)
                for key in required
                if key in properties
            }
        case "array":
            item_schema = node.get("items")
            min_items = int(node.get("minItems", 0))
            if not isinstance(item_schema, dict) or min_items == 0:
                return []
            return [_synthesise_node(item_schema, defs, seed, depth + 1) for _ in range(min_items)]
        case "string":
            return f"mock-{hashlib.sha256(seed.encode()).hexdigest()[:6]}"
        case "integer":
            return int(node.get("minimum", 1))
        case "number":
            minimum = float(node.get("minimum", 0.0))
            maximum = float(node.get("maximum", 1.0))
            return round(min(max(0.5, minimum), maximum), 4)
        case "boolean":
            return False
        case "null":
            return None
        case _:
            return None


# ---------------------------------------------------------------------------
# Convenience builders for scenario authoring
# ---------------------------------------------------------------------------


def agent_output(
    content: str,
    *,
    confidence: float = 0.85,
    claims: Sequence[str] = (),
    evidence: Sequence[str] = (),
    gaps: Sequence[str] = (),
    data: JsonDict | None = None,
) -> str:
    """Serialise an :class:`AgentOutput`-shaped reply for a mock rule."""
    return json.dumps(
        {
            "content": content,
            "confidence": confidence,
            "claims": list(claims),
            "evidence": list(evidence),
            "gaps": list(gaps),
            "data": data or {},
        }
    )


def routing_decision(
    action: str,
    *,
    reason: str = "scripted decision",
    confidence: float = 0.9,
    agents: Sequence[str] = (),
    instructions: Sequence[str] = (),
    answer: str | None = None,
    retry_node_id: str | None = None,
    failure_reason: str | None = None,
    approval_action: str | None = None,
    approval_risk_reason: str | None = None,
) -> str:
    """Serialise a :class:`RoutingDecision`-shaped reply for a mock rule."""
    payload: JsonDict = {"action": action, "reason": reason, "confidence": confidence}
    if agents:
        payload["targets"] = [
            {
                "agent_id": agent,
                "instruction": instructions[i] if i < len(instructions) else f"work on {agent}",
            }
            for i, agent in enumerate(agents)
        ]
    if answer is not None:
        payload["answer"] = answer
    if retry_node_id is not None:
        payload["retry_node_id"] = retry_node_id
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    if approval_action is not None:
        payload["approval_action"] = approval_action
    if approval_risk_reason is not None:
        payload["approval_risk_reason"] = approval_risk_reason
    return json.dumps(payload)


@dataclass(slots=True)
class MockScript:
    """A named collection of rules, so a scenario reads as a script.

    Sugar over a rule list, but it is the difference between a benchmark file
    that reads like a specification and one that reads like plumbing.
    """

    rules: list[MockRule] = field(default_factory=list)

    def on_agent(
        self,
        agent_marker: str,
        *responses: str,
        fault: Fault | None = None,
        latency_seconds: float = 0.0,
        name: str | None = None,
    ) -> MockScript:
        """Script the replies for the agent whose prompt contains ``agent_marker``."""
        self.rules.append(
            MockRule(
                name=name or f"agent:{agent_marker}",
                match_system=agent_marker,
                responses=responses,
                fault=fault,
                latency_seconds=latency_seconds,
            )
        )
        return self

    def on_agent_id(
        self,
        agent_id: str,
        *responses: str,
        fault: Fault | None = None,
        latency_seconds: float = 0.0,
    ) -> MockScript:
        """Script replies for one specific agent, matched by id.

        Preferred over :meth:`on_agent` whenever derived agents are in play: they
        share a system prompt, so only the request key distinguishes them.
        """
        self.rules.append(
            MockRule(
                name=f"agent_id:{agent_id}",
                match_request_key=f":{agent_id}:",
                responses=responses,
                fault=fault,
                latency_seconds=latency_seconds,
                priority=5,
            )
        )
        return self

    def on_supervisor(
        self,
        *responses: str,
        fault: Fault | None = None,
        latency_seconds: float = 0.0,
    ) -> MockScript:
        """Script the supervisor's successive routing decisions."""
        self.rules.append(
            MockRule(
                name="supervisor",
                match_system="supervisor",
                responses=responses,
                fault=fault,
                latency_seconds=latency_seconds,
                priority=10,
            )
        )
        return self

    def build(self, *, default_latency_seconds: float = 0.0, strict: bool = False) -> MockProvider:
        return MockProvider(
            self.rules, default_latency_seconds=default_latency_seconds, strict=strict
        )


#: Type of a factory that builds a provider for one benchmark scenario.
ProviderFactory = Callable[[], LLMProvider]
