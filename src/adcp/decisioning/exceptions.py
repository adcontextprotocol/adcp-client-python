"""Typed AdcpError subclasses for common rejection patterns.

Adopters raise these in Platform method bodies instead of constructing
``AdcpError(code=..., recovery=...)`` inline.  The framework's dispatcher
catches :class:`~adcp.decisioning.types.AdcpError` at the dispatch seam and
serialises to the wire ``adcp_error`` envelope — these subclasses are caught
by the same handler.

All subclasses hard-code the correct ``code`` and ``recovery`` values from
the AdCP spec (``schemas/cache/3.0.0/enums/error-code.json``).  Keyword
``details`` captures free-form extras; any unknown kwargs are forwarded as the
``details`` dict to :class:`~adcp.decisioning.types.AdcpError`.
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning.types import AdcpError


class PermissionDeniedError(AdcpError):
    """Raised when the authenticated principal lacks permission for ``action``.

    Maps to wire code ``PERMISSION_DENIED`` with ``recovery='correctable'``
    (spec ``enumMetadata`` classification — the request can be retried after
    the underlying permission is resolved, e.g. minting a valid governance
    token or contacting the seller).
    """

    def __init__(self, action: str = "", **details: Any) -> None:
        msg = f"Permission denied: {action}" if action else "Permission denied"
        super().__init__(
            "PERMISSION_DENIED",
            message=msg,
            recovery="correctable",
            details=details if details else None,
        )


class AuthRequiredError(AdcpError):
    """Raised when a request arrives without valid authentication.

    Maps to ``AUTH_REQUIRED`` with ``recovery='correctable'`` (per AdCP 3.0.4
    prose — missing-credentials case; the buyer should re-present credentials
    rather than abandon).  See the note in ``adcp.server.helpers`` about the
    planned 3.1 split into ``AUTH_MISSING`` / ``AUTH_INVALID``.
    """

    def __init__(self, **details: Any) -> None:
        super().__init__(
            "AUTH_REQUIRED",
            message="Authentication required",
            recovery="correctable",
            details=details if details else None,
        )


class ServiceUnavailableError(AdcpError):
    """Raised when the seller service is temporarily unavailable.

    Maps to ``SERVICE_UNAVAILABLE`` with ``recovery='transient'``.
    """

    def __init__(self, message: str = "Service temporarily unavailable", **details: Any) -> None:
        super().__init__(
            "SERVICE_UNAVAILABLE",
            message=message,
            recovery="transient",
            details=details if details else None,
        )


class RateLimitedError(AdcpError):
    """Raised when the buyer has exceeded the request rate limit.

    Maps to ``RATE_LIMITED`` with ``recovery='transient'``.  Pass
    ``retry_after`` to tell the buyer how long to wait.
    """

    def __init__(self, *, retry_after: int | None = None, **details: Any) -> None:
        super().__init__(
            "RATE_LIMITED",
            message="Too many requests",
            recovery="transient",
            retry_after=retry_after,
            details=details if details else None,
        )


class MediaBuyNotFoundError(AdcpError):
    """Raised when the referenced media buy does not exist.

    Maps to ``MEDIA_BUY_NOT_FOUND`` with ``recovery='correctable'``.
    """

    def __init__(self, **details: Any) -> None:
        super().__init__(
            "MEDIA_BUY_NOT_FOUND",
            message="Media buy not found",
            recovery="correctable",
            details=details if details else None,
        )


class AccountNotFoundError(AdcpError):
    """Raised when the referenced account does not exist.

    Maps to ``ACCOUNT_NOT_FOUND`` with ``recovery='terminal'``.
    """

    def __init__(self, **details: Any) -> None:
        super().__init__(
            "ACCOUNT_NOT_FOUND",
            message="Account not found",
            recovery="terminal",
            details=details if details else None,
        )


class BillingNotPermittedForAgentError(AdcpError):
    """Raised when a buyer agent attempts a billing operation it is not
    authorised to perform.

    Maps to ``BILLING_NOT_PERMITTED_FOR_AGENT`` with ``recovery='correctable'``
    (spec ``enumMetadata`` classification — retry with a permitted billing value
    from ``error.details.suggested_billing``, or surface to a human when absent).
    """

    def __init__(self, **details: Any) -> None:
        super().__init__(
            "BILLING_NOT_PERMITTED_FOR_AGENT",
            message="Billing operations are not permitted for this agent",
            recovery="correctable",
            details=details if details else None,
        )


class RequestValidationError(AdcpError):
    """Raised when the request fails validation (malformed fields, constraint
    violations, etc.).

    Maps to ``VALIDATION_ERROR`` with ``recovery='correctable'``.

    Named ``RequestValidationError`` (not ``ValidationError``) to avoid
    shadowing ``pydantic.ValidationError`` and ``adcp.validation.legacy.ValidationError``
    in adopter import namespaces.
    """

    def __init__(
        self, message: str = "Request validation failed", **details: Any
    ) -> None:
        super().__init__(
            "VALIDATION_ERROR",
            message=message,
            recovery="correctable",
            details=details if details else None,
        )


__all__ = [
    "AccountNotFoundError",
    "AuthRequiredError",
    "BillingNotPermittedForAgentError",
    "MediaBuyNotFoundError",
    "PermissionDeniedError",
    "RateLimitedError",
    "RequestValidationError",
    "ServiceUnavailableError",
]
