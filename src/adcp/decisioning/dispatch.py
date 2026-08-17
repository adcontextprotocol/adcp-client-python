"""Dispatch layer for the v6.0 DecisioningPlatform framework.

The dispatch layer ties everything together at the seam between the
existing ``adcp.server`` transport machinery and the new
``DecisioningPlatform`` Protocol-driven adopter shape:

* :func:`validate_platform` — server-boot fail-fast: confirms every
  claimed specialism has its required methods, governance opt-in is
  honored, and ``accounts`` is a real ``AccountStore``.
* :func:`compose_caller_identity` — composite cache scope key
  ``f"{store_qualname}:{account.id}"`` (round-3 D9 — structural
  cross-store isolation).
* :func:`_build_request_context` — the hydration helper that turns a
  ``ToolContext`` + resolved ``Account`` into a typed
  ``RequestContext`` per D2 / D9 / D15.
* :func:`_invoke_platform_method` — the method-call seam. Detects
  async-vs-sync, runs sync on a thread-pool executor with
  ``contextvars`` snapshot, projects ``TaskHandoff`` returns, wraps
  non-``AdcpError`` exceptions to ``INTERNAL_ERROR`` (wire never
  leaks a stack trace).
* :func:`_project_handoff` — TaskHandoff lifecycle: allocates
  ``task_id``, projects the wire ``Submitted`` envelope, kicks off
  the adopter's handoff fn in the background, persists terminal
  artifact via the task registry.

Codegen-emitted ``handler.py`` (Stage 3 next file) calls
``_invoke_platform_method`` from each typed shim; ``serve.py``
(Stage 3 last) wires the executor + registry + middleware.

This module is framework-internal — adopters import nothing from
here. The Protocol contracts adopters write against live in
:mod:`adcp.decisioning.specialisms.*`.
"""

from __future__ import annotations

import asyncio
import contextvars
import difflib
import functools
import inspect
import logging
import os
import typing
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Literal

from adcp.decisioning.account_projection import (
    strip_credentials_from_wire_result,
)
from adcp.decisioning.platform import (
    GOVERNANCE_SPECIALISMS,
    DecisioningCapabilities,
    DecisioningPlatform,
)
from adcp.decisioning.state import _NotYetWiredStateReader
from adcp.decisioning.task_registry import (
    TaskHandoffContext,
    TaskRegistry,
)
from adcp.decisioning.time_budget import (
    RoutedSyncExecution,
    SyncExecutorAdmission,
    _bind_routed_sync_execution,
    submit_supervised,
)
from adcp.decisioning.types import (
    AdcpError,
    TaskHandoff,
    WorkflowHandoff,
    is_task_handoff,
    is_workflow_handoff,
)
from adcp.decisioning.webhook_emit import (
    SPEC_WEBHOOK_TASK_TYPES,
    _extract_push_notification_url_and_token,
    emit_terminal_completion_webhook,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import BaseModel, ValidationError

    from adcp.decisioning.accounts import AccountStore
    from adcp.decisioning.context import AuthInfo, RequestContext
    from adcp.decisioning.registry import BuyerAgent
    from adcp.decisioning.types import Account
    from adcp.server.base import ToolContext
    from adcp.webhook_sender import WebhookSender
    from adcp.webhook_supervisor import WebhookDeliverySupervisor

    WebhookDeliveryTarget = WebhookSender | WebhookDeliverySupervisor

logger = logging.getLogger(__name__)

# Strong references for synchronous adopter lifecycles that outlive a
# cancelled request. A Python thread cannot be cancelled; its completion hooks
# must still settle durable proposal/idempotency state.
_SUPERVISED_SYNC_LIFECYCLES: set[asyncio.Task[Any]] = set()

# ---------------------------------------------------------------------------
# Specialism enum — spec slugs known to the framework
# ---------------------------------------------------------------------------

#: Canonical spec specialism enum, mirrored verbatim from
#: ``schemas/cache/enums/specialism.json``. Used by
#: :func:`validate_platform` for typo suggestions: an unknown slug that
#: close-matches anything in ``SPEC_SPECIALISM_ENUM`` is treated as a
#: typo (hard fail with "did you mean…"); a slug that doesn't close-match
#: any spec value is forward-compat-tolerated via UserWarning.
#:
#: Drift policy: when the spec adds a specialism, bump this constant.
#: A unit test (``test_spec_specialism_enum_matches_schema_cache``) reads
#: the on-disk enum and asserts equality, so out-of-band drift surfaces
#: in CI.
SPEC_SPECIALISM_ENUM: frozenset[str] = frozenset(
    {
        "audience-sync",
        "brand-rights",
        "collection-lists",
        "content-standards",
        "creative-ad-server",
        "creative-generative",
        "creative-template",
        "creative-transformers",
        "governance-aware-seller",
        "governance-delivery-monitor",
        "governance-spend-authority",
        "property-lists",
        "sales-broadcast-tv",
        "sales-catalog-driven",
        "sales-guaranteed",
        "sales-non-guaranteed",
        "sales-proposal-mode",
        "sales-social",
        "signal-marketplace",
        "signal-owned",
        "signed-requests",
        "sponsored-intelligence",
    }
)


# ---------------------------------------------------------------------------
# REQUIRED_METHODS_PER_SPECIALISM — what each specialism must implement
# ---------------------------------------------------------------------------

#: Required platform methods per specialism. ``validate_platform`` walks
#: ``capabilities.specialisms`` against this map at server boot and
#: fail-fasts when a claimed specialism is missing methods.
#:
#: Keyed by specialism slug — every key MUST also appear in
#: :data:`SPEC_SPECIALISM_ENUM` (the on-disk spec enum). v6.0 ships
#: enforced method coverage for the sales-* slugs the framework provides
#: a Protocol for; non-sales spec slugs (audience-sync, signal-*,
#: creative-*, governance-*, brand-rights, collection-lists,
#: content-standards, property-lists) emit an "unenforced specialism"
#: UserWarning until their per-Protocol coverage lands in v6.1+.
#:
#: Drift policy: when a specialism Protocol gains a required method,
#: bump this map AND add a v6.x migration note. The v6.0 enforced subset
#: is intentionally narrow — adding a method here without a Protocol
#: behind it would break adopters mid-version.
REQUIRED_METHODS_PER_SPECIALISM: dict[str, frozenset[str]] = {
    # Five sales-* specialisms share the unified hybrid SalesPlatform
    # surface. Per the SalesPlatform docstring, every sales-* claim
    # requires the five core methods. The four optional methods
    # (get_media_buys, provide_performance_feedback,
    # list_creative_formats_legacy, list_creatives) are present-or-absent —
    # not enforced here. The v6.0 rc.1 spec mandates them; v6.0 alpha
    # tolerates absence so adopters can ship in stages.
    "sales-non-guaranteed": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-guaranteed": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-broadcast-tv": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-social": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-proposal-mode": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    # Catalog-driven requires the sales core PLUS sync_catalogs (to push
    # the inventory taxonomy).
    "sales-catalog-driven": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
            "sync_catalogs",
        }
    ),
    # Signals specialisms. Marketplace/provisioned signals require
    # activation onto destinations; seller-owned signals are already
    # usable on that seller's inventory, so discovery is sufficient.
    "signal-marketplace": frozenset(
        {
            "get_signals",
            "activate_signal",
        }
    ),
    "signal-owned": frozenset(
        {
            "get_signals",
        }
    ),
    # Audience-sync — first-party CRM audience push with delta upsert.
    # ``poll_audience_statuses`` is an adopter-internal helper not
    # surfaced as a wire tool; ``sync_audiences`` is the only required
    # method for spec coverage.
    "audience-sync": frozenset(
        {
            "sync_audiences",
        }
    ),
    # Creative builder specialisms — template-driven transform,
    # account-scoped transformer builds, AND brief-driven generation
    # share the unified
    # ``CreativeBuilderPlatform`` Protocol per JS commit ``841616d7``
    # (F13). ``build_creative`` is the only wire-required method;
    # ``preview_creative``, ``refine_creative``, ``sync_creatives`` are
    # optional and surface ``UNSUPPORTED_FEATURE`` to buyers when
    # missing.
    "creative-template": frozenset(
        {
            "build_creative_legacy",
        }
    ),
    "creative-generative": frozenset(
        {
            "build_creative_legacy",
        }
    ),
    "creative-transformers": frozenset(
        {
            "build_creative_legacy",
        }
    ),
    # Creative-ad-server — stateful library, per-creative pricing, tag
    # generation, per-creative delivery. ``preview_creative`` is
    # required here (distinct from CreativeBuilderPlatform where it's
    # optional) — buyers expect preview surface from any stateful
    # library.
    "creative-ad-server": frozenset(
        {
            "build_creative_legacy",
            "preview_creative_legacy",
            "list_creatives",
            "get_creative_delivery",
        }
    ),
    # Governance-AGENT specialisms — both share the unified
    # ``CampaignGovernancePlatform`` Protocol. The spec's third
    # governance slug, ``governance-aware-seller``, names a SELLER
    # claim (sales-* archetype that composes with a governance agent
    # via sync_governance + check_governance) — it does NOT
    # implement CampaignGovernancePlatform. Stays unenforced until
    # sync_governance handler shim wiring lands for sales adopters.
    #
    # SECURITY GATE: claiming any governance-* slug also requires
    # ``capabilities.governance_aware=True`` — enforced independently
    # by ``validate_platform`` against ``GOVERNANCE_SPECIALISMS``.
    # Required-method coverage and governance-aware are independent
    # gates; both fire.
    "governance-spend-authority": frozenset(
        {
            "check_governance",
            "sync_plans",
            "report_plan_outcome",
            "get_plan_audit_logs",
        }
    ),
    "governance-delivery-monitor": frozenset(
        {
            "check_governance",
            "sync_plans",
            "report_plan_outcome",
            "get_plan_audit_logs",
        }
    ),
    # Brand-rights — identity discovery + licensing for branded
    # inventory. Three required methods, all sync. ``acquire_rights``
    # has 3-arm discriminated success union (acquired / pending /
    # rejected) — rejection-as-data, not AdcpError.
    "brand-rights": frozenset(
        {
            "get_brand_identity",
            "get_rights",
            "acquire_rights",
        }
    ),
    # Content-standards — brand safety policies, content adjacency
    # rules, per-creative compliance. Six required methods (CRUD +
    # calibration + delivery validation); analyzer reads
    # (``get_media_buy_artifacts``, ``get_creative_features``) are
    # optional and surface ``UNSUPPORTED_FEATURE`` to buyers when
    # missing.
    "content-standards": frozenset(
        {
            "list_content_standards",
            "get_content_standards",
            "create_content_standards",
            "update_content_standards",
            "calibrate_content",
            "validate_content_delivery",
        }
    ),
    # Property-lists / Collection-lists — list-publishing specialisms
    # with parallel CRUD shapes. Each has 5 required methods (create,
    # update, get, list, delete) on its respective list type. Tokens
    # are scoped per-seller for revocation; compromise-driven
    # revocation MUST trigger the delete path.
    "property-lists": frozenset(
        {
            "create_property_list",
            "update_property_list",
            "get_property_list",
            "list_property_lists",
            "delete_property_list",
        }
    ),
    "collection-lists": frozenset(
        {
            "create_collection_list",
            "update_collection_list",
            "get_collection_list",
            "list_collection_lists",
            "delete_collection_list",
        }
    ),
    "sponsored-intelligence": frozenset(
        {
            "si_get_offering",
            "si_initiate_session",
            "si_send_message",
            "si_terminate_session",
        }
    ),
}


