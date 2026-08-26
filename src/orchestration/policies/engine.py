"""Policy engine: the authorisation decision for every tool invocation.

This is the enforcement point. Not the prompt, not the model's cooperation, not
the agent definition alone -- every tool call passes through
:meth:`PolicyEngine.evaluate` and receives one of three effects:

``ALLOW``
    Proceed.
``DENY``
    Refuse. The agent is told, and can adapt.
``REQUIRE_APPROVAL``
    Suspend the execution until a human decides.

Evaluation is layered, and each layer can only ever *narrow* the outcome, never
widen it. That ordering is what makes the result auditable: a decision can be
explained by naming the first rule that constrained it.

1. Is the tool registered and enabled?
2. Is it on the agent's allowlist? (Deny-by-default: absence means denial.)
3. Does the agent's own permission entry deny it, or cap its call count?
4. Do the argument constraints hold?
5. Does its risk level, or an explicit flag, require human approval?
6. Do any deployment-wide rules apply?
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass, field

from orchestration.agents.registry import AgentRegistry
from orchestration.domain.base import JsonDict
from orchestration.domain.enums import PolicyEffect, RiskLevel
from orchestration.domain.tool import (
    DEFAULT_APPROVAL_RISK_LEVELS,
    SENSITIVE_ARGUMENT_KEYS,
    ToolPermission,
)
from orchestration.errors import ConfigurationError, NotFoundError
from orchestration.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The outcome of evaluating one tool invocation.

    Carries the rule that produced it, so an audit log answers "why" and not
    merely "what".
    """

    effect: PolicyEffect
    reason: str
    #: Identifies the layer that decided, e.g. ``"allowlist"`` or ``"risk"``.
    rule: str
    risk_level: RiskLevel = RiskLevel.SAFE
    #: Arguments after any constraint-driven narrowing.
    arguments: JsonDict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.effect is PolicyEffect.ALLOW

    @property
    def denied(self) -> bool:
        return self.effect is PolicyEffect.DENY

    @property
    def needs_approval(self) -> bool:
        return self.effect is PolicyEffect.REQUIRE_APPROVAL

    def as_event_payload(self) -> JsonDict:
        return {
            "effect": self.effect.value,
            "rule": self.rule,
            "reason": self.reason,
            "risk_level": self.risk_level.value,
        }


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """A deployment-wide rule, applied after per-agent permissions.

    Patterns use shell globbing, so ``agent_pattern="*_agent"`` and
    ``tool_pattern="write_*"`` read the way an operator expects.
    """

    name: str
    effect: PolicyEffect
    reason: str
    tool_pattern: str = "*"
    agent_pattern: str = "*"
    #: Only applies at or above this risk level.
    min_risk: RiskLevel | None = None

    def applies_to(self, agent_id: str, tool: str, risk: RiskLevel) -> bool:
        if not fnmatch.fnmatch(tool, self.tool_pattern):
            return False
        if not fnmatch.fnmatch(agent_id, self.agent_pattern):
            return False
        return not (self.min_risk is not None and _RISK_ORDER[risk] < _RISK_ORDER[self.min_risk])


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.SAFE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class PolicyEngine:
    """Authorises tool invocations.

    Args:
        agents: Source of per-agent allowlists.
        tools: Source of tool specs and enabled state.
        approval_risk_levels: Risk levels escalated to a human. Defaults to
            HIGH and CRITICAL.
        rules: Deployment-wide rules applied after per-agent permissions.
        call_counts: Per-execution tally of agent/tool calls, for ``max_calls``.
            Injected so it can be restored from a checkpoint -- otherwise a
            resumed execution would silently reset every call ceiling.
    """

    def __init__(
        self,
        *,
        agents: AgentRegistry,
        tools: ToolRegistry,
        approval_risk_levels: frozenset[RiskLevel] = DEFAULT_APPROVAL_RISK_LEVELS,
        rules: Sequence[PolicyRule] = (),
        call_counts: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self._agents = agents
        self._tools = tools
        self._approval_levels = approval_risk_levels
        self._rules = tuple(rules)
        self._call_counts: dict[tuple[str, str], int] = (
            call_counts if call_counts is not None else {}
        )

    @property
    def call_counts(self) -> dict[tuple[str, str], int]:
        """Live tally, so the executor can checkpoint it."""
        return self._call_counts

    # -- evaluation --------------------------------------------------------

    def evaluate(self, agent_id: str, tool: str, arguments: JsonDict) -> PolicyDecision:
        """Authorise one tool invocation.

        Never raises for a policy outcome -- a denial is a returned decision, not
        an exception, because the agent is expected to receive it and adapt.
        Configuration errors (an unknown agent) still raise, since those are bugs
        rather than expected outcomes.
        """
        definition = self._agents.try_get(agent_id)
        if definition is None:
            raise NotFoundError(f"policy evaluation for unknown agent {agent_id!r}", agent=agent_id)

        # Layer 1: does the tool exist and is it usable at all?
        if not self._tools.has(tool):
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=f"tool {tool!r} is not registered in this deployment",
                rule="unknown_tool",
            )
        spec = self._tools.get_spec(tool)
        if not self._tools.is_enabled(tool):
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=f"tool {tool!r} is disabled in this deployment",
                rule="tool_disabled",
                risk_level=spec.risk,
            )

        # Layer 2: deny-by-default allowlist.
        permission = definition.permission_for(tool)
        if permission is None:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=(
                    f"agent {agent_id!r} is not permitted to use {tool!r}; "
                    f"its allowlist is: {', '.join(sorted(definition.tool_names)) or 'empty'}"
                ),
                rule="allowlist",
                risk_level=spec.risk,
            )

        # Layer 3: the agent's own permission entry.
        if permission.effect is PolicyEffect.DENY:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=permission.reason or f"agent {agent_id!r} denies {tool!r} explicitly",
                rule="permission_effect",
                risk_level=spec.risk,
            )

        exhausted = self._check_call_ceiling(agent_id, tool, permission)
        if exhausted is not None:
            return exhausted

        # Layer 4: argument constraints.
        violation = self._check_constraints(permission, arguments)
        if violation is not None:
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=violation,
                rule="argument_constraint",
                risk_level=spec.risk,
            )

        # Layer 5 and 6 both escalate rather than allow, and the stricter wins.
        requires_approval = (
            permission.effect is PolicyEffect.REQUIRE_APPROVAL
            or spec.requires_approval
            or spec.risk in self._approval_levels
        )

        for rule in self._rules:
            if not rule.applies_to(agent_id, tool, spec.risk):
                continue
            if rule.effect is PolicyEffect.DENY:
                return PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason=f"{rule.reason} (rule: {rule.name})",
                    rule=f"deployment_rule:{rule.name}",
                    risk_level=spec.risk,
                )
            if rule.effect is PolicyEffect.REQUIRE_APPROVAL:
                requires_approval = True

        if requires_approval:
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reason=self._approval_reason(agent_id, tool, spec.risk, permission),
                rule="risk",
                risk_level=spec.risk,
                arguments=redact_arguments(arguments),
            )

        return PolicyDecision(
            effect=PolicyEffect.ALLOW,
            reason=f"{tool!r} is allowlisted for {agent_id!r} at risk {spec.risk.value}",
            rule="allowlist",
            risk_level=spec.risk,
            arguments=arguments,
        )

    def record_call(self, agent_id: str, tool: str) -> int:
        """Increment the call tally after a permitted invocation."""
        key = (agent_id, tool)
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        return self._call_counts[key]

    def calls_made(self, agent_id: str, tool: str) -> int:
        return self._call_counts.get((agent_id, tool), 0)

    # -- layers ------------------------------------------------------------

    def _check_call_ceiling(
        self, agent_id: str, tool: str, permission: ToolPermission
    ) -> PolicyDecision | None:
        if permission.max_calls is None:
            return None
        used = self.calls_made(agent_id, tool)
        if used < permission.max_calls:
            return None
        return PolicyDecision(
            effect=PolicyEffect.DENY,
            reason=(
                f"agent {agent_id!r} has used its {permission.max_calls} permitted {tool!r} call(s)"
            ),
            rule="max_calls",
        )

    @staticmethod
    def _check_constraints(permission: ToolPermission, arguments: JsonDict) -> str | None:
        """Apply argument constraints, returning a violation message or ``None``.

        A small, closed constraint vocabulary. Deliberately not a general
        expression language: constraints come from agent definitions that may be
        registered over the API, and a general evaluator there would be a code
        execution path.
        """
        for field_name, constraint in permission.constraints.items():
            if not isinstance(constraint, dict):
                continue
            value = arguments.get(field_name)
            if value is None:
                # A constraint on an absent argument cannot be violated. The
                # tool's own schema is what enforces presence.
                continue
            text = str(value)

            prefix = constraint.get("prefix")
            if isinstance(prefix, str) and not _path_has_prefix(text, prefix):
                return f"argument {field_name}={text!r} must start with {prefix!r} for this agent"

            forbidden = constraint.get("not_prefix")
            if isinstance(forbidden, str) and _path_has_prefix(text, forbidden):
                return f"argument {field_name}={text!r} must not start with {forbidden!r}"

            pattern = constraint.get("pattern")
            if isinstance(pattern, str) and not fnmatch.fnmatch(text, pattern):
                return f"argument {field_name}={text!r} must match {pattern!r}"

            allowed = constraint.get("one_of")
            if isinstance(allowed, list) and text not in {str(a) for a in allowed}:
                return f"argument {field_name}={text!r} must be one of {allowed}"

            maximum = constraint.get("max")
            if (
                isinstance(maximum, int | float)
                and isinstance(value, int | float)
                and value > maximum
            ):
                return f"argument {field_name}={value} exceeds the maximum of {maximum}"

        return None

    @staticmethod
    def _approval_reason(
        agent_id: str, tool: str, risk: RiskLevel, permission: ToolPermission
    ) -> str:
        if permission.reason:
            return (
                f"{tool!r} requires human approval (risk: {risk.value}). "
                f"Agent note: {permission.reason}"
            )
        return (
            f"agent {agent_id!r} requested {tool!r}, which is classified "
            f"{risk.value} risk and requires human approval before execution"
        )


