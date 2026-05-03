"""Typed exception subclasses for the AdCP error code vocabulary.

:class:`adcp.decisioning.AdcpError` is the wire-shaped structured error
adopters raise from inside Protocol method bodies — the framework
catches at the dispatch seam and projects to the ``adcp_error``
envelope. The base class accepts any spec code as a string, but
adopters who want catch-by-type ergonomics (``except PermissionDeniedError:``)
or who want the correct ``recovery`` semantic auto-applied repeatedly
re-derive the boilerplate. These subclasses bind each spec code to its
canonical recovery classification (per
``schemas/cache/enums/error-code.json#enumMetadata``) and offer a
sensible default message adopters can override.

Recovery values are normative — they MUST match the ``enumMetadata``
block in the error-code schema. Adopters MUST NOT override
``recovery`` on these subclasses; if a different recovery is needed
for a vendor variant, raise the base :class:`AdcpError` directly.
"""

from __future__ import annotations

from typing import Any, Literal

from adcp.decisioning.types import AdcpError

__all__ = [
    "AccountNotFoundError",
    "AuthRequiredError",
    "BillingNotPermittedForAgentError",
    "MediaBuyNotFoundError",
    "PermissionDeniedError",
    "RateLimitedError",
    "ServiceUnavailableError",
    "UnsupportedFeatureError",
    "ValidationError",
]


class PermissionDeniedError(AdcpError):
    """Spec ``PERMISSION_DENIED`` (``recovery='correctable'``).

    Raised when the authenticated caller is not authorized for the
    requested action under the seller's policies, or a required signed
    credential is missing/invalid.

    :param scope: When the gate is a per-agent provisioning constraint,
        set to ``'agent'`` (with ``status``); when billing-relationship,
        set to ``'billing'``. Sellers MUST emit ``scope='agent'`` only
        when buyer-agent identity has been established (signed-request
        derivation or credential-to-agent mapping); otherwise omit.
    :param status: When ``scope='agent'``, the per-agent state
        (``'sandbox_only'``, etc.) — see
        ``error-details/agent-permission-denied.json``.
    :param message: Optional human-readable override of the default.
    :param details: Additional fields merged into ``error.details``.
    """

    def __init__(
        self,
        *,
        scope: Literal["agent", "billing"] | None = None,
        status: str | None = None,
        message: str | None = None,
        field: str | None = None,
        suggestion: str | None = None,
        **details: Any,
    ) -> None:
        merged_details: dict[str, Any] = dict(details)
        if scope is not None:
            merged_details["scope"] = scope
        if status is not None:
            merged_details["status"] = status

        super().__init__(
            "PERMISSION_DENIED",
            message=message or "Caller is not authorized for the requested action.",
            recovery="correctable",
            field=field,
            suggestion=suggestion,
            details=merged_details or None,
        )


class AuthRequiredError(AdcpError):
    """Spec ``AUTH_REQUIRED`` (``recovery='correctable'``).

    Raised when no credentials were presented. (The schema classifies
    this as ``correctable`` — the buyer fixes by attaching credentials
    and retrying.)
    """

    def __init__(
        self,
        *,
        message: str | None = None,
        field: str | None = None,
        suggestion: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            "AUTH_REQUIRED",
            message=message or "Authentication is required to access this resource.",
            recovery="correctable",
            field=field,
            suggestion=suggestion,
            details=dict(details) or None,
        )


class ServiceUnavailableError(AdcpError):
    """Spec ``SERVICE_UNAVAILABLE`` (``recovery='transient'``).

    Raised when the seller service is temporarily unavailable. The
    buyer retries with exponential backoff; ``retry_after`` MAY be set
    to hint a minimum delay.
    """

    def __init__(
        self,
        *,
        message: str | None = None,
        retry_after: int | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            "SERVICE_UNAVAILABLE",
            message=message or "Service is temporarily unavailable.",
            recovery="transient",
            retry_after=retry_after,
            details=dict(details) or None,
        )