# ---------------------------------------------------------------------------
# RECOMMENDED_METHODS_PER_SPECIALISM — v6.0 rc.1 promotion staging
# ---------------------------------------------------------------------------

#: Methods the SalesPlatform Protocol docstring marks "Required when
#: claiming any sales-* specialism in v6.0 rc.1+" but which the v6.0 alpha
#: enforced subset (REQUIRED_METHODS_PER_SPECIALISM) tolerates as absent.
#: ``validate_platform`` emits one ``UserWarning`` per missing method
#: pointing the adopter at the Protocol docstring; in strict mode
#: (``ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM=1``) the same misses
#: project to ``AdcpError("INVALID_REQUEST")`` instead.
#:
#: Promotion path (DX-423): when v6.0 rc.1 ships, fold these entries
#: into ``REQUIRED_METHODS_PER_SPECIALISM`` and delete this map. Until
#: then the soft-warn lets adopters mid-upgrade ship without a hard
#: break, while still flagging the gap loudly enough that nobody ships
#: a sales-* platform missing four spec-required methods by accident
#: (the v3 ref seller did exactly that until the deep review caught it).
#:
#: All five "narrow" sales-* slugs share the same recommended set; the
#: catalog-driven slug inherits the same four (its REQUIRED set already
#: adds ``sync_catalogs``).
_SALES_RECOMMENDED: frozenset[str] = frozenset(
    {
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats_legacy",
        "list_creatives",
    }
)
RECOMMENDED_METHODS_PER_SPECIALISM: dict[str, frozenset[str]] = {
    "sales-non-guaranteed": _SALES_RECOMMENDED,
    "sales-guaranteed": _SALES_RECOMMENDED,
    "sales-broadcast-tv": _SALES_RECOMMENDED,
    "sales-social": _SALES_RECOMMENDED,
    "sales-proposal-mode": _SALES_RECOMMENDED,
    "sales-catalog-driven": _SALES_RECOMMENDED,
}

#: Env var that flips recommended-method misses from ``UserWarning`` to
#: ``AdcpError("INVALID_REQUEST")`` at server boot. Set to ``"1"`` to
#: opt in; any other value (including ``"true"``, unset, empty) leaves
#: the soft-warn behavior. Adopters who've completed the v6.0 rc.1
#: surface migration should set this in CI to lock the gain in.
_STRICT_VALIDATE_ENV = "ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM"


def _strict_validate_platform() -> bool:
    """True when the strict-validate env var is set to ``"1"``."""
    # Inline the literal name so docstring-vs-code consistency tests can
    # match it via plain regex (the test scans for ``os.environ.get("FOO")``
    # patterns and doesn't follow the indirection through ``_STRICT_VALIDATE_ENV``).
    return os.environ.get("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", "") == "1"


# ---------------------------------------------------------------------------
# INTERNAL_ERROR breadcrumbs (Emma AudioStack P2)
# ---------------------------------------------------------------------------

#: Substring suffixes that flag a ctx_metadata key as credential-shaped.
#: Lowercased for case-insensitive matching against the user-supplied
#: key. The list intentionally errs broad — a key like
#: ``"upstream.api_key"`` belongs in :class:`AuthInfo.credential`, not
#: ``ctx.metadata`` which round-trips into responses.
#:
#: Drift policy: when the spec or adopter conventions add a new
#: credential-shaped suffix, append here. The gate is fail-closed by
#: design — false positives require the adopter to rename the key, NOT
#: silently echo the credential.
_CREDENTIAL_SHAPED_KEY_SUFFIXES: tuple[str, ...] = (
    "credential",
    "credentials",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "bearer",
)


def _validate_ctx_metadata_credentials(metadata: Any) -> None:
    """Fail-closed gate: ctx.metadata must not carry credential-shaped
    keys.

    The framework projects buyer-supplied ``context`` extensions into
    ``tool_ctx.metadata`` and echoes context back on responses per
    the AdCP spec. An adopter who treats ``metadata`` as a generic
    KV bucket can accidentally round-trip a credential to the buyer.
    The ergonomic path for credentials is
    :class:`AuthInfo.credential` / typed credential classes
    (:class:`ApiKeyCredential`, :class:`OAuthCredential`,
    :class:`HttpSigCredential`); ``ctx.metadata`` is for non-secret
    request-scope hints (correlation ids, feature flags, trace ids).

    Matches against any key whose lowercased form ends with one of
    :data:`_CREDENTIAL_SHAPED_KEY_SUFFIXES`. Sub-keys at any nesting
    depth count — a buyer-supplied
    ``{"upstream": {"api_token": "..."}}`` is rejected the same as
    a flat ``{"api_token": "..."}``.

    :raises ValueError: when any credential-shaped key is found. The
        exception message names the offending key path so the adopter
        knows which field to migrate to ``AuthInfo.credential``.
    """
    if not metadata:
        return
    if not isinstance(metadata, dict):
        return
    for key, value in metadata.items():
        if isinstance(key, str):
            lower = key.lower()
            for suffix in _CREDENTIAL_SHAPED_KEY_SUFFIXES:
                if lower.endswith(suffix):
                    raise ValueError(
                        "ctx_metadata may not contain credential-shaped keys; "
                        "use AuthInfo.credential (or a typed credential class "
                        "like ApiKeyCredential / OAuthCredential / "
                        "HttpSigCredential) instead. Found: "
                        f"{key!r} (matched suffix {suffix!r}). "
                        "ctx.metadata round-trips into response context per "
                        "the AdCP spec; placing a credential here echoes it "
                        "to the buyer."
                    )
        if isinstance(value, dict):
            try:
                _validate_ctx_metadata_credentials(value)
            except ValueError as exc:
                # Re-raise with the parent key prefixed so the diagnostic
                # walks the adopter to the offending path.
                raise ValueError(f"In ctx_metadata[{key!r}]: {exc}") from None
        elif isinstance(value, list):
            # Lists of dicts are a realistic shape (e.g.,
            # ``{"upstream_configs": [{"api_token": "..."}]}``). Walk
            # each item with the same recursion so a credential-shaped
            # key buried in any list element fails the gate. Nested
            # lists (``[[{"token": "x"}]]``) recurse via the same
            # branch.
            try:
                _walk_ctx_metadata_list(value)
            except ValueError as exc:
                raise ValueError(f"In ctx_metadata[{key!r}]: {exc}") from None


def _walk_ctx_metadata_list(items: list[Any]) -> None:
    """Recurse into a list collected from ``ctx_metadata`` and reject
    any credential-shaped key found in a dict element.

    Nested lists are walked through this same function. Non-dict,
    non-list items (strings, numbers, None) are ignored — only
    container types can hide a credential-shaped key.
    """
    for index, item in enumerate(items):
        if isinstance(item, dict):
            try:
                _validate_ctx_metadata_credentials(item)
            except ValueError as exc:
                raise ValueError(f"[{index}]: {exc}") from None
        elif isinstance(item, list):
            try:
                _walk_ctx_metadata_list(item)
            except ValueError as exc:
                raise ValueError(f"[{index}]: {exc}") from None


def _extract_request_context(params: Any) -> dict[str, Any] | None:
    """Pull the buyer-supplied ``context`` extension off the original
    request for ``TaskRecord.request_context``.

    The framework hands platform methods a typed Pydantic model in
    production; test fixtures occasionally pass a raw dict.
    ``model_dump`` failures (rare — Pydantic models with
    non-serializable ``extra='allow'`` fields) log + return ``None``
    so downstream tasks/get reads simply omit ``context`` rather than
    surfacing a partial / corrupted value. Buyers polling tasks/get
    won't see a context echo on those (rare) requests, but their
    failure-mode is "missed correlation," not "corrupted wire shape".

    Returns ``None`` when the request had no context field, the
    coercion failed, or ``params`` itself was ``None`` (test fixtures
    invoking ``_project_handoff`` directly without going through
    ``_invoke_platform_method``). The 64KB amplification cap from
    :func:`adcp.server.helpers.inject_context` does NOT apply at this
    layer — the registry is server-internal storage, not a wire echo
    surface; size-bounded enforcement on tasks/get reads should live
    on the projection layer if required.
    """
    if params is None:
        return None
    if isinstance(params, dict):
        ctx = params.get("context")
        return dict(ctx) if isinstance(ctx, dict) else None
    if hasattr(params, "model_dump") and callable(params.model_dump):
        try:
            dumped = params.model_dump(mode="json", exclude_none=False)
        except Exception:
            logger.warning(
                "request_params model_dump failed for %s; tasks/get context "
                "echo skipped (correlation IDs lost). Verify the request "
                "model serializes cleanly via model_dump.",
                type(params).__name__,
                exc_info=True,
            )
            return None
        ctx = dumped.get("context") if isinstance(dumped, dict) else None
        return dict(ctx) if isinstance(ctx, dict) else None
    return None


def _internal_error_message(method_name: str, exc: BaseException) -> str:
    """Build the wire-side ``message`` for an INTERNAL_ERROR wrap.

    Adopters debugging "An internal error occurred" with no breadcrumb
    have to grep server logs to even see which exception fired (Emma
    AudioStack P2). Surfacing the exception class name in the wire
    message gives them a starting point without leaking the traceback.
    """
    cls_name = type(exc).__name__
    return f"Platform method {method_name!r} raised {cls_name}; see details for cause"


def _exception_cause_details(exc: BaseException) -> dict[str, Any]:
    """Return the shared sanitized exception-type breadcrumb."""
    return {"caused_by": {"type": type(exc).__name__}}


