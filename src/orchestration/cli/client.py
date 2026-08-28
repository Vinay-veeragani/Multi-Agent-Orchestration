"""A thin async HTTP client over the orchestration API.

One method per endpoint the CLI actually calls, all returning parsed JSON.
Error handling lives in one place (:meth:`ApiClient._request`): a non-2xx
response is turned into :class:`ApiError`, carrying the same structured error
body :mod:`orchestration.api.errors` produces, so a CLI command can print the
engine's own message instead of an HTTP status code.
"""

from __future__ import annotations

from types import TracebackType

import httpx

from orchestration.domain.base import JsonDict


class ApiError(Exception):
    """A non-2xx response from the API, carrying its structured error body."""

    def __init__(self, status_code: int, body: JsonDict) -> None:
        error = body.get("error", {})
        message = str(error.get("message", body)) if isinstance(error, dict) else str(body)
        code = error.get("code", "error") if isinstance(error, dict) else "error"
        super().__init__(f"[{code}] {message}")
        self.status_code = status_code
        self.code = code
        self.body = body


class ApiClient:
    """Async context manager wrapping an :class:`httpx.AsyncClient`.

    ``transport`` is a test seam: passing an ``httpx.ASGITransport`` lets a
    test drive the real FastAPI app in-process, the same way
    ``tests/integration/test_api.py`` does, without a real server listening on
    a port. Production use never sets it -- ``base_url`` alone determines
    where requests go.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, transport=transport
        )

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: object) -> JsonDict:
        response = await self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.json())
        body: JsonDict = response.json() if response.content else {}
        return body

    async def _request_list(self, method: str, path: str, **kwargs: object) -> list[JsonDict]:
        response = await self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.json())
        result: list[JsonDict] = response.json()
        return result

    async def health(self) -> JsonDict:
        return await self._request("GET", "/health")

    async def list_agents(self) -> list[JsonDict]:
        return await self._request_list("GET", "/agents")

    async def create_execution(
        self,
        task: str,
        *,
        workflow_id: str | None = None,
        max_turns: int | None = None,
        success_criteria: tuple[str, ...] = (),
        idempotency_key: str | None = None,
    ) -> JsonDict:
        payload: JsonDict = {"task": task, "success_criteria": list(success_criteria)}
        if workflow_id is not None:
            payload["workflow_id"] = workflow_id
        if max_turns is not None:
            payload["max_turns"] = max_turns
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/executions", json=payload)

    async def get_execution(self, execution_id: str) -> JsonDict:
        return await self._request("GET", f"/executions/{execution_id}")

    async def cancel_execution(self, execution_id: str, *, reason: str | None = None) -> JsonDict:
        payload = {"reason": reason} if reason else {}
        return await self._request("POST", f"/executions/{execution_id}/cancel", json=payload)

    async def resume_execution(self, execution_id: str) -> JsonDict:
        return await self._request("POST", f"/executions/{execution_id}/resume")

    async def decide_approval(
        self,
        execution_id: str,
        *,
        approve: bool,
        by: str,
        note: str | None = None,
        approval_id: str | None = None,
    ) -> JsonDict:
        payload: JsonDict = {"by": by}
        if note is not None:
            payload["note"] = note
        if approval_id is not None:
            payload["approval_id"] = approval_id
        verb = "approve" if approve else "reject"
        return await self._request("POST", f"/executions/{execution_id}/{verb}", json=payload)

    async def get_events(self, execution_id: str) -> list[JsonDict]:
        return await self._request_list("GET", f"/executions/{execution_id}/events")
