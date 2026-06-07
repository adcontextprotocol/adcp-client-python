"""DX helpers for ADCP server builders.

Automate error responses, state transitions, account resolution,
and context passthrough so developers focus on business logic.

    from adcp.server.helpers import adcp_error, valid_actions_for_status

    return adcp_error("BUDGET_TOO_LOW", "Budget $50 is below minimum $500",
                      field="budget", suggestion="Increase to at least $500")

    actions = valid_actions_for_status("active")
"""

from __future__ import annotations

import inspect
import logging
import warnings
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, cast

from adcp.server.base import AccountAwareToolContext, ToolContext

logger = logging.getLogger("adcp.server")

# All 32 codes from the ADCP spec (enums/error-code.json) plus SDK extensions.
# Recovery classification: transient (retry), correctable (fix request), terminal.
STANDARD_ERROR_CODES: dict[str, dict[str, str]] = {
    # --- Spec codes: Transient ---
    "RATE_LIMITED": {"recovery": "transient", "message": "Too many requests"},
    "SERVICE_UNAVAILABLE": {"recovery": "transient", "message": "Service temporarily unavailable"},
    # --- Spec codes: Correctable ---
    "INVALID_REQUEST": {"recovery": "correctable", "message": "Invalid request parameters"},
    "VALIDATION_ERROR": {"recovery": "correctable", "message": "Request validation failed"},
    "POLICY_VIOLATION": {"recovery": "correctable", "message": "Policy violation"},
    "PRODUCT_NOT_FOUND": {"recovery": "correctable", "message": "Product not found"},
    "PROPOSAL_NOT_FOUND": {"recovery": "correctable", "message": "Proposal not found"},
    "PRODUCT_UNAVAILABLE": {"recovery": "correctable", "message": "Product unavailable"},
    "PRODUCT_EXPIRED": {"recovery": "correctable", "message": "Product expired"},
    "PROPOSAL_EXPIRED": {"recovery": "correctable", "message": "Proposal expired"},
    "PROPOSAL_NOT_COMMITTED": {"recovery": "correctable", "message": "Proposal not committed"},
    "BUDGET_TOO_LOW": {"recovery": "correctable", "message": "Budget below minimum"},
    "BUDGET_EXHAUSTED": {"recovery": "correctable", "message": "Budget fully spent"},
    "BUDGET_EXCEEDED": {"recovery": "correctable", "message": "Would exceed budget allocation"},
    "CREATIVE_REJECTED": {"recovery": "correctable", "message": "Creative rejected"},
    "CREATIVE_DEADLINE_EXCEEDED": {
        "recovery": "correctable",
        "message": "Creative deadline passed",
    },
    "AUDIENCE_TOO_SMALL": {"recovery": "correctable", "message": "Audience too small"},
    "MEDIA_BUY_NOT_FOUND": {"recovery": "correctable", "message": "Media buy not found"},
    "PACKAGE_NOT_FOUND": {"recovery": "correctable", "message": "Package not found"},
    "SIGNAL_NOT_FOUND": {"recovery": "correctable", "message": "Signal not found"},
    "CONFLICT": {"recovery": "correctable", "message": "Revision conflict - refetch and retry"},
    "INVALID_STATE": {"recovery": "correctable", "message": "Invalid state for this operation"},
    "NOT_CANCELLABLE": {"recovery": "correctable", "message": "Cannot cancel this media buy"},
    "COMPLIANCE_UNSATISFIED": {"recovery": "correctable", "message": "Compliance not met"},
    "ACCOUNT_AMBIGUOUS": {"recovery": "correctable", "message": "Account reference is ambiguous"},
    "ACCOUNT_SETUP_REQUIRED": {"recovery": "correctable", "message": "Account setup required"},
    "ACCOUNT_PAYMENT_REQUIRED": {"recovery": "correctable", "message": "Payment required"},
    "IO_REQUIRED": {"recovery": "correctable", "message": "Insertion order required"},
    "SESSION_NOT_FOUND": {"recovery": "correctable", "message": "Session not found"},
    "SESSION_TERMINATED": {"recovery": "correctable", "message": "Session already terminated"},
    # AUTH_REQUIRED is `correctable` per the 3.0.4 prose tightening, but only
    # the missing-credentials sub-case is actually retry-safe. When the seller
    # rejected presented credentials (expired / revoked / malformed signature),
    # the buyer agent SHOULD NOT auto-retry — re-presenting a rejected
    # credential creates SSO retry-storm patterns. The 3.1 line splits this
    # into AUTH_MISSING (correctable) and AUTH_INVALID (terminal); on 3.0.x
    # the operational distinction lives in `suggestion` text.
    "AUTH_REQUIRED": {"recovery": "correctable", "message": "Authentication required"},
    # --- Spec codes: Terminal ---
    "ACCOUNT_NOT_FOUND": {"recovery": "terminal", "message": "Account not found"},
    "ACCOUNT_SUSPENDED": {"recovery": "terminal", "message": "Account suspended"},
    "AUTHORIZATION_REQUIRED": {
        "recovery": "terminal",
        "message": "Downstream authorization required",
    },
    "UNSUPPORTED_FEATURE": {"recovery": "terminal", "message": "Feature not supported"},
    # Idempotency (AdCP #2315). Both are "terminal" from a retry-behavior
    # standpoint — the caller MUST take a specific action (mint a fresh key or
    # reconcile state) rather than blindly retry.
    "IDEMPOTENCY_CONFLICT": {
        "recovery": "terminal",
        "message": "idempotency_key reused with a different payload",
    },
    "IDEMPOTENCY_EXPIRED": {
        "recovery": "terminal",
        "message": "Idempotency replay window has expired",
    },
    # --- SDK extensions (not in spec enum) ---
    "NOT_SUPPORTED": {"recovery": "terminal", "message": "Operation not supported"},
}

