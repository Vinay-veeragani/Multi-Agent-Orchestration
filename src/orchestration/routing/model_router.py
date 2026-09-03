"""Model router.

Selects a :class:`ModelConfig` for a call, given what the caller needs. The
design brief here was explicitly "do not over-engineer", and the implementation
takes that seriously: filter to viable candidates, then order them by one of four
named preferences. There is no learned policy, no bandit, no latency feedback
loop -- those need production traffic to be anything other than decoration.

What it does provide is the *seam*. Every LLM call in the engine goes through
:meth:`ModelRouter.select`, so replacing this policy later touches one class.

Every selection returns a :class:`ModelSelection` carrying the reason and the
candidates considered, so a routing choice is visible in traces rather than being
an unexplained model name in a log line.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestration.domain.enums import ModelCapability, TaskComplexity
from orchestration.domain.model import ModelConfig, ModelSelection, RoutingCriteria
from orchestration.errors import ConfigurationError, NotFoundError
from orchestration.models.catalog import ModelCatalog

#: Latency profile ordering, for the "fastest" preference.
_LATENCY_ORDER: dict[str, int] = {"fast": 0, "standard": 1, "slow": 2}

#: Capabilities implied by each complexity level. Complexity is a hint from the
#: caller; this turns it into concrete filter criteria.
_COMPLEXITY_REQUIREMENTS: dict[TaskComplexity, frozenset[ModelCapability]] = {
    TaskComplexity.TRIVIAL: frozenset(),
    TaskComplexity.SIMPLE: frozenset(),
    TaskComplexity.MODERATE: frozenset(),
    TaskComplexity.COMPLEX: frozenset({ModelCapability.REASONING}),
}

#: Preference applied when the caller only supplies a complexity.
_COMPLEXITY_PREFERENCE: dict[TaskComplexity, str] = {
    TaskComplexity.TRIVIAL: "cheapest",
    TaskComplexity.SIMPLE: "cheapest",
    TaskComplexity.MODERATE: "balanced",
    TaskComplexity.COMPLEX: "most_capable",
}


def _blended_cost(model: ModelConfig) -> float:
    """Single cost figure for ranking.

    Weighted 1:3 input to output, approximating a typical agent turn where the
    prompt (task plus prior outputs) is larger than the completion but output
    tokens cost several times more. A crude proxy, but a stable one, and it puts
    models in the right order.
    """
    return model.input_cost_per_mtok + 3.0 * model.output_cost_per_mtok


def _capability_weight(model: ModelConfig) -> int:
    """Rough capability score for the "most_capable" preference."""
    score = 0
    if model.has(ModelCapability.REASONING):
        score += 4
    if model.has(ModelCapability.LONG_CONTEXT):
        score += 2
    if model.has(ModelCapability.TOOL_USE):
        score += 1
    if model.has(ModelCapability.STRUCTURED_OUTPUT):
        score += 1
    if model.has(ModelCapability.VISION):
        score += 1
    return score


class ModelRouter:
    """Chooses a model per call from a catalog.

    Args:
        catalog: Available models.
        default_model_key: Fallback when no criteria narrow the field.
        allowed_providers: Restrict routing to providers that are actually
            configured. Without this the router would happily return an OpenAI
            model on an install with no OpenAI credential, turning a
            configuration problem into a confusing runtime failure.
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        *,
        default_model_key: str | None = None,
        allowed_providers: Sequence[str] | None = None,
        force_model_key: str | None = None,
    ) -> None:
        self._catalog = catalog
        self._allowed = frozenset(allowed_providers) if allowed_providers else None
        self._default_key = default_model_key or self._pick_initial_default()
        #: Operator override: every call, supervisor decisions included,
        #: returns this model regardless of capability/cost criteria. Distinct
        #: from `default_model_key` (an unused-by-selection fallback) -- this
        #: one actually participates in `select()`, deliberately bypassing the
        #: cost-aware heuristics below it so a deployment that wants a specific
        #: real model used everywhere (rather than the router silently
        #: preferring the free mock model whenever it is capable enough) gets
        #: exactly that.
        self._force_model_key = force_model_key
        if force_model_key is not None:
            self._catalog.get(force_model_key)  # fail fast if misconfigured

    def _pick_initial_default(self) -> str:
        candidates = self._available()
        if not candidates:
            raise ConfigurationError(
                "model catalog contains no usable chat models",
                allowed_providers=sorted(self._allowed) if self._allowed else None,
            )
        # Cheapest viable model as the default: an unconfigured caller should not
        # accidentally be routed to the most expensive option.
        return min(candidates, key=lambda m: (_blended_cost(m), m.key)).key

    @property
    def default_model(self) -> ModelConfig:
        return self._catalog.get(self._default_key)

    @property
    def catalog(self) -> ModelCatalog:
        return self._catalog

    def _available(self) -> tuple[ModelConfig, ...]:
        models = self._catalog.chat_models()
        if self._allowed is not None:
            models = tuple(m for m in models if m.provider.value in self._allowed)
        return models

    # -- selection ---------------------------------------------------------

    def select(
        self,
        criteria: RoutingCriteria | None = None,
        *,
        complexity: TaskComplexity | None = None,
        estimated_input_tokens: int = 0,
    ) -> ModelSelection:
        """Choose a model.

        Args:
            criteria: What the caller requires. Defaults to no constraints.
            complexity: Optional task-complexity hint, used only where the
                criteria do not already settle the matter.
            estimated_input_tokens: Used to exclude models whose context window
                cannot hold the request.

        Raises:
            NotFoundError: If a pinned model is not in the catalog.
            ConfigurationError: If nothing satisfies the criteria. The error
                lists what was required and what was rejected -- an unsatisfiable
                routing request is a configuration bug, and it should say so.
        """
        criteria = criteria or RoutingCriteria()

        if self._force_model_key:
            model = self._catalog.get(self._force_model_key)
            return ModelSelection(
                model=model,
                reason=f"operator-forced to {model.key!r} (ORCH_PINNED_MODEL_KEY)",
                considered=(model.key,),
                criteria=criteria,
            )

        if criteria.pinned_model:
            model = self._catalog.try_get(criteria.pinned_model)
            if model is None:
                raise NotFoundError(
                    f"pinned model {criteria.pinned_model!r} is not in the catalog",
                    model=criteria.pinned_model,
                    available=list(self._catalog.keys()),
                )
            return ModelSelection(
                model=model,
                reason=f"pinned by caller to {model.key!r}",
                considered=(model.key,),
                criteria=criteria,
            )

        required = set(criteria.required_capabilities)
        if complexity is not None:
            required |= _COMPLEXITY_REQUIREMENTS[complexity]

        candidates = list(self._available())
        considered = tuple(m.key for m in candidates)
        rejections: list[str] = []

        if criteria.require_local:
            kept = [m for m in candidates if m.has(ModelCapability.LOCAL)]
            if not kept:
                rejections.append("no local models available")
            candidates = kept

        if required:
            kept = [m for m in candidates if m.has(*required)]
            if not kept and candidates:
                rejections.append(
                    "no model has all of: " + ", ".join(sorted(c.value for c in required))
                )
            candidates = kept

        if criteria.min_context_limit is not None:
            kept = [m for m in candidates if m.context_limit >= criteria.min_context_limit]
            if not kept and candidates:
                rejections.append(f"no model has a context window >= {criteria.min_context_limit}")
            candidates = kept

        if estimated_input_tokens > 0:
            # Leave headroom for the completion itself, otherwise a model whose
            # window exactly fits the prompt gets selected and then truncates.
            kept = [
                m
                for m in candidates
                if m.context_limit >= estimated_input_tokens + m.max_output_tokens
            ]
            if not kept and candidates:
                rejections.append(
                    f"no model can hold {estimated_input_tokens} input tokens plus its output"
                )
            candidates = kept

        if criteria.max_cost_per_mtok is not None:
            kept = [m for m in candidates if _blended_cost(m) <= criteria.max_cost_per_mtok * 4.0]
            if not kept and candidates:
                rejections.append(
                    f"no model within the cost ceiling of {criteria.max_cost_per_mtok}/Mtok"
                )
            candidates = kept

        if not candidates:
            raise ConfigurationError(
                "no model satisfies the routing criteria",
                required_capabilities=sorted(c.value for c in required),
                require_local=criteria.require_local,
                min_context_limit=criteria.min_context_limit,
                considered=list(considered),
                rejections=rejections,
            )

        preference = criteria.prefer
        if complexity is not None and preference == "balanced":
            preference = _COMPLEXITY_PREFERENCE[complexity]  # type: ignore[assignment]

        chosen = self._order(candidates, preference)[0]
        return ModelSelection(
            model=chosen,
            reason=self._explain(chosen, preference, required, complexity),
            considered=considered,
            criteria=criteria,
        )

    def _order(self, candidates: list[ModelConfig], preference: str) -> list[ModelConfig]:
        """Sort candidates by the named preference.

        Every sort key ends with ``m.key`` so ordering is total and stable: two
        models with identical cost must not swap places between runs, or the
        benchmark would show phantom differences.
        """
        match preference:
            case "cheapest":
                return sorted(candidates, key=lambda m: (_blended_cost(m), m.key))
            case "fastest":
                return sorted(
                    candidates,
                    key=lambda m: (
                        _LATENCY_ORDER.get(m.latency_profile, 1),
                        _blended_cost(m),
                        m.key,
                    ),
                )
            case "most_capable":
                return sorted(
                    candidates,
                    key=lambda m: (-_capability_weight(m), -m.context_limit, m.key),
                )
            case _:
                # "balanced": prefer capability per unit cost, with free/local
                # models treated as very cheap rather than infinitely good.
                return sorted(
                    candidates,
                    key=lambda m: (
                        -(_capability_weight(m) + 1) / (_blended_cost(m) + 1.0),
                        m.key,
                    ),
                )

    def _explain(
        self,
        model: ModelConfig,
        preference: str,
        required: set[ModelCapability],
        complexity: TaskComplexity | None,
    ) -> str:
        parts = [f"{preference} preference selected {model.key!r}"]
        if complexity is not None:
            parts.append(f"complexity={complexity.value}")
        if required:
            parts.append("requires " + ",".join(sorted(c.value for c in required)))
        if model.is_free:
            parts.append("no marginal cost")
        else:
            parts.append(f"~${_blended_cost(model):.2f}/Mtok blended")
        return "; ".join(parts)

    # -- embeddings --------------------------------------------------------

    def select_embedding_model(self, *, require_local: bool = False) -> ModelSelection:
        """Choose an embedding model for the pgvector evidence store."""
        candidates = list(self._catalog.embedding_models())
        if self._allowed is not None:
            candidates = [m for m in candidates if m.provider.value in self._allowed]
        if require_local:
            candidates = [m for m in candidates if m.has(ModelCapability.LOCAL)]
        if not candidates:
            raise ConfigurationError(
                "no embedding model is available",
                allowed_providers=sorted(self._allowed) if self._allowed else None,
                require_local=require_local,
            )
        chosen = min(candidates, key=lambda m: (_blended_cost(m), m.key))
        return ModelSelection(
            model=chosen,
            reason=f"cheapest available embedding model ({chosen.key!r})",
            considered=tuple(m.key for m in candidates),
        )


def build_default_router(
    *,
    mock_only: bool = True,
    configured_providers: Sequence[str] | None = None,
    force_model_key: str | None = None,
) -> ModelRouter:
    """Construct a router.

    Defaults to mock-only, which is the safe default: a fresh install with no
    credentials gets a working engine rather than an authentication error, and no
    test can trigger a billable call by accident.
    """
    from orchestration.models.catalog import build_catalog

    catalog = build_catalog(mock_only=mock_only)
    providers = (
        list(configured_providers) if configured_providers else (["mock"] if mock_only else None)
    )
    return ModelRouter(catalog, allowed_providers=providers, force_model_key=force_model_key)
