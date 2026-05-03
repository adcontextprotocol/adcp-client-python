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
import logging
import os
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

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
from adcp.decisioning.types import (
    AdcpError,
    TaskHandoff,
    WorkflowHandoff,
    is_task_handoff,
    is_workflow_handoff,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from adcp.decisioning.accounts import AccountStore
    from adcp.decisioning.context import AuthInfo, RequestContext
    from adcp.decisioning.registry import BuyerAgent
    from adcp.decisioning.types import Account
    from adcp.server.base import ToolContext

logger = logging.getLogger(__name__)

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
    # list_creative_formats, list_creatives) are present-or-absent —
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
    # Signals specialisms — third-party data brokers and first-party
    # data providers share the same SignalsPlatform Protocol surface.
    "signal-marketplace": frozenset(
        {
            "get_signals",
            "activate_signal",
        }
    ),
    "signal-owned": frozenset(
        {
            "get_signals",
            "activate_signal",
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
    # Creative builder specialisms — template-driven transform AND
    # brief-driven generation share the unified
    # ``CreativeBuilderPlatform`` Protocol per JS commit ``841616d7``
    # (F13). ``build_creative`` is the only wire-required method;
    # ``preview_creative``, ``refine_creative``, ``sync_creatives`` are
    # optional and surface ``UNSUPPORTED_FEATURE`` to buyers when
    # missing.
    "creative-template": frozenset(
        {
            "build_creative",
        }
    ),
    "creative-generative": frozenset(
        {
            "build_creative",
        }
    ),
    # Creative-ad-server — stateful library, per-creative pricing, tag
    # generation, per-creative delivery. ``preview_creative`` is
    # required here (distinct from CreativeBuilderPlatform where it's
    # optional) — buyers expect preview surface from any stateful
    # library.
    "creative-ad-server": frozenset(
        {
            "build_creative",
            "preview_creative",
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
        "list_creative_formats",
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
    return os.environ.get(_STRICT_VALIDATE_ENV, "") == "1"


# ---------------------------------------------------------------------------
# INTERNAL_ERROR breadcrumbs (Emma AudioStack P2)
# ---------------------------------------------------------------------------

#: Cap on the message+repr we expose to the wire. Long stack traces or
#: secret-shaped repr (e.g., a ``Credential`` repr that includes the
#: token) get truncated. Stack trace lives in server logs only.
_INTERNAL_ERROR_DETAIL_CHARS = 200


def _internal_error_message(method_name: str, exc: BaseException) -> str:
    """Build the wire-side ``message`` for an INTERNAL_ERROR wrap.

    Adopters debugging "An internal error occurred" with no breadcrumb
    have to grep server logs to even see which exception fired (Emma
    AudioStack P2). Surfacing the exception class name in the wire
    message gives them a starting point without leaking the traceback.
    """
    cls_name = type(exc).__name__
    return f"Platform method {method_name!r} raised {cls_name}; see details for cause"


def _internal_error_details(exc: BaseException) -> dict[str, Any]:
    """Build the wire-side ``details`` payload for an INTERNAL_ERROR
    wrap.

    ``details.caused_by`` carries the exception class name + truncated
    str — no traceback, no module path, no chained ``__cause__``.
    The class name lets adopters distinguish ``AttributeError``
    (typo-shaped) from ``KeyError`` (missing-config-shaped) from
    ``ConnectionError`` (network-shaped) at a glance.

    **``caused_by.type`` is a debug breadcrumb, not a wire contract.**
    The value is Python's exception class name verbatim
    (``"AttributeError"``, ``"KeyError"``, ``"ValidationError"``).
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

    Truncation is defense-in-depth against an adopter who throws on
    secret material and ends up with a repr that includes the secret
    value verbatim. The full traceback is in the server log via
    ``logger.exception``; only the wire response is sanitized.

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
    raw = str(exc)
    if len(raw) > _INTERNAL_ERROR_DETAIL_CHARS:
        raw = raw[: _INTERNAL_ERROR_DETAIL_CHARS - 3] + "..."
    details: dict[str, Any] = {
        "caused_by": {
            "type": type(exc).__name__,
            "message": raw,
        }
    }
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
            # The caused_by.message already carries the truncated
            # repr; adopters can still triage via server logs.
            pass
    return details


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
       specialisms missing methods raise ``AdcpError("INVALID_REQUEST")``.
    4. Each claimed specialism's *recommended* methods (the v6.0 rc.1
       staging set in :data:`RECOMMENDED_METHODS_PER_SPECIALISM` —
       sales-* surface broadening per DX-423) are implemented on the
       platform subclass. Misses emit one ``UserWarning`` per
       method (deduped across overlapping specialisms). Setting
       ``ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM=1`` flips the soft
       warning into a hard ``AdcpError("INVALID_REQUEST")``.
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
    missing: list[tuple[str, str]] = []
    unknown: list[str] = []
    governance_specialisms_claimed: list[str] = []
    for specialism in platform.capabilities.specialisms:
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
    for specialism in platform.capabilities.specialisms:
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
    :param auth_info: Optional verified principal info — when present,
        ``auth_principal`` is populated from ``auth_info.principal``.
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
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.resolve import _NotYetWiredResolver

    auth_principal = auth_info.principal if auth_info is not None else None

    # Composite cache scope key when store is supplied (production
    # path). Falls back to tool_ctx.caller_identity for test fixtures.
    caller_identity: str | None
    if store is not None:
        caller_identity = compose_caller_identity(account, store)
    else:
        caller_identity = tool_ctx.caller_identity

    # Build the RequestContext with the explicit state/resolve kwargs
    # if provided; otherwise let the dataclass default factories
    # supply the v6.0 stubs.
    ctx_kwargs: dict[str, Any] = {
        "request_id": tool_ctx.request_id,
        "caller_identity": caller_identity,
        "tenant_id": tool_ctx.tenant_id,
        "metadata": dict(tool_ctx.metadata),
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


async def _invoke_platform_method(
    platform: DecisioningPlatform,
    method_name: str,
    params: BaseModel,
    ctx: RequestContext[Any],
    *,
    executor: ThreadPoolExecutor,
    registry: TaskRegistry,
    arg_projector: dict[str, Any] | None = None,
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
        ctx)``). Codegen-emitted shims pass this for those tools;
        most tools call with ``None``.
    """
    method = getattr(platform, method_name)

    try:
        if asyncio.iscoroutinefunction(method):
            if arg_projector is not None:
                result = await method(**arg_projector, ctx=ctx)
            else:
                result = await method(params, ctx)
        else:
            ctx_snapshot = contextvars.copy_context()
            loop = asyncio.get_running_loop()
            if arg_projector is not None:
                projected_kwargs = {**arg_projector, "ctx": ctx}
                result = await loop.run_in_executor(
                    executor,
                    functools.partial(ctx_snapshot.run, method, **projected_kwargs),
                )
            else:
                result = await loop.run_in_executor(
                    executor,
                    functools.partial(ctx_snapshot.run, method, params, ctx),
                )
    except AdcpError:
        # Adopter raised structured error — propagate verbatim. The
        # outer middleware projects to the wire envelope.
        raise
    except TypeError as exc:
        # Most likely an arg_projector signature-drift bug — adopter
        # renamed update_media_buy's `patch` kwarg → `update`, etc.
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
            raise AdcpError(
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
            ) from exc
        # Non-projected TypeError — fall through to generic wrap.
        logger.exception(
            "Unhandled exception in platform.%s — wrapping to INTERNAL_ERROR",
            method_name,
        )
        raise AdcpError(
            "INTERNAL_ERROR",
            message=_internal_error_message(method_name, exc),
            recovery="terminal",
            details=_internal_error_details(exc),
        ) from exc
    except Exception as exc:
        # Wrap unexpected exceptions so the wire never sees a stack
        # trace. Adopter logs the original via observability hooks;
        # __cause__ is preserved for server-side debugging.
        #
        # The ``details.caused_by`` shape (Emma AudioStack P2) gives
        # adopters a breadcrumb on the wire — without it, "An internal
        # error occurred" is a dead end and adopters have to grep
        # server logs. We expose only the exception class name + str
        # (not the traceback) so a misconfigured platform that throws
        # on secret material doesn't leak the secret value through
        # the wire response.
        logger.exception(
            "Unhandled exception in platform.%s — wrapping to INTERNAL_ERROR",
            method_name,
        )
        raise AdcpError(
            "INTERNAL_ERROR",
            message=_internal_error_message(method_name, exc),
            recovery="terminal",
            details=_internal_error_details(exc),
        ) from exc

    if is_task_handoff(result):
        return await _project_handoff(
            result,
            ctx,
            method_name=method_name,
            registry=registry,
            executor=executor,
        )
    if is_workflow_handoff(result):
        return await _project_workflow_handoff(
            result,
            ctx,
            method_name=method_name,
            registry=registry,
            executor=executor,
        )
    return result


async def _project_handoff(
    handoff: TaskHandoff[Any],
    ctx: RequestContext[Any],
    *,
    method_name: str,
    registry: TaskRegistry,
    executor: ThreadPoolExecutor,
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
    3. The background task awaits the handoff fn's return; on success
       calls ``registry.complete(task_id, result.model_dump() if
       Pydantic else result)``; on :class:`AdcpError` calls
       ``registry.fail(task_id, error.to_wire())``; on any other
       exception, wraps to ``INTERNAL_ERROR`` and calls
       ``registry.fail``.
    4. Returns the wire ``Submitted`` envelope dict to the synchronous
       caller (the platform method's typed shim), which projects it
       to the buyer.

    :param method_name: Wire-spec verb name (``'create_media_buy'``,
        etc.) — used as ``task_type`` on the registry row so
        ``tasks/get`` round-trips correctly.

    The handoff fn is extracted via the type-identity dispatch in
    :func:`adcp.decisioning.types.is_task_handoff`. Subclassed
    TaskHandoff instances (deliberate non-feature) silently take the
    sync-return path before reaching this function.
    """
    fn = handoff._fn

    task_id = await registry.issue(
        account_id=ctx.account.id,
        task_type=method_name,
    )

    # Hand off to background. The wire envelope returns immediately;
    # the fn runs to completion in the background and persists the
    # terminal artifact via the registry.
    handoff_ctx = TaskHandoffContext(id=task_id, _registry=registry)

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
            await registry.fail(task_id, exc.to_wire())
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
            await registry.fail(task_id, wrapped.to_wire())
            return

        # Persist terminal artifact. Pydantic responses get
        # ``model_dump()``; dict responses pass through.
        if hasattr(result, "model_dump"):
            await registry.complete(task_id, result.model_dump())
        elif isinstance(result, dict):
            await registry.complete(task_id, result)
        else:
            # Adopter returned an unexpected type (not Pydantic, not
            # dict). Best effort: stringify into a 'value' wrapper so
            # tasks/get returns something. Real impls always return
            # the typed Pydantic response.
            await registry.complete(task_id, {"value": str(result)})

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

    task_id = await registry.issue(
        account_id=ctx.account.id,
        task_type=method_name,
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