# Typed recovery classification sets for servers building their own error hierarchies.
TRANSIENT_CODES: frozenset[str] = frozenset(
    code for code, info in STANDARD_ERROR_CODES.items() if info["recovery"] == "transient"
)
CORRECTABLE_CODES: frozenset[str] = frozenset(
    code for code, info in STANDARD_ERROR_CODES.items() if info["recovery"] == "correctable"
)
TERMINAL_CODES: frozenset[str] = frozenset(
    code for code, info in STANDARD_ERROR_CODES.items() if info["recovery"] == "terminal"
)


def adcp_error(
    code: str,
    message: str | None = None,
    *,
    field: str | None = None,
    suggestion: str | None = None,
    recovery: str | None = None,
    retry_after: int | None = None,
    details: dict[str, str | int | float | bool | None] | None = None,
) -> dict[str, Any]:
    """Build a structured ADCP error response with auto-recovery.

    Standard codes get recovery auto-populated from the code table.
    Custom codes default to "terminal".

    Args:
        code: Error code (e.g., "BUDGET_TOO_LOW").
        message: Human-readable message. Defaults to standard message.
        field: Which request field caused the error.
        suggestion: Actionable fix suggestion.
        recovery: Override ("transient", "correctable", "terminal").
        retry_after: Seconds to wait (for RATE_LIMITED).
        details: Server-generated debugging data (constraint names, limits,
            thresholds). Use only server-generated values here. NEVER pass
            request params or user-supplied strings -- they flow to the
            caller's LLM context and could enable prompt injection.
    """
    std = STANDARD_ERROR_CODES.get(code, {})
    err: dict[str, Any] = {
        "code": code,
        "message": message or std.get("message", code),
        "recovery": recovery or std.get("recovery", "terminal"),
    }
    if field is not None:
        err["field"] = field
    if suggestion is not None:
        err["suggestion"] = suggestion
    if retry_after is not None:
        err["retry_after"] = retry_after
    if details is not None:
        err["details"] = details
    return {"errors": [err]}


# ============================================================================
# Media Buy State Machine
# ============================================================================

