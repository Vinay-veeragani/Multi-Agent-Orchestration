"""Translates the engine's error taxonomy into HTTP responses.

One handler, one mapping table -- routes never construct an ``HTTPException``
for an engine failure themselves. That keeps the status code a function of the
error's *class*, decided once here, rather than a judgment call repeated (and
liable to drift) at every call site.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from orchestration.errors import (
    ApprovalRejectedError,
    BudgetExceededError,
    ConcurrencyConflictError,
    ConfigurationError,
    DuplicateError,
    GraphValidationError,
    InputValidationError,
    InvalidStateTransitionError,
    NotFoundError,
    OrchestrationError,
    PermissionDeniedError,
    PolicyViolationError,
    SchemaViolationError,
    to_error_dict,
)

#: Status code per error type, most specific first -- ``isinstance`` against
#: this in order, so a subclass is never shadowed by a less specific ancestor
#: listed after it (there are none today, but the ordering is the contract).
_STATUS_BY_TYPE: tuple[tuple[type[OrchestrationError], int], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (DuplicateError, status.HTTP_409_CONFLICT),
    (ConcurrencyConflictError, status.HTTP_409_CONFLICT),
    (InvalidStateTransitionError, status.HTTP_409_CONFLICT),
    (ApprovalRejectedError, status.HTTP_409_CONFLICT),
    (PermissionDeniedError, status.HTTP_403_FORBIDDEN),
    (PolicyViolationError, status.HTTP_403_FORBIDDEN),
    (BudgetExceededError, status.HTTP_402_PAYMENT_REQUIRED),
    (InputValidationError, status.HTTP_400_BAD_REQUEST),
    (SchemaViolationError, status.HTTP_400_BAD_REQUEST),
    (GraphValidationError, status.HTTP_400_BAD_REQUEST),
    (ConfigurationError, status.HTTP_500_INTERNAL_SERVER_ERROR),
)


def _status_for(exc: OrchestrationError) -> int:
    for error_type, code in _STATUS_BY_TYPE:
        if isinstance(exc, error_type):
            return code
    # Retryable-by-default engine errors (timeouts, provider outages) surfaced
    # all the way to the API mean the engine itself could not recover.
    return status.HTTP_502_BAD_GATEWAY if exc.retryable else status.HTTP_500_INTERNAL_SERVER_ERROR


async def _handle_orchestration_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, OrchestrationError)
    return JSONResponse(status_code=_status_for(exc), content={"error": to_error_dict(exc)})


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(OrchestrationError, _handle_orchestration_error)