def _internal_error_details(exc: BaseException) -> dict[str, Any]:
    """Build the wire-side ``details`` payload for an INTERNAL_ERROR
    wrap.

    ``details.caused_by`` carries ONLY the exception class name —
    ``"AttributeError"`` (typo-shaped), ``"KeyError"``
    (missing-config-shaped), ``"ConnectionError"`` (network-shaped) —
    enough for the seller dev to triage at a glance. The exception's
    ``str()`` is deliberately omitted: any truncation length large
    enough to be useful (200 chars) is also large enough to leak a
    full OAuth client secret or bearer token if the adopter raised
    on secret material. The full traceback (with message) lives in
    the server log via ``logger.exception``; only the wire response
    is sanitized to a class-name breadcrumb.

    **``caused_by.type`` is a debug breadcrumb, not a wire contract.**
    Buyers built against the JS SDK won't see Python-flavoured class
    names from JS sellers — only Python sellers leak Python types.
    Treat this field as "hint to the seller dev reading their own
    server logs," NOT as something to branch on programmatically
    cross-language. The AdCP spec at ``schemas/cache/core/error.json``
    keeps ``details`` as ``additionalProperties: true`` so this is
    spec-compliant; it's just not portable. Buyer agents that want
    structured retry/fix/abandon classification should read
    ``recovery`` (terminal/correctable/transient) which IS the
    cross-language contract.

    **ValidationError special case** (Stability AI Emma P1 from the
    post-#340 matrix): when the platform method raises a pydantic
    ``ValidationError`` directly — typically because the seller's
    code constructed an invalid response model — the wire used to
    say "see details for cause" with no actual details. We now also
    emit ``details.validation_errors`` carrying the narrowed
    field-path list (using
    :func:`adcp.types.error_narrowing.narrow_union_errors` to filter
    discriminated-union noise). The buyer agent gets actionable
    field paths; the seller dev sees the same in their wire log.
    Pydantic ValidationError is the only common adopter exception
    where a structured field list is meaningful, so we don't
    generalize this to other exception types.
    """
    details = _exception_cause_details(exc)
    # Try to import lazily so a future refactor that splits the
    # validation tooling can't ripple through the dispatch layer.
    try:
        from pydantic import ValidationError

        from adcp.types.error_narrowing import narrow_union_errors
    except Exception:
        return details
    if isinstance(exc, ValidationError):
        try:
            errors_list = exc.errors(
                include_input=False,
                include_context=False,
                include_url=False,
            )
            details["validation_errors"] = list(narrow_union_errors(errors_list))
        except Exception:
            # Defensive — never let a narrowing bug 500 the wire.
            # The exception type still lets adopters triage via server logs.
            pass
    return details


# ---------------------------------------------------------------------------
# _validation_error_to_invalid_request — request-validation error wrapper
# ---------------------------------------------------------------------------


def _validation_error_to_invalid_request(method_name: str, exc: ValidationError) -> AdcpError:
    """Convert a ``pydantic.ValidationError`` raised inside a platform method
    to ``AdcpError('INVALID_REQUEST', recovery='correctable')``.

    The generic ``except Exception`` handler wraps all unhandled exceptions as
    ``INTERNAL_ERROR``. But a ``ValidationError`` from a platform delegate
    almost always means the buyer supplied a request field that failed the
    seller's stricter schema — semantically an ``INVALID_REQUEST`` the buyer
    can correct. This matches the behaviour of
    :func:`_coerce_params_to_platform_type` for the annotation-coercion path.

    Uses :func:`adcp.types.error_narrowing.narrow_union_errors` to strip
    discriminated-union noise from the ``details.validation_errors`` list.
    Both ``narrow_union_errors`` and ``exc.errors()`` are part of stable
    in-repo / Pydantic APIs respectively, so a failure here would be a
    genuine bug worth surfacing rather than masking with a fallback.
    """
    from adcp.types.error_narrowing import narrow_union_errors

    raw = exc.errors(include_input=False, include_context=False, include_url=False)
    errors: list[Any] = list(narrow_union_errors(raw))
    first: dict[str, Any] = dict(errors[0]) if errors else {}
    field_path = ".".join(str(loc) for loc in first.get("loc", ()))
    msg = first.get("msg", "validation failed")
    return AdcpError(
        "INVALID_REQUEST",
        message=(
            f"Request validation failed for {method_name!r}: {msg}"
            + (f" (field: {field_path!r})" if field_path else "")
        ),
        field=field_path or None,
        recovery="correctable",
        details={"validation_errors": errors},
    )


# ---------------------------------------------------------------------------
# validate_platform — server-boot fail-fast
# ---------------------------------------------------------------------------


def validate_platform(platform: DecisioningPlatform) -> None:
    """Server-boot validator — fail-fast before the first request.

    Checks (in order):

    1. ``platform.capabilities`` is a populated
       :class:`DecisioningCapabilities` (not the base default).
    2. ``platform.accounts`` is a real :class:`AccountStore`
       (anything truthy with a ``resolve`` method) — None catches
       subclasses that forgot to attach a store.
    3. Each claimed specialism's required methods are implemented
       on the platform subclass. Unknown specialisms emit
       ``UserWarning`` (forward-compat with v6.x+ specs); known
       specialisms missing methods raise an INVALID_REQUEST error.
    4. Each claimed specialism's *recommended* methods (the v6.0 rc.1
       staging set in :data:`RECOMMENDED_METHODS_PER_SPECIALISM` —
       sales-* surface broadening per DX-423) are implemented on the
       platform subclass. Misses emit one ``UserWarning`` per
       method (deduped across overlapping specialisms). Setting
       ``ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM=1`` flips the soft
       warning into a hard INVALID_REQUEST error.
    5. **Governance opt-in fail-fast (D15 round-4):** if any claimed
       specialism is in :data:`GOVERNANCE_SPECIALISMS` AND
       ``capabilities.governance_aware`` is False AND the platform
       hasn't wired a custom :class:`StateReader` (i.e., the dispatch
       hydration helper would supply ``_NotYetWiredStateReader``),
       raise. Silent governance-gate skipping is a security
       regression the framework refuses to ship.

    Catches per-validator exceptions and re-projects to
    ``AdcpError("INVALID_REQUEST")`` so server boot never crashes
    with a raw stack trace — the operator sees one structured
    diagnostic per problem (Round-4 Emma #16).

    :raises AdcpError: on any blocking validation failure. The error
        ``details`` carry per-issue diagnostics for operator triage.
    """
    if not isinstance(platform.capabilities, DecisioningCapabilities):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform.capabilities must be a "
                "DecisioningCapabilities instance — found "
                f"{type(platform.capabilities).__name__!r}. Subclasses MUST "
                "set ``capabilities = DecisioningCapabilities(...)`` on the "
                "class body."
            ),
            recovery="terminal",
        )

    accounts = getattr(platform, "accounts", None)
    if accounts is None:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform.accounts is None — subclasses MUST set "
                "an AccountStore (SingletonAccounts, ExplicitAccounts, "
                "FromAuthAccounts, or a custom AccountStore impl) on the "
                "class body."
            ),
            recovery="terminal",
        )

    # Specialism-method coverage.
    # ``capabilities.specialisms`` is ``list[Specialism | str]`` —
    # spec-known entries are coerced to enum at construction; novel /
    # pre-spec slugs pass through as strings (so this validator can
    # surface them with typo-vs-novel diagnostics). Lookup tables are
    # keyed by AdCP slug strings, so extract a slug regardless of form.
    missing: list[tuple[str, str]] = []
    unknown: list[str] = []
    governance_specialisms_claimed: list[str] = []
    for entry in platform.capabilities.specialisms:
        specialism = entry.value if hasattr(entry, "value") else entry
        if specialism in GOVERNANCE_SPECIALISMS:
            governance_specialisms_claimed.append(specialism)
        try:
            required = REQUIRED_METHODS_PER_SPECIALISM.get(specialism)
        except Exception as exc:
            # Defensive: a custom REQUIRED_METHODS_PER_SPECIALISM impl
            # (test-monkeypatch, etc.) that raises must not crash boot.
            # Round-4 Emma #16 — wrap validator throws.
            logger.warning(
                "REQUIRED_METHODS_PER_SPECIALISM lookup raised for %r: %r",
                specialism,
                exc,
            )
            required = None
        if required is None:
            unknown.append(specialism)
            continue
        for method_name in required:
            if not _has_overridden_method(platform, method_name):
                missing.append((specialism, method_name))

    # AdCP 3.2 compact lifecycle claims are independent from the legacy
    # sales-* facade requirements. Every advertised lifecycle tool must have
    # a same-named platform method; old-only 3.0/3.1 implementations remain
    # valid when ``lifecycle_tools`` is omitted.
    media_buy_caps = platform.capabilities.media_buy
    lifecycle_tools = getattr(media_buy_caps, "lifecycle_tools", None) or []
    for entry in lifecycle_tools:
        method_name = entry.value if hasattr(entry, "value") else str(entry)
        if not _has_overridden_method(platform, method_name):
            missing.append(("media_buy.lifecycle_tools", method_name))

    if unknown:
        # Three buckets:
        #   - typo: close-match to any spec slug → hard fail with hint
        #   - unenforced: spec-recognized but no method-coverage rules in
        #     this framework version → soft UserWarning (Protocol lands
        #     in v6.1+)
        #   - novel: not in spec at all → forward-compat UserWarning
        # The typo detector compares against the full spec enum (not just
        # REQUIRED_METHODS keys) so misspelling a spec slug we don't yet
        # enforce still surfaces as a typo.
        spec_known = sorted(SPEC_SPECIALISM_ENUM)
        typo_suggestions: list[tuple[str, str]] = []
        unenforced: list[str] = []
        novel: list[str] = []
        for slug in unknown:
            if slug in SPEC_SPECIALISM_ENUM:
                # Spec-recognized but not in REQUIRED_METHODS — adopter
                # claimed a real spec slug whose Protocol hasn't shipped
                # method-coverage rules yet.
                unenforced.append(slug)
                continue
            close = difflib.get_close_matches(slug, spec_known, n=1, cutoff=0.7)
            if close:
                typo_suggestions.append((slug, close[0]))
            else:
                novel.append(slug)

        if typo_suggestions:
            hints = "; ".join(
                f"{slug!r} → did you mean {match!r}?" for slug, match in sorted(typo_suggestions)
            )
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    f"DecisioningPlatform claims unknown specialism(s) "
                    f"that look like typos: {hints}. "
                    "Forward-compat tolerance applies only to genuinely "
                    "novel specialism slugs (not close spelling matches). "
                    f"Known spec specialisms: {spec_known}"
                ),
                recovery="terminal",
                details={
                    "typo_suggestions": [
                        {"claimed": slug, "did_you_mean": match} for slug, match in typo_suggestions
                    ],
                    "spec_specialisms": spec_known,
                },
            )

        if unenforced:
            warnings.warn(
                (
                    f"DecisioningPlatform claims spec-recognized specialism(s) "
                    f"{sorted(unenforced)!r} that this framework version "
                    f"doesn't yet enforce method coverage for. The claim is "
                    f"valid; required-method validation is skipped until the "
                    f"per-Protocol coverage lands. Implement the spec methods "
                    f"on your platform subclass so buyers don't 404."
                ),
                UserWarning,
                stacklevel=2,
            )

        if novel:
            warnings.warn(
                (
                    f"DecisioningPlatform claims novel specialism(s) "
                    f"{sorted(novel)!r} that aren't in the spec enum at "
                    f"schemas/cache/enums/specialism.json. Your framework "
                    f"version predates the spec, OR you're piloting a future "
                    f"specialism. Required-method validation skipped. "
                    f"Known spec specialisms: {spec_known}"
                ),
                UserWarning,
                stacklevel=2,
            )

    if missing:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform claims specialisms but is missing "
                f"required methods: {missing}. Implement each on your "
                "subclass or remove the specialism from "
                "capabilities.specialisms."
            ),
            recovery="terminal",
            details={"missing": [{"specialism": s, "method": m} for s, m in missing]},
        )

    # Recommended (v6.0 rc.1 staging) coverage — soft-warn by default,
    # hard-fail under ``ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM=1``.
    # Dedup by method name: a platform claiming both ``sales-guaranteed``
    # and ``sales-non-guaranteed`` shares the same recommended set, so
    # ``get_media_buys`` should warn once, not twice. We walk specialisms
    # in declared order and remember the first specialism that surfaced
    # each missing method — that becomes the "blame" specialism in the
    # diagnostic.
    recommended_missing: list[tuple[str, str]] = []
    seen_methods: set[str] = set()
    for entry in platform.capabilities.specialisms:
        specialism = entry.value if hasattr(entry, "value") else entry
        recommended = RECOMMENDED_METHODS_PER_SPECIALISM.get(specialism)
        if recommended is None:
            continue
        for method_name in sorted(recommended):
            if method_name in seen_methods:
                continue
            if not _has_overridden_method(platform, method_name):
                recommended_missing.append((specialism, method_name))
                seen_methods.add(method_name)

    if recommended_missing:
        if _strict_validate_platform():
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "DecisioningPlatform claims sales-* specialism(s) but is "
                    f"missing v6.0 rc.1 required methods: {recommended_missing}. "
                    "Strict mode is enabled "
                    f"({_STRICT_VALIDATE_ENV}=1); implement each on your "
                    "subclass. See the SalesPlatform Protocol docstring at "
                    "src/adcp/decisioning/specialisms/sales.py:184-227 for the "
                    "canonical method list."
                ),
                recovery="terminal",
                details={
                    "missing_recommended": [
                        {"specialism": s, "method": m} for s, m in recommended_missing
                    ],
                    "strict_env_var": _STRICT_VALIDATE_ENV,
                },
            )
        # ``stacklevel=3`` so the warning points at the adopter's
        # ``serve(platform)`` call site, not the SDK internals
        # (validate_platform is invoked from serve, which is invoked by
        # the adopter — three frames up lands on adopter code).
        for specialism, method_name in recommended_missing:
            warnings.warn(
                (
                    f"DecisioningPlatform claims {specialism!r} but is missing "
                    f"{method_name!r} — required by the SalesPlatform Protocol "
                    "for any sales-* specialism in v6.0 rc.1+. See the Protocol "
                    "docstring at src/adcp/decisioning/specialisms/sales.py:"
                    "184-227 for the full required method list. The framework "
                    "currently soft-warns to ease v6.0 rc.1 migration; set "
                    f"{_STRICT_VALIDATE_ENV}=1 to fail-fast at boot instead."
                ),
                UserWarning,
                stacklevel=3,
            )

    # Governance opt-in fail-fast (D15 round-4).
    if governance_specialisms_claimed and not platform.capabilities.governance_aware:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"Platform claims governance-* specialism(s) "
                f"{governance_specialisms_claimed!r} but "
                "capabilities.governance_aware is False. Set "
                "governance_aware=True AND wire a custom StateReader that "
                "returns real GovernanceContextJWS values, OR drop the "
                "governance-* specialism claim. Silent governance-gate "
                "skipping is a security boundary; the framework refuses "
                "to ship that. See "
                "docs/proposals/decisioning-platform-dispatch-design.md#d15"
            ),
            recovery="terminal",
            details={
                "governance_specialisms": sorted(governance_specialisms_claimed),
                "governance_aware": False,
            },
        )