# Status values from the ADCP spec (enums/media-buy-status.json).
# Actions are operations available via update_media_buy for each status.
# Public constant — servers can inspect, test against, or extend this.
MEDIA_BUY_STATE_MACHINE: dict[str, list[str]] = {
    "pending_creatives": [
        "cancel",
        "update_budget",
        "update_dates",
        "update_packages",
        "add_packages",
        "sync_creatives",
    ],
    "pending_start": [
        "cancel",
        "update_budget",
        "update_dates",
        "update_packages",
        "add_packages",
    ],
    "active": [
        "pause",
        "cancel",
        "update_budget",
        "update_dates",
        "update_packages",
        "add_packages",
    ],
    "paused": ["resume", "cancel", "update_budget", "update_dates"],
    "completed": [],
    "rejected": [],
    "canceled": [],
}


def valid_actions_for_status(status: str) -> list[str]:
    """Get valid buyer actions for a media buy status.

    Returns the list of ``update_media_buy`` actions available to a buyer for
    the given status string. Returns ``[]`` for terminal statuses and for any
    unrecognized status string.

    Valid statuses per ``enums/media-buy-status.json``:
    ``pending_creatives``, ``pending_start``, ``active``, ``paused``,
    ``completed``, ``rejected``, ``canceled``.

    Inspect or extend :data:`MEDIA_BUY_STATE_MACHINE` to add custom actions.
    """
    return list(MEDIA_BUY_STATE_MACHINE.get(status, []))


def is_terminal_status(status: str) -> bool:
    """Check if a media buy status is terminal (no further actions)."""
    return status in ("completed", "rejected", "canceled")


# ============================================================================
# Account Resolution
# ============================================================================

AccountResolver = Callable[[dict[str, Any]], Awaitable[Any | None]]


class AccountError(Exception):
    """Raised by account resolvers to indicate a specific account error.

    Use this in your resolver to return structured errors for cases
    beyond simple "not found"::

        async def my_resolver(ref):
            account = db.find(ref)
            if not account:
                return None  # auto-returns ACCOUNT_NOT_FOUND
            if account.status == "suspended":
                raise AccountError("ACCOUNT_SUSPENDED", "Account is suspended")
            if account.status == "payment_required":
                raise AccountError("ACCOUNT_PAYMENT_REQUIRED",
                    suggestion="Update payment method at https://...")
            return account
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        suggestion: str | None = None,
    ):
        self.code = code
        self.error_message = message
        self.suggestion = suggestion
        super().__init__(message or code)


async def resolve_account(
    params: dict[str, Any],
    resolver: AccountResolver | None,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Resolve an account reference from request params.

    Returns (account, None) on success, (None, error_dict) on failure,
    or (None, None) if no account field or no resolver configured.

    The resolver can return None (auto-ACCOUNT_NOT_FOUND) or raise
    ``AccountError`` for specific error codes (ACCOUNT_SUSPENDED,
    ACCOUNT_PAYMENT_REQUIRED, ACCOUNT_AMBIGUOUS, etc.).
    """
    if resolver is None or "account" not in params:
        return None, None
    try:
        account = await resolver(params["account"])
    except AccountError as e:
        return None, adcp_error(
            e.code,
            e.error_message,
            field="account",
            suggestion=e.suggestion,
        )
    if account is None:
        return None, adcp_error(
            "ACCOUNT_NOT_FOUND",
            "The specified account does not exist",
            field="account",
            suggestion="Use list_accounts to discover available accounts, "
            "or sync_accounts to create one",
        )
    return account, None


