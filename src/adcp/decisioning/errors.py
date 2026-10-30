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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.types.error_details import CreativeMergeRejectedDetails, Rejection

__all__ = [
    "AccountNotFoundError",
    "AuthRequiredError",
    "BillingNotPermittedForAgentError",
    "CreativeMergeRejectedError",
    "MediaBuyNotFoundError",
    "PermissionDeniedError",
    "RateLimitedError",
    "RightsChannelViolationDetails",
    "RightsChannelViolationError",
    "ServiceUnavailableError",
    "TermsRejectedError",
    "UnsupportedFeatureError",
    "ValidationError",
    "creative_merge_rejected_details",
    "rights_channel_violation_details",
]


class PermissionDeniedError(AdcpError):
    """Spec ``PERMISSION_DENIED`` (``recovery='correctable'``).

    Raised when the authenticated caller is not authorized for the
    requested action under the seller's policies, or a required signed
    credential is missing/invalid.

    For per-agent commercial-status rejections (suspended / blocked),
    raise :class:`AdcpError` with code ``"AGENT_SUSPENDED"`` /
    ``"AGENT_BLOCKED"`` directly — those are dedicated AdCP 3.1 codes
    with their own ``recovery="terminal"`` semantics, not flavors of
    ``PERMISSION_DENIED``.

    :param scope: When the gate is a per-agent non-status provisioning
        constraint, set to ``'agent'`` (with ``reason``); when
        billing-relationship, set to ``'billing'``. Sellers MUST emit
        ``scope='agent'`` only when buyer-agent identity has been
        established (signed-request derivation or credential-to-agent
        mapping); otherwise omit.
    :param reason: When ``scope='agent'``, the registered provisioning
        gate that fired (``'sandbox_only'``, etc.) — see
        ``error-details/agent-permission-denied.json``.
    :param message: Optional human-readable override of the default.
    :param details: Additional fields merged into ``error.details``.
    """

    def __init__(
        self,
        *,
        scope: Literal["agent", "billing"] | None = None,
        reason: str | None = None,
        message: str | None = None,
        field: str | None = None,
        suggestion: str | None = None,
        **details: Any,
    ) -> None:
        # Hard-reject the AdCP 3.0.5 placeholder ``status=`` kwarg.
        # Without this, the kwarg would land in ``**details`` and emit
        # ``error.details.status`` — a field 3.1 removed from
        # ``agent-permission-denied.json``, which seller-side
        # schema-validating receivers would reject. For per-agent
        # commercial status, raise ``AdcpError`` with code
        # ``"AGENT_SUSPENDED"`` / ``"AGENT_BLOCKED"`` directly.
        if "status" in details:
            raise TypeError(
                "PermissionDeniedError.status= was removed in AdCP 3.1 "
                "(spec PR adcontextprotocol/adcp#3906). The "
                "placeholder details.status field is gone from "
                "error-details/agent-permission-denied.json. Migrate to: "
                "raise AdcpError('AGENT_SUSPENDED', recovery='terminal') "
                "or AdcpError('AGENT_BLOCKED', recovery='terminal') for "
                "per-agent commercial-status rejections, or pass "
                "reason='sandbox_only' for the remaining provisioning "
                "gate."
            )

        merged_details: dict[str, Any] = dict(details)
        if scope is not None:
            merged_details["scope"] = scope
        if reason is not None:
            merged_details["reason"] = reason

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


class TermsRejectedError(AdcpError):
    """Spec ``TERMS_REJECTED`` (``recovery='correctable'``).

    Raised from ``create_media_buy`` when the seller rejects buyer-proposed
    ``measurement_terms``. The details SHOULD name the rejected term and the
    seller's acceptable range or supported vendors.

    AdCP 3.3 (``core/measurement-terms.json``): "A seller MUST NOT advertise a
    window (or other term value) in a product's measurement_terms that it would
    reject with TERMS_REJECTED when a buyer replays those terms unchanged on
    create_media_buy." Pass the package's ``proposed_terms`` and the product's
    ``advertised_terms`` and the constructor raises :class:`ValueError` instead
    of minting a rejection of a replay (see
    :func:`adcp.decisioning.measurement_terms.is_advertised_terms_replay`):
    accept the terms, or stop advertising them.

    :param term: The rejected term (``"billing_measurement"``,
        ``"makegood_policy"``, ...), echoed as ``error.details.term``.
    """

    def __init__(
        self,
        *,
        proposed_terms: Any = None,
        advertised_terms: Any = None,
        term: str | None = None,
        message: str | None = None,
        field: str | None = None,
        suggestion: str | None = None,
        **details: Any,
    ) -> None:
        if proposed_terms is not None and advertised_terms is not None:
            from adcp.decisioning.measurement_terms import is_advertised_terms_replay

            if is_advertised_terms_replay(proposed_terms, advertised_terms):
                raise ValueError(
                    "TERMS_REJECTED must not reject measurement_terms that replay the "
                    "product's advertised terms unchanged (AdCP 3.3). Accept them, or "
                    "remove them from the product's measurement_terms."
                )
        merged: dict[str, Any] = dict(details)
        if term is not None:
            merged["term"] = term
        super().__init__(
            "TERMS_REJECTED",
            message=message or "Proposed measurement terms were rejected.",
            recovery="correctable",
            field=field,
            suggestion=suggestion,
            details=merged or None,
        )