def _path_has_prefix(value: str, prefix: str) -> bool:
    """Whether ``value`` lies under ``prefix``, comparing normalised paths.

    Normalises separators and strips ``./`` so ``analysis/x.csv``,
    ``./analysis/x.csv`` and ``analysis\\x.csv`` all satisfy a prefix of
    ``analysis``. Without this the same constraint would behave differently on
    Windows and Linux, which is exactly the kind of platform-dependent security
    behaviour to avoid.

    Traversal is *not* handled here -- that is the tool sandbox's job, and it
    resolves paths properly. This is a narrowing constraint, not a boundary.
    """
    normalised = value.replace("\\", "/").removeprefix("./")
    target = prefix.replace("\\", "/").removeprefix("./").rstrip("/")
    if target in {"", "."}:
        return True
    return normalised == target or normalised.startswith(f"{target}/")


def redact_arguments(
    arguments: JsonDict, sensitive: frozenset[str] = SENSITIVE_ARGUMENT_KEYS
) -> JsonDict:
    """Mask sensitive values before arguments reach an approval record or event.

    Matches on a key *containing* a sensitive term, not equalling it, so
    ``openai_api_key`` and ``db_password`` are caught alongside ``api_key``.
    """
    redacted: JsonDict = {}
    for key, value in arguments.items():
        lowered = key.lower()
        if any(term in lowered for term in sensitive):
            redacted[key] = "***"
        elif isinstance(value, dict):
            redacted[key] = redact_arguments(value, sensitive)
        else:
            redacted[key] = value
    return redacted


#: Rules that make the shipped default configuration defensible. Registered by
#: :func:`build_default_policy_engine` unless a caller supplies its own.
DEFAULT_RULES: tuple[PolicyRule, ...] = (
    PolicyRule(
        name="critical_always_approved",
        effect=PolicyEffect.REQUIRE_APPROVAL,
        reason="critical-risk tools always require a human decision",
        min_risk=RiskLevel.CRITICAL,
    ),
    PolicyRule(
        name="no_production_database",
        effect=PolicyEffect.DENY,
        reason="agents may not query a production database",
        tool_pattern="production_*",
    ),
)


def build_default_policy_engine(
    *,
    agents: AgentRegistry,
    tools: ToolRegistry,
    extra_rules: Sequence[PolicyRule] = (),
) -> PolicyEngine:
    """Construct a policy engine with the default deployment rules."""
    if not isinstance(agents, AgentRegistry):  # pragma: no cover - defensive
        raise ConfigurationError("policy engine requires an AgentRegistry")
    return PolicyEngine(
        agents=agents,
        tools=tools,
        rules=(*DEFAULT_RULES, *extra_rules),
    )