async def resolve_account_into_context(
    params: dict[str, Any],
    context: AccountAwareToolContext | None,
    resolver: AccountResolver | None,
    *,
    account_id_attr: str = "account_id",
) -> dict[str, Any] | None:
    """Resolve an account reference and populate an
    :class:`~adcp.server.AccountAwareToolContext`.

    Collapses the standard three-line boilerplate (resolve → check error
    → extract id) into one call. Returns ``None`` on success (or when
    there's nothing to resolve); returns an error dict to be returned
    directly from the handler otherwise::

        async def get_products(self, params, context=None):
            err = await resolve_account_into_context(
                params, context, my_resolver,
            )
            if err:
                return err
            return products_response(catalog.for_account(context.account_id))

    :param params: The request params dict, expected to carry an
        ``account`` key with an ``AccountReference``.
    :param context: The handler's context. Must be
        :class:`~adcp.server.AccountAwareToolContext` (or a subclass of
        it) to receive the resolved fields. Passing a plain
        ``ToolContext`` runs resolution for the error path but logs a
        ``UserWarning`` — the silent-skip would otherwise break the
        multi-tenant scope contract.
    :param resolver: An :data:`AccountResolver` — same shape as
        :func:`resolve_account` accepts.
    :param account_id_attr: Attribute name on the resolver's account
        object that holds the stable id. Defaults to ``"account_id"``
        — matches the SDK's spec-generated :class:`~adcp.types.Account`
        type. Override when your resolver returns a domain object
        using a different attr name.
    """
    account, err = await resolve_account(params, resolver)
    if err is not None:
        return err
    if account is None:
        return None

    if not isinstance(context, AccountAwareToolContext):
        warnings.warn(
            "resolve_account_into_context received a context that isn't an "
            "AccountAwareToolContext — account was resolved but context not "
            "mutated. Populate your handler's context_factory to return "
            "AccountAwareToolContext (or a subclass), or parameterise your "
            "handler with ADCPHandler[AccountAwareToolContext]. Silent skip "
            "means downstream cache/audit keys will scope to None.",
            UserWarning,
            stacklevel=2,
        )
        return None

    if not hasattr(account, account_id_attr):
        raise ValueError(
            f"Resolved account of type {type(account).__name__!r} has no "
            f"{account_id_attr!r} attribute. Pass account_id_attr= to "
            f"resolve_account_into_context() if your resolver returns a "
            f"domain object using a different field name."
        )

    context.account = account
    context.account_id = getattr(account, account_id_attr)
    return None


# ============================================================================
# Context Passthrough
# ============================================================================


_MAX_CONTEXT_SIZE = 64 * 1024  # 64KB limit on context passthrough


def inject_context(
    params: dict[str, Any],
    response: dict[str, Any],
    *,
    max_size: int = _MAX_CONTEXT_SIZE,
) -> dict[str, Any]:
    """Auto-inject context passthrough from request into response.

    ADCP requires that if a request contains a ``context`` field,
    the response must echo it back unchanged. A size limit prevents
    resource amplification from oversized context payloads.

    The context field is opaque and may contain attacker-controlled
    data -- do not interpret or display its contents.
    """
    if "context" in params and "context" not in response:
        import json

        ctx = params["context"]
        if len(json.dumps(ctx, default=str)) <= max_size:
            response["context"] = ctx
    return response


# ============================================================================
# Response Enhancer
# ============================================================================

ResponseEnhancer = (
    Callable[[dict[str, Any]], None] | Callable[[str, dict[str, Any], "ToolContext | None"], None]
)
"""Server-wide callback that stamps cross-cutting fields on every response.

Configure it via ``serve(response_enhancer=...)`` (or the matching
:class:`~adcp.server.ServeConfig` field). The framework calls it after the
context-echo envelope is assembled and before schema validation, on every
response class — framework-tool successes, custom-tool successes
(``get_task_status`` / ``list_tasks``), the pre-auth
``get_adcp_capabilities`` discovery response, and structured ``adcp_error``
responses — on both the MCP and A2A transports.

Two arities are supported, dispatched by positional-parameter count:

- **Context-blind** ``(result_dict) -> None`` — the common case; mutate the
  response dict in place to stamp a field on every response.
- **Context-aware** ``(method_name, result_dict, context) -> None`` — when
  the stamp depends on the tool or the caller. ``context`` is the
  :class:`~adcp.server.ToolContext` for this dispatch, or ``None`` for an
  unauthenticated / pre-auth discovery call.

The enhancer mutates the response dict in place; its return value is
ignored. It runs **synchronously** (it is not awaited). A raised exception
is caught and logged at ``WARNING`` — the un-enhanced response ships rather
than turning a buggy enhancer into a transport error.

Because the enhancer runs *after* the wire response is stripped of any
credential the buyer echoed in ``context``, it cannot re-introduce a
credential into the response envelope.

Idempotency note: the server-side idempotency cache commits the
*pre-enhancement* response, so a replayed request re-runs the enhancer.
Non-idempotent enhancers (timestamps, random IDs) will therefore diverge
between the original response and its replays.
"""


