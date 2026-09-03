"""``/providers`` -- LLM provider credentials, set live from the UI.

Distinct from `.env`/``Settings``: those are read once at process start, so a
credential entered here must live somewhere a running process can pick up
without a restart. See ``ProviderCredentialRepository`` and
``AppState.reload_providers``, which this router calls after every write.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, status

from orchestration.api.schemas import (
    ModelOption,
    ProviderInfo,
    ProvidersPageResponse,
    SetActiveProviderRequest,
    UpdateProviderRequest,
)
from orchestration.api.security import get_app_state, require_api_key
from orchestration.api.state import AppState
from orchestration.domain.enums import Provider
from orchestration.errors import InputValidationError, NotFoundError
from orchestration.llm.factory import configured_providers
from orchestration.models.catalog import build_catalog
from orchestration.persistence.repositories import (
    ProviderCredentialRepository,
    RoutingSettingsRepository,
)

router = APIRouter(prefix="/providers", tags=["providers"], dependencies=[Depends(require_api_key)])


@dataclass(frozen=True, slots=True)
class _ProviderMeta:
    name: str
    label: str
    #: Attribute name on Settings holding the SecretStr API key, or None for
    #: a provider (Ollama) that authenticates no other way.
    secret_attr: str | None
    base_url_attr: str
    provider: Provider


#: Every provider an operator can configure from the UI. Mock is deliberately
#: absent -- it needs no credential and is always available regardless.
_PROVIDERS: tuple[_ProviderMeta, ...] = (
    _ProviderMeta("openai", "OpenAI (ChatGPT)", "openai_api_key", "openai_base_url", Provider.OPENAI),
    _ProviderMeta(
        "anthropic", "Anthropic (Claude)", "anthropic_api_key", "anthropic_base_url", Provider.ANTHROPIC
    ),
    _ProviderMeta("gemini", "Google (Gemini)", "gemini_api_key", "gemini_base_url", Provider.GEMINI),
    _ProviderMeta("groq", "Groq", "groq_api_key", "groq_base_url", Provider.GROQ),
    _ProviderMeta("ollama", "Ollama (local)", None, "ollama_base_url", Provider.OLLAMA),
)
_PROVIDERS_BY_NAME = {p.name: p for p in _PROVIDERS}

_CATALOG = build_catalog(mock_only=False)


def _mask(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


def _model_options(provider: Provider) -> tuple[ModelOption, ...]:
    return tuple(
        ModelOption(
            key=m.key,
            model=m.model,
            context_limit=m.context_limit,
            input_cost_per_mtok=m.input_cost_per_mtok,
            output_cost_per_mtok=m.output_cost_per_mtok,
            capabilities=tuple(sorted(c.value for c in m.capabilities)),
        )
        for m in _CATALOG.by_provider(provider)
    )


def _info(meta: _ProviderMeta, app_state: AppState, row: dict | None) -> ProviderInfo:
    env_secret = getattr(app_state.settings, meta.secret_attr) if meta.secret_attr else None
    env_configured = bool(env_secret) or (meta.secret_attr is None and app_state.settings.ollama_enabled)

    if row and (row.get("api_key") or meta.secret_attr is None):
        source = "database"
        configured = True
        masked = _mask(row["api_key"]) if row.get("api_key") else None
        base_url = row.get("base_url") or getattr(app_state.settings, meta.base_url_attr)
        selected_model_key = row.get("selected_model_key")
    elif env_configured:
        source = "environment"
        configured = True
        masked = _mask(env_secret.get_secret_value()) if env_secret else None
        base_url = getattr(app_state.settings, meta.base_url_attr)
        selected_model_key = None
    else:
        source = "none"
        configured = False
        masked = None
        base_url = getattr(app_state.settings, meta.base_url_attr)
        selected_model_key = None

    return ProviderInfo(
        provider=meta.name,
        label=meta.label,
        configured=configured,
        source=source,  # type: ignore[arg-type]
        masked_api_key=masked,
        base_url=base_url,
        selected_model_key=selected_model_key,
        models=_model_options(meta.provider),
    )


def _require_meta(provider: str) -> _ProviderMeta:
    meta = _PROVIDERS_BY_NAME.get(provider)
    if meta is None:
        raise NotFoundError(
            f"provider {provider!r} is not configurable here",
            provider=provider,
            available=sorted(_PROVIDERS_BY_NAME),
        )
    return meta


@router.get("", response_model=ProvidersPageResponse)
async def list_providers(app_state: AppState = Depends(get_app_state)) -> ProvidersPageResponse:
    async with app_state.database.session() as session:
        rows = await ProviderCredentialRepository(session).list_all()
        active_provider = await RoutingSettingsRepository(session).get_active_provider()
    by_provider = {r["provider"]: r for r in rows}
    return ProvidersPageResponse(
        active_provider=active_provider,
        providers=tuple(_info(meta, app_state, by_provider.get(meta.name)) for meta in _PROVIDERS),
    )


@router.put("/active", response_model=ProvidersPageResponse)
async def set_active_provider(
    request: SetActiveProviderRequest, app_state: AppState = Depends(get_app_state)
) -> ProvidersPageResponse:
    """Choose the single provider that drives every agent call, or clear it.

    Registered ahead of ``PUT /{provider}`` so the literal path ``active``
    cannot be swallowed by that route's ``{provider}`` path parameter.
    """
    if request.provider is not None:
        meta = _require_meta(request.provider)
        async with app_state.database.session() as session:
            rows = await ProviderCredentialRepository(session).list_all()
        overrides = {row["provider"]: row for row in rows}
        available = configured_providers(app_state.settings, overrides=overrides)
        if meta.name not in available:
            raise InputValidationError(
                f"provider {meta.name!r} is not connected yet -- add an API key first",
                provider=meta.name,
            )

    async with app_state.database.session() as session:
        await RoutingSettingsRepository(session).set_active_provider(request.provider)

    await app_state.reload_providers()
    return await list_providers(app_state)


@router.put("/{provider}", response_model=ProviderInfo)
async def update_provider(
    provider: str,
    request: UpdateProviderRequest,
    app_state: AppState = Depends(get_app_state),
) -> ProviderInfo:
    """Set (or update) one provider's credential, live.

    A partial update: omitted fields keep whatever is already stored, so
    saving just a model selection does not require retyping the key.
    """
    meta = _require_meta(provider)
    if request.selected_model_key is not None:
        model = _CATALOG.try_get(request.selected_model_key)
        if model is None or model.provider is not meta.provider:
            raise InputValidationError(
                f"{request.selected_model_key!r} is not a model of provider {provider!r}",
                provider=provider,
                selected_model_key=request.selected_model_key,
                available=[m.key for m in _CATALOG.by_provider(meta.provider)],
            )

    async with app_state.database.session() as session:
        repo = ProviderCredentialRepository(session)
        existing = await repo.get(provider)
        api_key = existing.get("api_key") if existing else None
        if request.clear_api_key:
            api_key = None
        elif request.api_key is not None:
            api_key = request.api_key
        base_url = request.base_url if request.base_url is not None else (
            existing.get("base_url") if existing else None
        )
        selected_model_key = (
            request.selected_model_key
            if request.selected_model_key is not None
            else (existing.get("selected_model_key") if existing else None)
        )
        row = await repo.upsert(
            provider,
            api_key=api_key,
            base_url=base_url,
            selected_model_key=selected_model_key,
        )

    await app_state.reload_providers()
    return _info(meta, app_state, row)


@router.delete("/{provider}", response_model=ProviderInfo, status_code=status.HTTP_200_OK)
async def delete_provider(
    provider: str, app_state: AppState = Depends(get_app_state)
) -> ProviderInfo:
    """Remove a UI-stored credential, reverting to whatever `.env` provides (if anything).

    Clears "active provider" too if this was it -- an active provider that
    is no longer connected would otherwise silently fall back to whatever
    `.env` provides for it (or nothing), which is not what "active" means.
    """
    meta = _require_meta(provider)
    async with app_state.database.session() as session:
        await ProviderCredentialRepository(session).delete(provider)
        routing = RoutingSettingsRepository(session)
        if await routing.get_active_provider() == provider:
            await routing.set_active_provider(None)

    await app_state.reload_providers()
    return _info(meta, app_state, None)