class RateLimitedError(AdcpError):
    """Spec ``RATE_LIMITED`` (``recovery='transient'``).

    Raised when the request rate exceeds the seller's threshold. The
    buyer retries after the ``retry_after`` interval.
    """

    def __init__(
        self,
        *,
        message: str | None = None,
        retry_after: int | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            "RATE_LIMITED",
            message=message or "Request rate exceeded.",
            recovery="transient",
            retry_after=retry_after,
            details=dict(details) or None,
        )


class MediaBuyNotFoundError(AdcpError):
    """Spec ``MEDIA_BUY_NOT_FOUND`` (``recovery='correctable'``).

    Raised when the referenced media buy does not exist or is not
    accessible to the requesting agent. Sellers MUST return this code
    uniformly for any ``media_buy_id`` not owned by the calling
    account — never distinguish "exists in another tenant" from
    "does not exist".
    """

    def __init__(
        self,
        *,
        media_buy_id: str | None = None,
        message: str | None = None,
        field: str | None = None,
        **details: Any,
    ) -> None:
        merged: dict[str, Any] = dict(details)
        if media_buy_id is not None:
            merged["media_buy_id"] = media_buy_id
        super().__init__(
            "MEDIA_BUY_NOT_FOUND",
            message=message or "Media buy not found.",
            recovery="correctable",
            field=field,
            details=merged or None,
        )


class AccountNotFoundError(AdcpError):
    """Spec ``ACCOUNT_NOT_FOUND`` (``recovery='terminal'``).

    Raised when the account reference cannot be resolved. The buyer
    verifies the account via ``list_accounts`` or contacts the seller.
    """

    def __init__(
        self,
        *,
        message: str | None = None,
        field: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            "ACCOUNT_NOT_FOUND",
            message=message or "Account not found.",
            recovery="terminal",
            field=field,
            details=dict(details) or None,
        )


class BillingNotPermittedForAgentError(AdcpError):
    """Spec ``BILLING_NOT_PERMITTED_FOR_AGENT`` (``recovery='correctable'``).

    Raised when the seller's ``supported_billing`` capability accepts
    the requested billing model, but the calling buyer agent's
    commercial relationship with the seller does not. The recovery
    shape is deliberately minimal — ``error.details`` MUST conform to
    ``error-details/billing-not-permitted-for-agent.json``
    (``rejected_billing`` plus an optional single ``suggested_billing``
    retry value, typically ``'operator'``).

    :param rejected_billing: The billing values the agent attempted that
        are not permitted (echoed in ``error.details.rejected_billing``).
    :param suggested_billing: Optional single retry value the seller
        recommends (echoed in ``error.details.suggested_billing``).
    """

    def __init__(
        self,
        *,
        rejected_billing: list[str],
        suggested_billing: list[str] | None = None,
        message: str | None = None,
    ) -> None:
        merged: dict[str, Any] = {"rejected_billing": list(rejected_billing)}
        if suggested_billing is not None:
            merged["suggested_billing"] = list(suggested_billing)
        super().__init__(
            "BILLING_NOT_PERMITTED_FOR_AGENT",
            message=(
                message or "Calling agent is not permitted to use the requested billing value."
            ),
            recovery="correctable",
            details=merged,
        )


class ValidationError(AdcpError):
    """Spec ``VALIDATION_ERROR`` (``recovery='correctable'``).

    Raised when a request contains invalid field values or violates
    business rules beyond schema validation. ``field`` SHOULD identify
    the offending path so buyers can highlight the input.
    """

    def __init__(
        self,
        *,
        message: str | None = None,
        field: str | None = None,
        suggestion: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            "VALIDATION_ERROR",
            message=message or "Request failed validation.",
            recovery="correctable",
            field=field,
            suggestion=suggestion,
            details=dict(details) or None,
        )


class UnsupportedFeatureError(AdcpError):
    """Spec ``UNSUPPORTED_FEATURE`` (``recovery='correctable'``).

    Raised when a requested feature or field is not supported by this
    seller. The buyer checks ``get_adcp_capabilities`` and removes the
    unsupported field.
    """

    def __init__(
        self,
        *,
        message: str | None = None,
        field: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            "UNSUPPORTED_FEATURE",
            message=message or "Requested feature is not supported by this seller.",
            recovery="correctable",
            field=field,
            details=dict(details) or None,
        )