def _has_overridden_method(platform: DecisioningPlatform, method_name: str) -> bool:
    """True when the platform subclass provides ``method_name``.

    The base :class:`DecisioningPlatform` class itself doesn't define
    specialism methods (D11 — base is intentionally minimal). So
    ``hasattr(platform, method_name)`` is sufficient: if the attribute
    exists, the subclass put it there.
    """
    return hasattr(platform, method_name) and callable(getattr(platform, method_name))


# ---------------------------------------------------------------------------
# compose_caller_identity — D9 round-3 composite cache scope key
# ---------------------------------------------------------------------------


def compose_caller_identity(
    account: Account[Any],
    store: AccountStore[Any],
) -> str:
    """Compose the cache scope key from ``module + qualname + account.id``.

    Round-3 D9 + Round-4 review: the framework's idempotency middleware
    reads ``ctx.caller_identity`` for cache scoping. Using ``account.id``
    alone leaks across stores when two adopters use different
    ``AccountStore`` impls but happen to mint colliding ids. The
    composite ``f"{store_module}.{store_qualname}:{account.id}"`` gives
    structural cross-store isolation at zero coordination cost.

    Includes ``__module__`` because ``__qualname__`` is the dotted path
    *within* a module — two ``MyStore`` classes in different packages
    share the same qualname. Without the module prefix the isolation
    promise breaks across cross-package re-implementations.

    Empty / whitespace ``account.id`` raises ``AdcpError`` —
    ``Account(id="")`` would silently collapse every tenant whose
    AccountStore returns the empty default into a single cache scope.
    The dataclass default ``Account(id="<unset>")`` is also rejected so
    a misconfigured store that forgets to populate ``id`` fails fast
    rather than leaking buy-side data.

    Within-store collisions (one impl, identical ``account.id`` for two
    distinct accounts) remain an adopter bug at
    ``AccountStore.resolve``; the framework can't structurally prevent
    that without a runtime registry costing more than it buys.
    """
    if not account.id or not account.id.strip() or account.id == "<unset>":
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"AccountStore returned an account with empty/unset id "
                f"({account.id!r}). The framework refuses to scope the "
                "idempotency cache by an empty key — every empty-id "
                "tenant would share state. Fix: ensure your "
                "AccountStore.resolve always returns Account(id=<non-empty>) "
                "and never leaves the dataclass default."
            ),
            recovery="terminal",
        )
    cls = type(store)
    return f"{cls.__module__}.{cls.__qualname__}:{account.id}"


# ---------------------------------------------------------------------------
# _build_request_context — the hydration helper
# ---------------------------------------------------------------------------


def _build_request_context(
    tool_ctx: ToolContext,
    account: Account[Any],
    auth_info: AuthInfo | None,
    *,
    store: AccountStore[Any] | None = None,
    state_reader: Any | None = None,
    resource_resolver: Any | None = None,
    buyer_agent: BuyerAgent | None = None,
) -> RequestContext[Any]:
    """Hydrate a :class:`RequestContext` per the D2 + D9 + D15 contract.

    Mirrors the TS-side ``to-context.ts:buildRequestContext``. The
    framework supplies the context per request; adopters never
    construct one (the class docstring on
    :class:`adcp.decisioning.RequestContext` carries the
    ``@internal-construction`` note).

    Sets ``ctx.caller_identity`` to the composite cache scope key
    via :func:`compose_caller_identity` when ``store`` is supplied.
    Wiring this is critical — it's the framework's idempotency
    middleware's only safeguard against cross-store cache collisions
    (D9 round-3). When ``store`` is ``None`` (test fixtures, custom
    dispatch paths), falls back to ``tool_ctx.caller_identity``
    verbatim. Production callers from ``handler.py`` always supply
    the store.

    :param tool_ctx: The framework's :class:`ToolContext` from the
        underlying transport. Carries ``request_id``, ``tenant_id``,
        and ``metadata``; we override its caller_identity to the
        composite key.
    :param account: Resolved account from the platform's
        :class:`AccountStore.resolve`.
    :param auth_info: Optional verified principal info — when present
        and carrying a non-``None`` principal, ``auth_principal`` is
        populated from ``auth_info.principal``. Otherwise the helper
        synthesizes an :class:`AuthInfo` (``kind="bearer"``,
        ``credential=None``) from :data:`adcp.server.auth.current_principal`
        — the ContextVar :class:`BearerTokenAuthMiddleware` populates —
        so bearer-flow callers get both a typed ``ctx.auth_info`` and
        ``ctx.auth_principal`` read without reaching into framework-
        private state. ``ctx.auth_info`` stays ``None`` outside both
        flows (no-op for unauthenticated dev fixtures).
    :param store: The AccountStore that produced ``account``. Required
        for the production cache-isolation guarantee; the dispatch
        adapter always supplies it. Test fixtures may pass ``None``
        to skip the composite-key derivation.
    :param state_reader: Custom ``StateReader`` impl. Defaults to the
        v6.0 stub. Accept as a parameter so ``serve()`` can wire a
        v6.1 backing store without touching dispatch.
    :param resource_resolver: Custom ``ResourceResolver`` impl. Same
        plumbing rationale as ``state_reader``.
    """
    # Local import to avoid a circular at module-load time. dispatch.py
    # is imported by serve.py; context.py and accounts.py both reach
    # back into adcp.decisioning, so the cycle is real if we hoist.
    from adcp.decisioning.context import AuthInfo, RequestContext
    from adcp.decisioning.resolve import _NotYetWiredResolver

    # ``auth_info`` / ``auth_principal`` are the typed reads adopter
    # handlers use. Two sources populate them:
    #
    # * Signed-request flows hydrate ``AuthInfo`` upstream and the
    #   adapter passes it as ``auth_info``; ``auth_info.principal``
    #   carries the verified caller label.
    # * Bearer-token flows (:class:`BearerTokenAuthMiddleware`) never
    #   construct an ``AuthInfo``; they stash the principal in the
    #   :data:`adcp.server.auth.current_principal` ContextVar instead.
    #   Synthesize one here so bearer adopters can branch on
    #   ``ctx.auth_info.kind == "bearer"`` (the typed flow
    #   discriminator) without reaching into the framework-private
    #   ContextVar themselves. ``credential=None`` is passed
    #   explicitly so :meth:`AuthInfo.__post_init__` skips the
    #   flat-field synthesis path and the accompanying
    #   :class:`DeprecationWarning` (see context.py:396-426): the
    #   sentinel default fires synthesis, an explicit ``None`` does
    #   not. We don't know the bearer's ``key_id`` / ``scopes`` —
    #   bearer tokens are opaque to the SDK — so we leave those
    #   fields at their dataclass defaults; adopters who want richer
    #   data should write their own ``context_factory``.
    #
    # Local import keeps the layering local — read the bearer ContextVar
    # without forcing a top-level dep on adcp.server.auth.
    from adcp.server.auth import current_principal as _current_principal
    from adcp.server.auth import current_transport as _current_transport

    if auth_info is None:
        bearer_principal = _current_principal.get()
        if bearer_principal is not None:
            auth_info = AuthInfo(
                kind="bearer",
                principal=bearer_principal,
                credential=None,
            )

    auth_principal = auth_info.principal if auth_info is not None else None

    # ctx_metadata credential gate — fail-closed before any platform
    # method sees the metadata. Buyers can populate ``context``
    # extensions on the wire request that the framework projects into
    # ``tool_ctx.metadata``; an adopter who treats ``metadata`` as a
    # general-purpose KV bucket might shove a credential through it,
    # only to discover the value round-trips into the response (the
    # framework echoes context into responses per the AdCP spec).
    # The ergonomic path for credentials is :class:`AuthInfo.credential`
    # / typed credential classes; ``metadata`` is for non-secret
    # request-scope hints. See the "ctx_metadata: write-only credentials
    # prohibited" section in CLAUDE.md.
    _validate_ctx_metadata_credentials(tool_ctx.metadata)

    # Composite cache scope key when store is supplied (production
    # path). Falls back to tool_ctx.caller_identity for test fixtures.
    caller_identity: str | None
    if store is not None:
        caller_identity = compose_caller_identity(account, store)
    else:
        caller_identity = tool_ctx.caller_identity

    # Extract transport from metadata. In production paths RequestMetadata
    # always populates metadata["transport"] before calling the context
    # factory; None here means a test fixture supplied a bare ToolContext.
    raw_transport = tool_ctx.metadata.get("transport")
    if raw_transport not in ("mcp", "a2a", None):
        raise ValueError(
            f"metadata['transport'] must be 'mcp', 'a2a', or absent; got {raw_transport!r}"
        )
    transport: Literal["mcp", "a2a"] | None = raw_transport

    # Set the ContextVar for code outside the handler call stack (webhook
    # services, background helpers) that don't receive a RequestContext.
    # No reset token is saved: asyncio tasks each get their own context
    # copy, so set() is task-scoped and doesn't bleed across requests.
    # Callers that need the previous value must save/restore it themselves
    # (the test suite exercises this via asyncio.copy_context() isolation).
    _current_transport.set(transport)

    # SDK-owned keys set by auth_context_factory / build_context examples
    # ("transport", "tool_name") are framework-internal — strip them from
    # the handler-visible metadata so adopters can't accidentally rely on
    # undocumented dict paths and ctx.transport is the sole typed surface.
    _sdk_metadata_keys = frozenset({"transport", "tool_name"})
    clean_metadata = {k: v for k, v in tool_ctx.metadata.items() if k not in _sdk_metadata_keys}

    # Build the RequestContext with the explicit state/resolve kwargs
    # if provided; otherwise let the dataclass default factories
    # supply the v6.0 stubs.
    ctx_kwargs: dict[str, Any] = {
        "request_id": tool_ctx.request_id,
        "caller_identity": caller_identity,
        "tenant_id": tool_ctx.tenant_id,
        "resolved_adcp_version": tool_ctx.resolved_adcp_version,
        "metadata": clean_metadata,
        "transport": transport,
        "account": account,
        "auth_info": auth_info,
        "auth_principal": auth_principal,
        "buyer_agent": buyer_agent,
    }
    if state_reader is not None:
        ctx_kwargs["state"] = state_reader
    else:
        ctx_kwargs["state"] = _NotYetWiredStateReader()
    if resource_resolver is not None:
        ctx_kwargs["resolve"] = resource_resolver
    else:
        ctx_kwargs["resolve"] = _NotYetWiredResolver()

    return RequestContext(**ctx_kwargs)


