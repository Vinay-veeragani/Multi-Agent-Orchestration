"""Condition evaluation and template rendering.

Workflow definitions arrive from YAML files, HTTP requests, and replanning LLMs.
All three are untrusted input, so neither conditions nor templates may execute
code. Both are therefore deliberately limited:

**Conditions** are a closed operator set applied to a dotted path lookup. There
is no expression parser, no ``eval``, and no way to reach outside the evaluation
context the execution state chooses to expose.

**Templates** substitute ``{path}`` placeholders by the same dotted lookup.
Python's own ``str.format`` is not used, because ``"{x.__class__}"`` reaches
attributes and ``"{0.__globals__}"`` is a known sandbox escape.

A path that does not resolve is a *miss*, not an error: a condition on a value
that has not been produced yet is false, and a template placeholder for a missing
value renders as an empty marker. Raising instead would make an ordinary
mid-execution state look like a workflow bug.
"""

from __future__ import annotations

import re
from typing import Any, Final

from orchestration.domain.base import JsonDict
from orchestration.domain.workflow import Condition, NodeCondition

#: Sentinel distinguishing "resolved to None" from "path did not resolve".
_MISSING: Final = object()

#: Matches a ``{dotted.path}`` placeholder.
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.\[\]-]*)\}")

#: Guard against a pathological path from a generated workflow.
MAX_PATH_DEPTH: Final[int] = 12


def resolve_path(context: JsonDict, path: str) -> Any:
    """Resolve a dotted path against ``context``.

    Supports mapping keys, list indices (``results.0.title`` or
    ``results[0].title``), and nothing else. In particular it never touches
    Python attributes, which is what keeps a workflow definition from reaching
    engine internals.

    Returns :data:`_MISSING` when the path does not resolve.
    """
    segments = _split_path(path)
    if not segments or len(segments) > MAX_PATH_DEPTH:
        return _MISSING

    current: Any = context
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list | tuple):
            if not segment.lstrip("-").isdigit():
                return _MISSING
            index = int(segment)
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _split_path(path: str) -> list[str]:
    """Split ``a.b[0].c`` into ``["a", "b", "0", "c"]``."""
    normalised = path.replace("[", ".").replace("]", "")
    return [segment for segment in normalised.split(".") if segment]


def evaluate_condition(condition: Condition, context: JsonDict) -> bool:
    """Evaluate one predicate.

    An unresolvable path makes every operator false except ``not_exists`` and
    ``falsy`` -- which is the intuitive reading: a value that does not exist is
    not greater than anything, but it is indeed absent.
    """
    value = resolve_path(context, condition.path)
    missing = value is _MISSING

    match condition.operator:
        case "exists":
            return not missing
        case "not_exists":
            return missing
        case "truthy":
            return not missing and bool(value)
        case "falsy":
            return missing or not bool(value)

    if missing:
        return False

    expected = condition.value

    match condition.operator:
        case "eq":
            return _loose_equal(value, expected)
        case "ne":
            return not _loose_equal(value, expected)
        case "lt" | "lte" | "gt" | "gte":
            return _compare(condition.operator, value, expected)
        case "in":
            return _contains(expected, value)
        case "not_in":
            return not _contains(expected, value)
        case _:
            # "contains" is the only remaining member of the closed operator
            # Literal, so this arm is exhaustive rather than a fallback.
            return _contains(value, expected)


def _loose_equal(left: Any, right: Any) -> bool:
    """Equality that treats ``1`` and ``1.0`` as equal but not ``True`` and ``1``.

    JSON round-trips turn integers into floats, so a strict comparison would make
    a condition behave differently before and after a checkpoint. Booleans are
    excluded from the numeric path because ``True == 1`` is almost never what a
    workflow author meant.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right or left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return float(left) == float(right)
    return bool(left == right)


def _compare(operator: str, left: Any, right: Any) -> bool:
    """Ordered comparison, false when the operands are not comparable.

    Returning false rather than raising: a condition comparing a string to a
    number is a workflow authoring mistake, but discovering it as a crash
    mid-execution is worse than the branch simply not being taken. Graph
    validation is where authoring mistakes should surface.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        # Fall back to string ordering when both are strings.
        if isinstance(left, str) and isinstance(right, str):
            return _apply_order(operator, left, right)
        return False
    return _apply_order(operator, float(left), float(right))


def _apply_order(operator: str, left: Any, right: Any) -> bool:
    match operator:
        case "lt":
            return bool(left < right)
        case "lte":
            return bool(left <= right)
        case "gt":
            return bool(left > right)
        case "gte":
            return bool(left >= right)
        case _:  # pragma: no cover
            return False


def _contains(container: Any, item: Any) -> bool:
    """Membership test that never raises on an unsupported container."""
    if isinstance(container, str):
        return str(item) in container
    if isinstance(container, dict):
        return item in container
    if isinstance(container, list | tuple | set | frozenset):
        return any(_loose_equal(element, item) for element in container)
    return False


def evaluate_group(group: NodeCondition | None, context: JsonDict) -> bool:
    """Evaluate a condition group.

    An empty or absent group is ``True`` -- an unconditional edge is always active,
    which is what makes conditions optional throughout the graph.
    """
    if group is None or group.is_empty:
        return True
    results = (evaluate_condition(c, context) for c in group.conditions)
    return all(results) if group.mode == "all" else any(results)


def explain_group(group: NodeCondition | None, context: JsonDict) -> str:
    """Human-readable evaluation trace, for events and debugging.

    Conditional routing is otherwise the hardest part of an execution to
    understand after the fact: "why did it take that branch" needs the resolved
    values, not just the predicate.
    """
    if group is None or group.is_empty:
        return "always (no conditions)"
    parts: list[str] = []
    for condition in group.conditions:
        value = resolve_path(context, condition.path)
        rendered = "<missing>" if value is _MISSING else repr(value)
        outcome = evaluate_condition(condition, context)
        parts.append(
            f"{condition.path}={rendered} {condition.operator} {condition.value!r} -> {outcome}"
        )
    joiner = " AND " if group.mode == "all" else " OR "
    return joiner.join(parts) + f" => {evaluate_group(group, context)}"


def render_template(template: str, context: JsonDict, *, keep_missing: bool = False) -> str:
    """Substitute ``{dotted.path}`` placeholders from ``context``.

    Not ``str.format``: that resolves attributes, so a template from an untrusted
    workflow could read ``{task.__class__.__mro__}`` and worse. This resolves only
    mapping keys and list indices.

    Args:
        keep_missing: When true an unresolved placeholder is left verbatim;
            otherwise it renders as ``[missing: path]`` so the gap is visible in
            the prompt rather than silently blank.
    """

    def _replace(match: re.Match[str]) -> str:
        path = match.group(1)
        value = resolve_path(context, path)
        if value is _MISSING:
            return match.group(0) if keep_missing else f"[missing: {path}]"
        if isinstance(value, str):
            return value
        if isinstance(value, dict | list | tuple):
            import json

            return json.dumps(value, default=str)
        return str(value)

    return _PLACEHOLDER_RE.sub(_replace, template)


def template_paths(template: str) -> tuple[str, ...]:
    """Paths a template references, for validating a workflow before it runs."""
    return tuple(dict.fromkeys(match.group(1) for match in _PLACEHOLDER_RE.finditer(template)))


def unresolved_paths(template: str, context: JsonDict) -> tuple[str, ...]:
    """Which of a template's paths do not resolve against ``context``."""
    return tuple(
        path for path in template_paths(template) if resolve_path(context, path) is _MISSING
    )