def _enhancer_is_context_aware(enhancer: ResponseEnhancer) -> bool:
    """Return ``True`` when *enhancer* takes the 3-arg context-aware shape.

    Dispatch is by positional-parameter arity: a single positional
    parameter is the context-blind ``(result_dict)`` shape; three is the
    context-aware ``(method_name, result_dict, context)`` shape. A callable
    with ``*args`` is treated as context-aware so adopters writing a
    catch-all signature still receive the method name and context.

    Signature introspection failures (C callables, exotic wrappers) fall
    back to the context-blind shape — the safe default that matches the
    most common adopter intent.
    """
    try:
        sig = inspect.signature(enhancer)
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in sig.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    has_var_positional = any(p.kind is p.VAR_POSITIONAL for p in sig.parameters.values())
    return len(positional) >= 3 or has_var_positional


def _apply_response_enhancer(
    enhancer: ResponseEnhancer | None,
    method_name: str,
    result: dict[str, Any],
    context: ToolContext | None,
) -> dict[str, Any]:
    """Run the configured *enhancer* against *result*, mutating it in place.

    Returns the same ``result`` dict reference (no clone) so callers can use
    the return value or the original interchangeably. When *enhancer* is
    ``None`` the dict is returned unchanged.

    The enhancer is invoked synchronously. Its return value is ignored — it
    must mutate ``result`` in place. A raised exception is caught and logged
    at ``WARNING`` (including *method_name*); the original ``result`` is
    returned un-enhanced so a buggy enhancer never becomes a transport
    error.
    """
    if enhancer is None:
        return result
    try:
        if _enhancer_is_context_aware(enhancer):
            context_aware = cast(
                "Callable[[str, dict[str, Any], ToolContext | None], None]", enhancer
            )
            context_aware(method_name, result, context)
        else:
            context_blind = cast("Callable[[dict[str, Any]], None]", enhancer)
            context_blind(result)
    except Exception:
        logger.warning(
            "response_enhancer raised for %s — shipping the un-enhanced "
            "response. This is a bug in the enhancer, not in the response.",
            method_name,
            exc_info=True,
        )
    return result


# ============================================================================
# Cancellation Helper
# ============================================================================


def cancel_media_buy_response(
    media_buy_id: str,
    canceled_by: str,
    *,
    reason: str | None = None,
    canceled_at: str | None = None,
    affected_packages: list[Any] | None = None,
    revision: int | None = None,
    sandbox: bool = True,
) -> dict[str, Any]:
    """Build a cancellation response with auto-defaults.

    Auto-sets canceled_at to now, status to "canceled", valid_actions to [].
    Requires canceled_by ("buyer" or "seller") - the field developers
    most commonly forget.
    """
    if canceled_by not in ("buyer", "seller"):
        raise ValueError(f"canceled_by must be 'buyer' or 'seller', got {canceled_by!r}")
    resp: dict[str, Any] = {
        "media_buy_id": media_buy_id,
        "status": "canceled",
        "canceled_by": canceled_by,
        "canceled_at": canceled_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "valid_actions": [],
        "sandbox": sandbox,
    }
    if reason is not None:
        resp["reason"] = reason
    if affected_packages is not None:
        resp["affected_packages"] = affected_packages
    if revision is not None:
        resp["revision"] = revision
    return resp