# ---------------------------------------------------------------------------
# _invoke_platform_method + _project_handoff — the call seam
# ---------------------------------------------------------------------------


def _coerce_params_to_platform_type(method: Any, params: Any, method_name: str) -> Any:
    """Re-validate ``params`` through the platform method's own type annotation.

    The shim layer (``PlatformHandler``) deserialises the wire dict into
    the library's request type (e.g. ``GetProductsRequest`` with
    ``extra='allow'``).  When the platform subclass overrides the method
    with a *stricter* subclass annotation (e.g. ``extra='forbid'``, custom
    field validators), re-validate so those rules fire at the dispatch
    boundary — not silently bypassed.

    Decision logic:

    * Same type — no-op; avoid double-validation overhead.
    * Strict subclass (``issubclass(annotation, type(params))``) — dump +
      re-validate through the subclass.  A ``ValidationError`` means the
      wire request genuinely violated the subclass contract; raise as
      ``AdcpError('INVALID_REQUEST')`` so the wire envelope carries a
      spec-typed recovery hint rather than ``INTERNAL_ERROR``.
    * No subclass relation, Union annotation, non-Pydantic annotation, or
      ``get_type_hints`` failure — skip coercion and return ``params``
      unchanged.

    Only called when ``arg_projector is None`` (the projector path replaces
    positional args entirely, so ``params`` is unused there).

    .. note::
        The ``model_dump(mode="python") → model_validate()`` roundtrip is
        safe because generated library request types carry no mutating
        ``field_validator`` or ``model_validator`` declarations today.  If
        that changes, a validator declared on the *base* type would fire
        twice: once when the shim builds the library instance, and again
        here.  Revisit if generated types gain mutating validators.
    """
    from pydantic import BaseModel, ValidationError

    if not isinstance(params, BaseModel):
        return params
    try:
        hints = typing.get_type_hints(method)
    except Exception:
        return params

    sig = inspect.signature(method)
    for name, param_obj in sig.parameters.items():
        if name in ("self", "ctx", "context"):
            continue
        if param_obj.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            # *args / **kwargs — not a typed request param; stop searching.
            break
        annotation = hints.get(name)
        if annotation is None:
            # Non-standard signature (e.g. unannotated first arg); skip
            # coercion rather than guessing which param is the request.
            break
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            break
        if annotation is type(params):
            return params  # identical type — skip
        if issubclass(annotation, type(params)):
            try:
                # mode="python" preserves native types (datetime, Decimal,
                # UUID) so subclass validators receive them as-is, not as
                # JSON-serialized strings.
                return annotation.model_validate(params.model_dump(mode="python"))
            except ValidationError as exc:
                errors = exc.errors(include_input=False, include_context=False, include_url=False)
                first: dict[str, Any] = dict(errors[0]) if errors else {}
                field_path = ".".join(str(loc) for loc in first.get("loc", ()))
                msg = first.get("msg", "validation failed")
                raise AdcpError(
                    "INVALID_REQUEST",
                    message=(
                        f"Request validation failed for {method_name!r}: {msg}"
                        + (f" (field: {field_path!r})" if field_path else "")
                    ),
                    field=field_path or None,
                    recovery="correctable",
                ) from exc
        break

    return params


