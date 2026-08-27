"""``X-API-Key`` authentication.

Deliberately simple: a shared-secret header checked against
``settings.api_key_set``, which is exactly what a reference deployment behind
a private network or a gateway needs. Nothing here should be mistaken for a
substitute for real authn/authz in front of a public deployment.
"""

from __future__ import annotations

from fastapi import Depends, Header, Request

from orchestration.api.state import AppState
from orchestration.errors import PermissionDeniedError


def get_app_state(request: Request) -> AppState:
    return request.app.state.app_state  # type: ignore[no-any-return]


async def require_api_key(
    app_state: AppState = Depends(get_app_state),
    x_api_key: str | None = Header(default=None),
) -> None:
    if not app_state.settings.api_require_auth:
        return
    if x_api_key is None or x_api_key not in app_state.settings.api_key_set:
        raise PermissionDeniedError("missing or invalid API key", header="X-API-Key")