class CreativeMergeRejectedError(AdcpError):
    """Spec ``CREATIVE_MERGE_REJECTED`` (``recovery='correctable'``, AdCP 3.3).

    Raised from ``sync_creatives`` when merge assignment operations fail
    seller verification. Per ``enums/error-code.json`` the seller evaluates
    every merge in the request, leaves every assignment unchanged (assignment
    operations are atomic), SHOULD set ``error.field`` to the first rejected
    ``assignment_operations`` entry, and SHOULD return
    ``error-details/creative-merge-rejected.json`` listing each rejection.

    ``rejections`` are validated against the generated
    ``CreativeMergeRejectedDetails`` model; ``field`` defaults to
    ``assignment_operations[<lowest operation_index>]``. A seller that does not
    declare ``creative.supports_assignment_merge`` raises
    :class:`UnsupportedFeatureError` instead.

    :param rejections: One entry per rejected merge: ``operation_index``,
        ``creative_id``, ``package_id``, ``merged_creative_id`` and ``reason``
        (``content_mismatch``, ``format_mismatch``, ``not_on_package``,
        ``not_supported``), as mappings or ``Rejection`` models.
    """

    def __init__(
        self,
        *,
        rejections: Sequence[Rejection | Mapping[str, Any]],
        message: str | None = None,
        field: str | None = None,
        suggestion: str | None = None,
    ) -> None:
        from adcp.types.error_details import CreativeMergeRejectedDetails

        parsed = CreativeMergeRejectedDetails.model_validate(
            {
                "rejections": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                    for item in rejections
                ]
            }
        )
        first = min(int(item.operation_index) for item in parsed.rejections)
        super().__init__(
            "CREATIVE_MERGE_REJECTED",
            message=message or "One or more creative merge operations were rejected.",
            recovery="correctable",
            field=field if field is not None else f"assignment_operations[{first}]",
            suggestion=suggestion,
            details=parsed.model_dump(mode="json"),
        )


class RightsChannelViolationError(AdcpError):
    """Spec ``RIGHTS_CHANNEL_VIOLATION`` (``recovery='correctable'``, AdCP 3.3).

    A creative's ``rights[]`` ``channels`` / ``excluded_channels`` restriction
    does not permit the media channel of the requested output. The code is
    advisory in 3.3: a creative agent MAY raise it to decline the output, or
    attach :meth:`to_wire` as a non-terminal entry in ``errors[]`` on an
    otherwise successful build. ``error.field`` SHOULD point at the offending
    rights entry (``creative_manifest.rights[0].excluded_channels``) and the
    details SHOULD include ``rights_id``, the requested ``channel`` and the
    ``capability_id`` when the build named one. There is no error-details
    schema for this code; :func:`rights_channel_violation_details` reads these
    keys back.
    """

    def __init__(
        self,
        *,
        rights_id: str,
        channel: str,
        capability_id: str | None = None,
        message: str | None = None,
        field: str | None = None,
        suggestion: str | None = None,
        **details: Any,
    ) -> None:
        merged: dict[str, Any] = {**details, "rights_id": rights_id, "channel": channel}
        if capability_id is not None:
            merged["capability_id"] = capability_id
        super().__init__(
            "RIGHTS_CHANNEL_VIOLATION",
            message=message
            or f"Rights grant {rights_id!r} does not permit the {channel!r} channel.",
            recovery="correctable",
            field=field,
            suggestion=suggestion,
            details=merged,
        )


@dataclass(frozen=True)
class RightsChannelViolationDetails:
    """The documented ``RIGHTS_CHANNEL_VIOLATION`` detail keys, when present."""

    rights_id: str | None
    channel: str | None
    capability_id: str | None


def _code_and_details(error: Any) -> tuple[Any, Any]:
    if isinstance(error, Mapping):
        return error.get("code"), error.get("details")
    return getattr(error, "code", None), getattr(error, "details", None)


def creative_merge_rejected_details(error: Any) -> CreativeMergeRejectedDetails | None:
    """Narrow a ``CREATIVE_MERGE_REJECTED`` error's details to the typed model.

    Accepts an :class:`AdcpError`, a generated ``Error`` or a wire mapping.
    Returns ``None`` for any other code, or when the details are absent or do
    not conform to ``error-details/creative-merge-rejected.json`` (the schema
    makes them a SHOULD, so a buyer falls back to ``error.field``).
    """
    from pydantic import ValidationError as PydanticValidationError

    from adcp.types.error_details import CreativeMergeRejectedDetails

    code, details = _code_and_details(error)
    if code != "CREATIVE_MERGE_REJECTED" or details is None:
        return None
    try:
        return CreativeMergeRejectedDetails.model_validate(details)
    except PydanticValidationError:
        return None


def rights_channel_violation_details(error: Any) -> RightsChannelViolationDetails | None:
    """Read ``rights_id``, ``channel`` and ``capability_id`` off the error.

    Returns ``None`` unless the code is ``RIGHTS_CHANNEL_VIOLATION``. Each key
    is a SHOULD, so any of them may be ``None``; non-string values read as
    ``None``.
    """
    code, details = _code_and_details(error)
    if code != "RIGHTS_CHANNEL_VIOLATION":
        return None
    if hasattr(details, "model_dump"):
        details = details.model_dump(mode="json")
    values = details if isinstance(details, Mapping) else {}

    def _text(key: str) -> str | None:
        value = values.get(key)
        return value if isinstance(value, str) else None

    return RightsChannelViolationDetails(
        rights_id=_text("rights_id"),
        channel=_text("channel"),
        capability_id=_text("capability_id"),
    )