async def _invoke_platform_method(
    platform: DecisioningPlatform,
    method_name: str,
    params: BaseModel,
    ctx: RequestContext[Any],
    *,
    executor: ThreadPoolExecutor,
    registry: TaskRegistry,
    arg_projector: dict[str, Any] | None = None,
    extra_kwargs: dict[str, Any] | None = None,
    on_complete: Callable[[Any], Awaitable[None]] | None = None,
    on_failure: Callable[[BaseException], Awaitable[None]] | None = None,
    webhook_target: WebhookDeliveryTarget | None = None,
    webhook_auto_emit: bool = True,
    pre_handoff_reject: Callable[[], None] | None = None,
    sync_admission: SyncExecutorAdmission | None = None,
) -> Any:
    """Invoke a platform method, projecting hybrid returns.

    Detects async-vs-sync via ``asyncio.iscoroutinefunction`` (NOT
    ``inspect.iscoroutinefunction`` — the latter doesn't unwrap
    ``functools.partial`` until 3.12). Sync methods run on the
    explicit thread-pool executor with an explicit
    ``contextvars.copy_context()`` snapshot so middleware-set
    ContextVars survive the cross-thread hop (D5 + D6).

    ``TaskHandoff`` returns flow through :func:`_project_handoff` to
    allocate a task_id, kick off the handoff fn, and project the
    Submitted envelope.

    Wraps any non-:class:`AdcpError` exception to
    ``AdcpError("INTERNAL_ERROR", recovery="terminal")`` so the wire
    response never leaks a stack trace. Adopters get the original
    exception logged via the framework's observability hooks (the
    raise re-raises the wrapped error; the original is the
    ``__cause__``).

    :param arg_projector: Optional kwargs dict for tools whose Python
        method signature differs from the wire shape (D1
        arg-projection, e.g. ``update_media_buy(media_buy_id, patch,
        ctx)``). Replaces the default positional ``(params, ctx)``
        call entirely. Codegen-emitted shims pass this for those
        tools; most tools call with ``None``.
    :param extra_kwargs: Optional additional kwargs appended to the
        normal ``(params, ctx)`` call, used when the framework injects
        framework-computed values (e.g. ``configs=`` from
        ``ProductConfigStore``). Ignored when ``arg_projector`` is set.

    :param on_complete: Optional framework hook invoked with the
        adapter's typed return value before the dispatch returns to
        the caller. Fired exactly once per call — inline on the sync
        return path, or (forwarded to :func:`_project_handoff`) after
        the bg task lands on the handoff path. Used by v1.5
        create_media_buy to finalize the consumption reservation when
        the typed result is available.

    :param on_failure: Optional framework hook invoked with the
        terminal exception when the adapter raises (sync path) or
        when the handoff fn / on_complete raises (handoff path).
        Symmetric with ``on_complete``. Used by v1.5 create_media_buy
        to release the consumption reservation so the buyer can retry.
        Hook errors are logged but never block exception propagation.

    :param webhook_target: Forwarded to :func:`_project_handoff` so the
        background completion path can deliver the terminal completion /
        failure webhook when the buyer registered
        ``push_notification_config``. The handler wires its
        ``webhook_sender`` / ``webhook_supervisor``; only the handoff
        (async) arm uses it — the sync arm's auto-emit is a separate
        call in the handler shim.

    :param webhook_auto_emit: Forwarded to :func:`_project_handoff` as
        the async task-webhook delivery gate. Production handlers keep
        this enabled because a submitted task with push configuration
        requires a terminal webhook. Direct low-level callers may
        disable it when they own that delivery themselves.

    :param pre_handoff_reject: Optional zero-arg callback invoked when
        the adapter returned a :class:`TaskHandoff`, BEFORE
        :func:`_project_handoff` mints a registry row or launches the
        background task. Raising from it (e.g. an :class:`AdcpError`)
        rejects the handoff with NO side effects — no task row, no
        background work, no completion webhook. The discovery
        wholesale-is-synchronous guard uses this so an adopter handing
        off on a ``wholesale`` request is rejected cleanly instead of
        leaking a task the buyer was told was rejected. Runs only on the
        ``TaskHandoff`` arm; sync / workflow-handoff returns ignore it.
    :param sync_admission: Optional bounded admission controller for a sync
        method. Its permit remains held until the underlying thread future
        actually completes, including after caller cancellation.
    """
    # pydantic is a required dep; import here (not at module level) to mirror
    # the lazy-import discipline used throughout this module.
    from pydantic import ValidationError as _ValidationError  # noqa: PLC0415

    method = getattr(platform, method_name)
    sync_lifecycle_continues = False
    routed_sync_execution: RoutedSyncExecution | None = None
    # Re-validate through the platform method's own annotation when it's a
    # stricter subclass of the shim's already-deserialized type.  Skipped
    # when arg_projector is set — that path replaces positional args entirely.
    #
    # Wrapped in its own try/except so on_failure fires when coercion raises
    # AdcpError before the main try block — proposal-flow callers wire
    # on_failure to release a reservation taken before _invoke_platform_method;
    # if we raise outside the try block the reservation leaks until TTL.
    if arg_projector is None:
        try:
            params = _coerce_params_to_platform_type(method, params, method_name)
        except AdcpError as exc:
            if on_failure is not None:
                await _safe_on_failure_call(on_failure, exc, method_name)
            raise

    try:
        if asyncio.iscoroutinefunction(method):
            # Async router delegates may resolve to synchronous tenant
            # children only after account routing. Propagate the same bounded
            # admission controller and configured executor through ContextVars
            # so that path cannot bypass the timed-sync limit.
            with _bind_routed_sync_execution(sync_admission, executor) as routed_sync_execution:
                if arg_projector is not None:
                    result = await method(**arg_projector, ctx=ctx)
                elif extra_kwargs:
                    result = await method(params, ctx, **extra_kwargs)
                else:
                    result = await method(params, ctx)
        else:
            if arg_projector is not None:
                projected_kwargs = {**arg_projector, "ctx": ctx}
                worker_call = functools.partial(method, **projected_kwargs)
            elif extra_kwargs:
                worker_call = functools.partial(method, params, ctx, **extra_kwargs)
            else:
                worker_call = functools.partial(method, params, ctx)

            worker_async_future = await submit_supervised(
                executor,
                sync_admission,
                worker_call,
            )
            try:
                result = await asyncio.shield(worker_async_future)
            except asyncio.CancelledError:
                if on_complete is not None or on_failure is not None:
                    sync_lifecycle_continues = True
                    _supervise_sync_lifecycle(
                        worker_async_future,
                        ctx=ctx,
                        method_name=method_name,
                        registry=registry,
                        executor=executor,
                        on_complete=on_complete,
                        on_failure=on_failure,
                        pre_handoff_reject=pre_handoff_reject,
                        request_params=params,
                        webhook_target=webhook_target,
                        webhook_auto_emit=webhook_auto_emit,
                    )
                raise
    except AdcpError as exc:
        # Adopter raised structured error — propagate verbatim. The
        # outer middleware projects to the wire envelope. Fire
        # on_failure first so the framework can release any reservation
        # taken before dispatch (v1.5 create_media_buy lifecycle).
        if on_failure is not None:
            await _safe_on_failure_call(on_failure, exc, method_name)
        raise
    except TypeError as exc:
        # Most likely an arg_projector or extra_kwargs signature-drift bug.
        # Bare INTERNAL_ERROR would hide the cause; project to
        # INVALID_REQUEST with a hint pointing at the adopter's
        # method signature so they fix it without a server-log dive.
        # Note: server logs see the full traceback; wire response
        # stays opaque.
        if arg_projector is not None:
            logger.exception(
                "TypeError invoking platform.%s — likely arg_projector "
                "signature drift (kwargs %s vs adopter signature)",
                method_name,
                sorted(arg_projector.keys()),
            )
            wrapped = AdcpError(
                "INVALID_REQUEST",
                message=(
                    f"Platform method {method_name!r} signature mismatch — "
                    "the framework's wire-shape projection sent "
                    f"kwargs {sorted(arg_projector.keys())!r} + ctx, but "
                    "the adopter method rejected them. Check the "
                    "method's Python signature against the per-specialism "
                    "Protocol class (typically a renamed parameter)."
                ),
                recovery="terminal",
            )
            if on_failure is not None:
                await _safe_on_failure_call(on_failure, wrapped, method_name)
            raise wrapped from exc
        if extra_kwargs is not None:
            logger.exception(
                "TypeError invoking platform.%s — likely extra_kwargs "
                "signature drift (injected kwargs %s vs adopter signature)",
                method_name,
                sorted(extra_kwargs.keys()),
            )
            wrapped = AdcpError(
                "INVALID_REQUEST",
                message=(
                    f"Platform method {method_name!r} rejected framework-injected "
                    f"kwargs {sorted(extra_kwargs.keys())!r}. Declare the matching "
                    "parameter(s) in your platform method signature, or remove them "
                    "if you don't need them."
                ),
                recovery="terminal",
            )
            if on_failure is not None:
                await _safe_on_failure_call(on_failure, wrapped, method_name)
            raise wrapped from exc
        # Non-projected TypeError — fall through to generic wrap.
        logger.exception(
            "Unhandled exception in platform.%s — wrapping to INTERNAL_ERROR",
            method_name,
        )
        wrapped = AdcpError(
            "INTERNAL_ERROR",
            message=_internal_error_message(method_name, exc),
            recovery="terminal",
            details=_internal_error_details(exc),
        )
        if on_failure is not None:
            await _safe_on_failure_call(on_failure, wrapped, method_name)
        raise wrapped from exc
    except _ValidationError as exc:
        # A ValidationError that escaped the platform delegate is almost
        # always the buyer sending a field that fails the seller's stricter
        # request schema.  Surface it as INVALID_REQUEST (correctable) so
        # the buyer knows the payload is fixable and gets the field path.
        # Mirrors _coerce_params_to_platform_type for the annotation path.
        logger.warning(
            "pydantic.ValidationError in platform.%s — wrapping to INVALID_REQUEST",
            method_name,
            exc_info=True,
        )
        wrapped = _validation_error_to_invalid_request(method_name, exc)
        if on_failure is not None:
            await _safe_on_failure_call(on_failure, wrapped, method_name)
        raise wrapped from exc
    except Exception as exc:
        # Wrap unexpected exceptions so the wire never sees a stack
        # trace. Adopter logs the original via observability hooks;
        # __cause__ is preserved for server-side debugging.
        #
        # The ``details.caused_by`` shape (Emma AudioStack P2) gives
        # adopters a breadcrumb on the wire — without it, "An internal
        # error occurred" is a dead end and adopters have to grep
        # server logs. We expose only the exception class name (not the
        # message or traceback) so a misconfigured platform that throws
        # on secret material doesn't leak the secret value through
        # the wire response.
        logger.exception(
            "Unhandled exception in platform.%s — wrapping to INTERNAL_ERROR",
            method_name,
        )
        wrapped = AdcpError(
            "INTERNAL_ERROR",
            message=_internal_error_message(method_name, exc),
            recovery="terminal",
            details=_internal_error_details(exc),
        )
        if on_failure is not None:
            await _safe_on_failure_call(on_failure, wrapped, method_name)
        raise wrapped from exc
    except BaseException as exc:
        # ``asyncio.CancelledError`` (and shutdown BaseExceptions) bypass the
        # wire-error wrapping above, but must still release framework state
        # reserved before adapter dispatch. Preserve the exact exception.
        nested_sync_future = (
            routed_sync_execution.worker if routed_sync_execution is not None else None
        )
        if isinstance(nested_sync_future, asyncio.Future) and (
            on_complete is not None or on_failure is not None
        ):
            sync_lifecycle_continues = True
            _supervise_sync_lifecycle(
                nested_sync_future,
                ctx=ctx,
                method_name=method_name,
                registry=registry,
                executor=executor,
                on_complete=on_complete,
                on_failure=on_failure,
                pre_handoff_reject=pre_handoff_reject,
                request_params=params,
                webhook_target=webhook_target,
                webhook_auto_emit=webhook_auto_emit,
            )
        # A cancelled async mutation may already have crossed an external
        # side-effect boundary. Keep its reservation fail-closed for later
        # reconciliation instead of making an immediate retry eligible to
        # double-book. Synchronous work is settled from its real worker
        # outcome above; ordinary BaseException failures still run the hook.
        if (
            on_failure is not None
            and not sync_lifecycle_continues
            and not isinstance(exc, asyncio.CancelledError)
        ):
            await _safe_on_failure_call(on_failure, exc, method_name)
        raise

    return await _project_invocation_result(
        result,
        ctx=ctx,
        method_name=method_name,
        registry=registry,
        executor=executor,
        on_complete=on_complete,
        on_failure=on_failure,
        pre_handoff_reject=pre_handoff_reject,
        request_params=params,
        webhook_target=webhook_target,
        webhook_auto_emit=webhook_auto_emit,
    )


async def _project_invocation_result(
    result: Any,
    *,
    ctx: RequestContext[Any],
    method_name: str,
    registry: TaskRegistry,
    executor: ThreadPoolExecutor,
    on_complete: Callable[[Any], Awaitable[None]] | None,
    on_failure: Callable[[BaseException], Awaitable[None]] | None,
    pre_handoff_reject: Callable[[], None] | None,
    request_params: BaseModel,
    webhook_target: WebhookDeliveryTarget | None,
    webhook_auto_emit: bool,
) -> Any:
    """Project a raw adopter result and settle its framework lifecycle hooks."""
    if is_task_handoff(result):
        # Reject before any side effect (registry row, background task,
        # completion webhook) is created. The wholesale discovery guard
        # uses this so an adopter handing off on a synchronous-only
        # wholesale request never leaks a task the buyer is told is
        # rejected.
        if pre_handoff_reject is not None:
            pre_handoff_reject()
        return await _project_handoff(
            result,
            ctx,
            method_name=method_name,
            registry=registry,
            executor=executor,
            on_complete=on_complete,
            on_failure=on_failure,
            request_params=request_params,
            webhook_target=webhook_target,
            webhook_auto_emit=webhook_auto_emit,
        )
    if is_workflow_handoff(result):
        return await _project_workflow_handoff(
            result,
            ctx,
            method_name=method_name,
            registry=registry,
            executor=executor,
            request_params=request_params,
        )

    # Sync return path. Fire on_complete with the typed result before
    # the credential strip + return. If the hook raises, fire on_failure
    # and propagate — same single-hook-per-call semantic as the handoff
    # path. v1.5 create_media_buy uses on_complete to finalize the
    # consumption reservation when the adapter returned inline.
    if on_complete is not None:
        try:
            await on_complete(result)
        except BaseException as exc:
            if on_failure is not None:
                await _safe_on_failure_call(on_failure, exc, method_name)
            raise

    # Defense-in-depth credential strip on every sync return. The typed
    # projections (:func:`to_wire_account` etc.) handle the case where
    # the adopter returns the framework's typed dataclasses; this
    # boundary catches loose dicts and Pydantic models with
    # ``extra='allow'``. Method-gated to avoid walking large product
    # / signal catalogs that can't carry credentials.
    return strip_credentials_from_wire_result(method_name, result)


async def _settle_cancelled_sync_lifecycle(
    worker_future: asyncio.Future[Any],
    *,
    ctx: RequestContext[Any],
    method_name: str,
    registry: TaskRegistry,
    executor: ThreadPoolExecutor,
    on_complete: Callable[[Any], Awaitable[None]] | None,
    on_failure: Callable[[BaseException], Awaitable[None]] | None,
    pre_handoff_reject: Callable[[], None] | None,
    request_params: BaseModel,
    webhook_target: WebhookDeliveryTarget | None,
    webhook_auto_emit: bool,
) -> None:
    """Settle a sync worker after its request task has been cancelled."""
    try:
        result = await asyncio.shield(worker_future)
    except asyncio.CancelledError:
        # Cancelling this supervisor must not cancel or roll back the
        # non-cancellable thread it observes. Its reservation remains held.
        raise
    except Exception as exc:
        if on_failure is not None:
            await _safe_on_failure_call(on_failure, exc, method_name)
        return
    if is_task_handoff(result) or is_workflow_handoff(result):
        # The cancelled caller never received a task id. Do not promote an
        # unreachable handoff; returning a handoff has not executed its work.
        if on_failure is not None:
            await _safe_on_failure_call(on_failure, asyncio.CancelledError(), method_name)
        logger.warning(
            "Discarded %s handoff returned after request cancellation; no task id was issued",
            method_name,
        )
        return
    try:
        await _project_invocation_result(
            result,
            ctx=ctx,
            method_name=method_name,
            registry=registry,
            executor=executor,
            on_complete=on_complete,
            on_failure=on_failure,
            pre_handoff_reject=pre_handoff_reject,
            request_params=request_params,
            webhook_target=webhook_target,
            webhook_auto_emit=webhook_auto_emit,
        )
    except Exception:
        # Lifecycle hooks already apply their own rollback semantics. There is
        # no request waiter left to receive this exception, so retain it in
        # server logs rather than producing an unhandled-task warning.
        logger.exception(
            "Cancelled request's synchronous %s lifecycle failed while settling",
            method_name,
        )


def _supervise_sync_lifecycle(
    worker_future: asyncio.Future[Any],
    *,
    ctx: RequestContext[Any],
    method_name: str,
    registry: TaskRegistry,
    executor: ThreadPoolExecutor,
    on_complete: Callable[[Any], Awaitable[None]] | None,
    on_failure: Callable[[BaseException], Awaitable[None]] | None,
    pre_handoff_reject: Callable[[], None] | None,
    request_params: BaseModel,
    webhook_target: WebhookDeliveryTarget | None,
    webhook_auto_emit: bool,
) -> None:
    """Own a cancelled request's worker until its lifecycle settles."""
    lifecycle = asyncio.create_task(
        _settle_cancelled_sync_lifecycle(
            worker_future,
            ctx=ctx,
            method_name=method_name,
            registry=registry,
            executor=executor,
            on_complete=on_complete,
            on_failure=on_failure,
            pre_handoff_reject=pre_handoff_reject,
            request_params=request_params,
            webhook_target=webhook_target,
            webhook_auto_emit=webhook_auto_emit,
        )
    )
    _SUPERVISED_SYNC_LIFECYCLES.add(lifecycle)
    lifecycle.add_done_callback(_SUPERVISED_SYNC_LIFECYCLES.discard)


async def _safe_on_failure_call(
    on_failure: Callable[[BaseException], Awaitable[None]],
    exc: BaseException,
    method_name: str,
) -> None:
    """Fire the framework on_failure hook; log and swallow hook errors.

    Hook errors must NEVER block exception propagation — the buyer
    needs to see the original adapter failure. Used by both the sync
    path in :func:`_invoke_platform_method` and (via the inner
    ``_fail`` closure) the handoff path in :func:`_project_handoff`.
    """
    try:
        await on_failure(exc)
    except Exception:
        logger.exception(
            "on_failure hook raised while handling %s for %s — original exception still propagates",
            type(exc).__name__,
            method_name,
        )


async def _project_handoff(
    handoff: TaskHandoff[Any],
    ctx: RequestContext[Any],
    *,
    method_name: str,
    registry: TaskRegistry,
    executor: ThreadPoolExecutor,
    on_complete: Callable[[Any], Awaitable[None]] | None = None,
    on_failure: Callable[[BaseException], Awaitable[None]] | None = None,
    request_params: BaseModel | None = None,
    webhook_target: WebhookDeliveryTarget | None = None,
    webhook_auto_emit: bool = True,
) -> dict[str, Any]:
    """Promote a TaskHandoff to a background task.

    Lifecycle:

    1. Allocate ``task_id`` via ``registry.issue(account_id=...,
       task_type=method_name)``. The registry persists the row in
       ``submitted`` state.
    2. Kick off the handoff fn in the background via
       :func:`asyncio.create_task` (async fn) or
       :func:`loop.run_in_executor` (sync fn) with an explicit
       ``contextvars.copy_context()`` snapshot. ``create_task``
       inherits the snapshot for free; ``run_in_executor`` doesn't,
       hence the explicit copy.
    3. The background task awaits the handoff fn's return. On success,
       if ``on_complete`` is provided, the framework awaits it with the
       typed result before persisting. Then ``registry.complete(task_id,
       result.model_dump() if Pydantic else result)``. On
       :class:`AdcpError` from the handoff fn OR ``on_complete``, calls
       ``registry.fail(task_id, error.to_wire())``; on any other
       exception, wraps to ``INTERNAL_ERROR`` and calls
       ``registry.fail``.
    4. Returns the wire ``Submitted`` envelope dict to the synchronous
       caller (the platform method's typed shim), which projects it
       to the buyer.

    :param method_name: Wire-spec verb name (``'create_media_buy'``,
        etc.) — used as ``task_type`` on the registry row so
        ``tasks/get`` round-trips correctly.

    :param on_complete: Optional framework hook invoked with the typed
        result of the handoff fn before ``registry.complete``. Used by
        the proposal-finalize lifecycle to commit the proposal to
        :class:`ProposalStore` exactly once when the HITL approval
        lands. If the hook raises, the framework treats it like a
        handoff fn failure: ``on_failure`` runs (if set), then
        ``registry.fail`` is called with the wrapped error, and
        ``registry.complete`` is NOT called. This is what gives the
        v1.5 single-ledger guarantee its teeth — a commit failure
        cannot leave the task in 'submitted' forever or land the
        proposal in a half-committed state.

    :param on_failure: Optional framework hook invoked with the
        terminal exception when the handoff fn raises OR when
        ``on_complete`` raises. Used by the v1.5 create_media_buy HITL
        path to release the consumption reservation
        (``CONSUMING → COMMITTED``) so the buyer can retry without
        ``PROPOSAL_NOT_COMMITTED`` blocking them. Hook errors are
        logged but never block the ``registry.fail`` call — the buyer
        needs the failure visible via ``tasks/get`` regardless of
        hook outcomes.

    :param request_params: The original request Pydantic model that
        triggered the task. Used to echo the request's ``context``
        extension into the registry-stored wire envelope on both
        success (``registry.complete``) and failure
        (``registry.fail``) paths — closes #563. Mirrors the sync
        AdcpError path's context-passthrough (PR #560). When ``None``,
        no echo happens (e.g. test fixtures invoking the handoff
        helper directly). Also the source of the buyer's
        ``push_notification_config`` (url / token / operation_id) for
        the terminal-completion webhook.

    :param webhook_target: The wired :class:`~adcp.webhook_sender.WebhookSender`
        or :class:`~adcp.webhook_supervisor.WebhookDeliverySupervisor`. When
        the buyer supplied ``push_notification_config`` on
        ``request_params``, the background completion path emits the
        terminal completion / failure webhook to that URL EXACTLY ONCE —
        on success after ``registry.complete``, on failure after
        ``registry.fail``. This is the async-path half of the spec
        webhook contract (adcp#5389): a ``Submitted`` task carrying a
        push config MUST deliver at least the terminal notification.
        ``None`` (and the no-push case) skips delivery — the buyer polls
        ``tasks/get`` instead. The framework's polling path is unchanged.

    :param webhook_auto_emit: Async task-webhook delivery gate. The
        production handler defaults this to ``True`` independently of
        the legacy sync-completion compatibility flag. Callers pass
        ``False`` only when they own terminal task delivery.

    The handoff fn is extracted via the type-identity dispatch in
    :func:`adcp.decisioning.types.is_task_handoff`. Subclassed
    TaskHandoff instances (deliberate non-feature) silently take the
    sync-return path before reaching this function.
    """
    if (
        webhook_auto_emit
        and method_name in SPEC_WEBHOOK_TASK_TYPES
        and _extract_push_notification_url_and_token(request_params) is not None
        and webhook_target is None
    ):
        rejection = AdcpError(
            "INVALID_REQUEST",
            message=(
                "push_notification_config requires webhook_sender or "
                "webhook_supervisor before this request can enter the "
                "TaskHandoff lifecycle"
            ),
            recovery="correctable",
            field="push_notification_config",
            suggestion=(
                "Configure webhook delivery, omit push_notification_config "
                "and poll tasks/get, or set auto_emit_task_webhooks=False "
                "only when adopter code owns terminal webhook delivery"
            ),
        )
        if on_failure is not None:
            await _safe_on_failure_call(on_failure, rejection, method_name)
        raise rejection

    fn = handoff._fn

    # Extract the buyer's ``context`` extension from the original
    # request and lock it onto the TaskRecord at issue-time. The
    # registry surfaces it at the top level of ``tasks/get`` reads
    # (sibling of ``result`` / ``error`` per
    # ``schemas/cache/core/tasks_get_response.json``). Capturing once
    # at issue-time means the terminal-state helpers (_fail,
    # registry.complete) never need to know about request-side
    # context — keeps the wire-shape boundary in one place.
    task_id = await registry.issue(
        account_id=ctx.account.id,
        task_type=method_name,
        request_context=_extract_request_context(request_params),
    )

    # Hand off to background. The wire envelope returns immediately;
    # the fn runs to completion in the background and persists the
    # terminal artifact via the registry.
    handoff_ctx = TaskHandoffContext(id=task_id, _registry=registry)

    async def _fail(exc: AdcpError) -> None:
        """Run the framework's on_failure hook (if set) then
        ``registry.fail``. Hook errors are logged but never block the
        registry.fail — the buyer needs the failure visible via
        tasks/get regardless of hook outcomes.

        Note: the request's ``context`` extension lands on the
        ``tasks/get`` response at the top level (sibling of
        ``error``), not inside the ``adcp_error`` envelope — see
        :meth:`TaskRecord.to_dict` and the
        ``schemas/cache/core/tasks_get_response.json``
        ``TasksGetResponse.context`` field. The context is captured
        once at ``registry.issue()`` time below; ``_fail`` doesn't
        touch it.
        """
        if on_failure is not None:
            try:
                await on_failure(exc)
            except Exception:
                logger.exception(
                    "on_failure hook raised for task %s — failure is "
                    "still recorded in the registry",
                    task_id,
                )
        error_wire = exc.to_wire()
        await registry.fail(task_id, error_wire)
        # Terminal failure webhook (spec MUST when push config present).
        # Fired AFTER registry.fail so the buyer's tasks/get poll and the
        # push notification observe the same terminal state. The error
        # wire dict is the payload `result`; the helper is self-isolating
        # (logged-and-swallowed) so a delivery failure never re-raises into
        # the background task.
        await emit_terminal_completion_webhook(
            target=webhook_target,
            enabled=webhook_auto_emit,
            method_name=method_name,
            params=request_params,
            status="failed",
            task_id=task_id,
            result=error_wire,
        )

    async def _run() -> None:
        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(handoff_ctx)
            else:
                ctx_snapshot = contextvars.copy_context()
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    executor,
                    functools.partial(ctx_snapshot.run, fn, handoff_ctx),
                )
        except AdcpError as exc:
            await _fail(exc)
            return
        except Exception as exc:
            logger.exception(
                "Unhandled exception in handoff fn for task %s — wrapping",
                task_id,
            )
            wrapped = AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"Background task for {method_name!r} raised "
                    f"{type(exc).__name__}; see details for cause"
                ),
                recovery="terminal",
                details=_internal_error_details(exc),
            )
            await _fail(wrapped)
            return
        except BaseException:
            # Cancellation does not prove adopter work stopped. Leave any
            # reservation fail-closed for expiry/reconciliation rather than
            # release it while side effects may still be outstanding.
            raise

        # Framework completion hook (e.g., proposal_store.commit for
        # finalize, mark_proposal_consumed for create_media_buy). Runs
        # with the TYPED result before model_dump so the closure can
        # pull typed fields (.expires_at, .proposal, .media_buy_id)
        # off it directly. Failures here are treated identically to a
        # handoff fn failure: on_failure runs, registry.fail is called,
        # registry.complete is NOT called. This is the load-bearing seam
        # for the v1.5 single-ledger D3 guarantee.
        if on_complete is not None:
            try:
                await on_complete(result)
            except AdcpError as exc:
                await _fail(exc)
                return
            except Exception as exc:
                logger.exception(
                    "Unhandled exception in on_complete hook for task %s — wrapping",
                    task_id,
                )
                wrapped = AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Post-completion hook for {method_name!r} raised "
                        f"{type(exc).__name__}; see details for cause"
                    ),
                    recovery="terminal",
                    details=_internal_error_details(exc),
                )
                await _fail(wrapped)
                return

        # Persist terminal artifact. Pydantic responses get
        # ``model_dump()``; dict responses pass through.
        #
        # Credential strip BEFORE persistence: durable registries
        # (Postgres, Redis) write the artifact to disk; even in-memory,
        # ``tasks/get`` returns it verbatim. A bearer credential
        # surviving the typed projection (e.g., Pydantic
        # ``extra='allow'`` model carrying ``governance_agents[i].
        # authentication``) would land in the buyer's ``tasks/get``
        # poll AND the idempotency replay cache. Method-gated so
        # non-account methods skip the recursive walk.
        if hasattr(result, "model_dump"):
            persisted = result.model_dump()
        elif isinstance(result, dict):
            persisted = result
        else:
            # Adopter returned an unexpected type (not Pydantic, not
            # dict). Best effort: stringify into a 'value' wrapper so
            # tasks/get returns something. Real impls always return
            # the typed Pydantic response.
            persisted = {"value": str(result)}
        persisted = strip_credentials_from_wire_result(method_name, persisted)
        # ``request.context`` echo lands at the top level of the
        # ``tasks/get`` response (sibling of ``result``), not inside
        # the typed result body. ``TaskRecord.request_context`` was
        # captured at ``registry.issue()`` time and ``to_dict()``
        # surfaces it under the top-level ``context`` key; nothing to
        # do here on the result path.
        await registry.complete(task_id, persisted)
        # Terminal completion webhook (spec MUST when push config present).
        # Fired AFTER registry.complete so the buyer's tasks/get poll and
        # the push notification observe the same terminal artifact. EXACTLY
        # ONCE — the sync auto-emit gate skips the {task_id, status}
        # submitted projection, so the handoff path owns the completion
        # notification end-to-end. The helper is self-isolating
        # (logged-and-swallowed); a delivery failure never re-raises here.
        await emit_terminal_completion_webhook(
            target=webhook_target,
            enabled=webhook_auto_emit,
            method_name=method_name,
            params=request_params,
            status="completed",
            task_id=task_id,
            result=persisted,
        )

    # ``asyncio.create_task`` only weak-refs the resulting Task — under
    # GC pressure or with no outer awaiter, the task can be collected
    # mid-flight, leaving the registry stuck in 'submitted' forever.
    # Track in a module-level set with a done-callback that discards
    # the entry once the task completes. Documented Python footgun:
    # https://docs.python.org/3/library/asyncio-task.html#creating-tasks
    #
    # Per Python 3.11+ semantics, ``asyncio.create_task`` inherits the
    # current task's ContextVar state by reference (NOT a snapshot).
    # That's the right behavior here — the background task should see
    # the request-scope ContextVars set by middleware, NOT a stale
    # snapshot from before middleware ran. Sync handoffs go through
    # ``run_in_executor`` with explicit ``copy_context`` inside ``_run``.
    bg_task = asyncio.create_task(_run(), name=f"adcp-handoff-{task_id}")
    _BACKGROUND_HANDOFF_TASKS.add(bg_task)
    bg_task.add_done_callback(_BACKGROUND_HANDOFF_TASKS.discard)

    # Wire ``Submitted`` envelope per
    # ``schemas/cache/core/protocol-envelope.json``: only ``task_id`` +
    # ``status`` are framework-emitted at this layer; the per-tool
    # ``payload`` is empty for the submitted state. ``task_type`` is
    # deliberately NOT on the wire — it lives on TaskRecord for
    # ``tasks/get`` reads only, since the Python method name leaking to
    # buyers would couple the wire to handler-internal naming.
    return {
        "task_id": task_id,
        "status": "submitted",
    }


#: Strong-ref the in-flight handoff tasks so the asyncio loop's
#: weak-ref behavior doesn't garbage-collect them mid-flight. Each
#: completed task removes itself via :meth:`asyncio.Task.add_done_callback`.
#: Module-level so the set survives across requests; framework-internal,
#: never exported.
_BACKGROUND_HANDOFF_TASKS: set[asyncio.Task[None]] = set()


async def _project_workflow_handoff(
    handoff: WorkflowHandoff,
    ctx: RequestContext[Any],
    *,
    method_name: str,
    registry: TaskRegistry,
    executor: ThreadPoolExecutor,
    request_params: BaseModel | Any | None = None,
) -> dict[str, Any]:
    """Project a :class:`WorkflowHandoff` to the wire Submitted envelope.

    Distinct from :func:`_project_handoff`: NO background coroutine
    runs. The framework allocates a ``task_id`` via
    :meth:`TaskRegistry.issue` and calls the adopter's enqueue fn
    ONCE — synchronously if it's a sync callable, awaited if it's a
    coroutine. The enqueue fn registers the work into the adopter's
    external system (trafficker UI queue, batch DB, Airflow trigger,
    etc.) and returns; the framework then returns the Submitted
    envelope to the buyer.

    The adopter's external workflow later calls
    ``registry.complete(task_id, result)`` or
    ``registry.fail(task_id, error)`` directly — minutes, hours, or
    days later. The registry is the long-lived control surface; the
    framework's role ends after enqueue.

    **Rollback.** If the enqueue fn raises, the just-allocated
    task_id is discarded from the registry via
    :meth:`TaskRegistry.discard` so the buyer never sees a Submitted
    envelope referencing an orphan id their external workflow never
    registered. The exception is re-raised; the dispatch wrapper
    catches it and projects to ``AdcpError`` per the handler
    contract.

    :param method_name: Wire-spec verb name — used as ``task_type``
        on the registry row so ``tasks/get`` round-trips correctly.
    """
    fn = handoff._fn

    # Same context-echo capture as :func:`_project_handoff`: the
    # request's ``context`` extension lives on the TaskRecord and
    # surfaces at the top level of ``tasks/get`` reads (#563). The
    # WorkflowHandoff path persists the task and immediately returns
    # — the adopter's enqueue fn does not write to the registry — so
    # context capture must happen here at issue-time too.
    task_id = await registry.issue(
        account_id=ctx.account.id,
        task_type=method_name,
        request_context=_extract_request_context(request_params),
    )
    handoff_ctx = TaskHandoffContext(id=task_id, _registry=registry)

    try:
        if asyncio.iscoroutinefunction(fn):
            await fn(handoff_ctx)
        else:
            ctx_snapshot = contextvars.copy_context()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                executor,
                functools.partial(ctx_snapshot.run, fn, handoff_ctx),
            )
    except BaseException:
        # Rollback: the buyer can't be left with a Submitted envelope
        # referencing a task_id the adopter's external workflow never
        # registered. Discard the just-allocated registry row, then
        # re-raise so the outer dispatch wrapper projects the
        # exception to AdcpError. ``BaseException`` (not Exception)
        # so KeyboardInterrupt / SystemExit also clean up the
        # registry side; framework state should never strand on
        # interpreter teardown.
        await registry.discard(task_id)
        raise

    # Wire ``Submitted`` envelope — same shape as the TaskHandoff
    # path. Buyers can't tell which path the seller took; that's
    # intentional. ``task_type`` lives on the registry row (for
    # ``tasks/get``), not on the wire envelope, per the same posture
    # as :func:`_project_handoff`.
    return {
        "task_id": task_id,
        "status": "submitted",
    }


__all__ = [
    "RECOMMENDED_METHODS_PER_SPECIALISM",
    "REQUIRED_METHODS_PER_SPECIALISM",
    "SPEC_SPECIALISM_ENUM",
    "compose_caller_identity",
    "validate_platform",
]
