"""``PlatformHandler`` — wire-shape shims that route to a DecisioningPlatform.

This module is the codegen target — ``scripts/generate_decisioning_handler.py``
will (in a follow-up PR) emit this file by walking the per-specialism
Protocols. For v6.0 alpha foundation, the file is hand-written; the
codegen drift test ships in Stage 4.

Each shim:

1. Accepts the typed Pydantic request + framework :class:`ToolContext`.
2. Resolves the account via ``platform.accounts.resolve``.
3. Builds the typed :class:`RequestContext` via
   :func:`_build_request_context` (D2 + D9 + D15).
4. Calls :func:`_invoke_platform_method` to invoke the platform method,
   which projects ``TaskHandoff`` and wraps non-``AdcpError`` exceptions
   to the wire envelope.
5. Returns whatever the platform method returned — typed Pydantic
   response, plain dict matching the wire shape, or the ``Submitted``
   envelope dict from a TaskHandoff projection. The ``cast()`` on each
   shim is a static-typing hint for callers; it is NOT a runtime
   validation pass. The framework's transport layer
   (``adcp.server.serve``) handles wire serialization for both Pydantic
   and dict returns. Adopters relying on Pydantic round-trip validation
   can opt in via ``response_validator`` middleware.

The class-level ``advertised_tools: ClassVar[set[str]]`` declaration is
auto-registered with the framework's tool-discovery seam via
:meth:`adcp.server.base.ADCPHandler.__init_subclass__` (PR #318). Adopters
get a focused ``tools/list`` filter without manual registration.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import warnings
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from adcp.decisioning._get_products_helpers import _project_product_fields
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.dispatch import (
    _build_request_context,
    _invoke_platform_method,
)
from adcp.decisioning.implementation_config import ProductConfigStore
from adcp.decisioning.pagination import _query_hash, apply_framework_pagination
from adcp.decisioning.property_list import (
    maybe_apply_property_list_filter,
    property_list_capability_enabled,
)
from adcp.decisioning.proposal_dispatch import (
    mark_proposal_consumed,
    maybe_hydrate_recipes_for_create_media_buy,
    maybe_hydrate_recipes_for_media_buy_id,
    maybe_intercept_finalize,
    maybe_persist_draft_after_get_products,
    release_proposal_reservation,
)
from adcp.decisioning.refine import (
    RefineResult,
    assert_buying_mode_consistent,
    has_refine_support,
    project_refine_response,
)
from adcp.decisioning.time_budget import project_incomplete_response, resolve_time_budget
from adcp.decisioning.webhook_emit import maybe_emit_sync_completion
from adcp.server.base import ADCPHandler, ToolContext

logger = logging.getLogger(__name__)

# Pydantic Request/Response types are imported at module scope (NOT
# under TYPE_CHECKING) so that ``typing.get_type_hints(method)`` can
# resolve every shim's ``params`` annotation at runtime. The dispatcher
# at ``adcp.server.mcp_tools._resolve_params_pydantic_model`` walks
# these hints to deserialise wire-shape dicts into the typed Pydantic
# models the shims expect; without runtime visibility, ``get_type_hints``
# raises ``NameError`` on the forward refs (the file uses
# ``from __future__ import annotations``), the resolver swallows the
# exception, and the dispatcher falls back to the dict path — which
# crashes inside the shim with ``'dict' object has no attribute
# 'account'`` (Emma sales-direct backend test, verdict 2/10).
from adcp.types import (
    AccountReference,
    AcquireRightsRequest,
    AcquireRightsResponse,
    ActivateSignalRequest,
    ActivateSignalSuccessResponse,
    BuildCreativeRequest,
    BuildCreativeResponse,
    CalibrateContentRequest,
    CalibrateContentResponse,
    CheckGovernanceRequest,
    CheckGovernanceResponse,
    CreateCollectionListRequest,
    CreateCollectionListResponse,
    CreateContentStandardsRequest,
    CreateContentStandardsResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    CreatePropertyListRequest,
    CreatePropertyListResponse,
    DeleteCollectionListRequest,
    DeleteCollectionListResponse,
    DeletePropertyListRequest,
    DeletePropertyListResponse,
    GetBrandIdentityRequest,
    GetBrandIdentitySuccessResponse,
    GetCollectionListRequest,
    GetCollectionListResponse,
    GetContentStandardsRequest,
    GetContentStandardsResponse,
    GetCreativeDeliveryRequest,
    GetCreativeDeliveryResponse,
    GetCreativeFeaturesRequest,
    GetCreativeFeaturesResponse,
    GetMediaBuyArtifactsRequest,
    GetMediaBuyArtifactsResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetMediaBuysRequest,
    GetMediaBuysResponse,
    GetPlanAuditLogsRequest,
    GetPlanAuditLogsResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetPropertyListRequest,
    GetPropertyListResponse,
    GetRightsRequest,
    GetRightsSuccessResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    ListCollectionListsRequest,
    ListCollectionListsResponse,
    ListContentStandardsRequest,
    ListContentStandardsResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    ListPropertyListsRequest,
    ListPropertyListsResponse,
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    ReportPlanOutcomeRequest,
    ReportPlanOutcomeResponse,
    SyncAudiencesRequest,
    SyncAudiencesSuccessResponse,
    SyncCreativesRequest,
    SyncCreativesSuccessResponse,
    SyncPlansRequest,
    SyncPlansResponse,
    UpdateCollectionListRequest,
    UpdateCollectionListResponse,
    UpdateContentStandardsRequest,
    UpdateContentStandardsResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuySuccessResponse,
    UpdatePropertyListRequest,
    UpdatePropertyListResponse,
    UpdateRightsRequest,
    UpdateRightsResponse,
    ValidateContentDeliveryRequest,
    ValidateContentDeliveryResponse,
)

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning.platform import DecisioningPlatform
    from adcp.decisioning.property_list import PropertyListFetcher
    from adcp.decisioning.registry import BuyerAgent, BuyerAgentRegistry
    from adcp.decisioning.resolve import ResourceResolver
    from adcp.decisioning.state import StateReader
    from adcp.decisioning.task_registry import TaskRegistry
    from adcp.decisioning.types import Account
    from adcp.webhook_sender import WebhookSender
    from adcp.webhook_supervisor import WebhookDeliverySupervisor


# ---------------------------------------------------------------------------
# Class-level advertised tool surface
# ---------------------------------------------------------------------------

#: All wire tools the PlatformHandler shim covers. Each Protocol family
#: contributes its required + optional methods. The framework's
#: ``tools/list`` filters to this set; adopters get only the tools their
#: claimed specialism Protocols cover, plus the framework's
#: ``_is_method_overridden`` filter drops shims whose platform method
#: isn't implemented (sales-only adopters don't accidentally advertise
#: ``build_creative``).
_SALES_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats",
        "list_creatives",
    }
)
_CREATIVE_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "build_creative",
        "preview_creative",
        "get_creative_delivery",
    }
)
_SIGNALS_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "get_signals",
        "activate_signal",
    }
)
_AUDIENCE_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "sync_audiences",
    }
)
_GOVERNANCE_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "check_governance",
        "sync_plans",
        "report_plan_outcome",
        "get_plan_audit_logs",
    }
)
_BRAND_RIGHTS_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "get_brand_identity",
        "get_rights",
        "acquire_rights",
        "update_rights",
    }
)
_CONTENT_STANDARDS_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "list_content_standards",
        "get_content_standards",
        "create_content_standards",
        "update_content_standards",
        "calibrate_content",
        "validate_content_delivery",
        "get_media_buy_artifacts",
        "get_creative_features",
    }
)
_PROPERTY_LISTS_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "create_property_list",
        "update_property_list",
        "get_property_list",
        "list_property_lists",
        "delete_property_list",
    }
)
_COLLECTION_LISTS_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "create_collection_list",
        "update_collection_list",
        "get_collection_list",
        "list_collection_lists",
        "delete_collection_list",
    }
)

#: Methods adopters MAY leave unimplemented per their Protocol. The shim
#: surfaces ``AdcpError(code='UNSUPPORTED_FEATURE')`` to buyers calling
#: an unimplemented optional method instead of leaking AttributeError.
#: Required methods (per ``REQUIRED_METHODS_PER_SPECIALISM``) are
#: enforced at server boot by ``validate_platform`` — the optional set
#: complements that gate at runtime.
_OPTIONAL_PLATFORM_METHODS: frozenset[str] = frozenset(
    {
        # Sales-* optional (gated by claim, not method presence)
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats",
        "list_creatives",
        # CreativeBuilderPlatform optional
        "preview_creative",
        # ContentStandardsPlatform optional analyzer reads
        "get_media_buy_artifacts",
        "get_creative_features",
        # AudiencePlatform adopter-internal helper (not wire-served, but
        # listed here for symmetry should a future shim wire it)
        "poll_audience_statuses",
    }
)


#: Map each spec specialism slug to the tools that specialism's Protocol
#: serves on the wire. Used by :meth:`PlatformHandler.advertised_tools_for_instance`
#: to filter ``tools/list`` to ONLY the tools the platform's claimed
#: specialisms are responsible for — without this filter, a sales-only
#: adopter would see all 40+ shims advertised (Emma cross-cutting P1
#: confirmed by 3 of 3 backend tests).
#:
#: Keys MUST be drawn from
#: :data:`adcp.decisioning.dispatch.SPEC_SPECIALISM_ENUM`. Slugs not in
#: this map (``signed-requests``, ``governance-aware-seller``) are
#: meta-claims that don't expose tools directly; they compose with
#: another specialism that does.
SPECIALISM_TO_ADVERTISED_TOOLS: dict[str, frozenset[str]] = {
    # Sales-* archetypes — all use the unified SalesPlatform surface.
    "sales-non-guaranteed": _SALES_ADVERTISED_TOOLS,
    "sales-guaranteed": _SALES_ADVERTISED_TOOLS,
    "sales-broadcast-tv": _SALES_ADVERTISED_TOOLS,
    "sales-social": _SALES_ADVERTISED_TOOLS,
    "sales-catalog-driven": _SALES_ADVERTISED_TOOLS,
    "sales-proposal-mode": _SALES_ADVERTISED_TOOLS,
    # Creative — Builder + AdServer. Builder claims expose
    # build_creative + optional preview_creative; AdServer adds
    # get_creative_delivery (per CreativeAdServerPlatform Protocol).
    # Both share the same advertised set; the per-method override
    # filter (``_is_method_overridden``) drops unimplemented optionals.
    "creative-generative": _CREATIVE_ADVERTISED_TOOLS,
    "creative-template": _CREATIVE_ADVERTISED_TOOLS,
    "creative-ad-server": _CREATIVE_ADVERTISED_TOOLS,
    # Signals — marketplace + owned share the same wire surface.
    "signal-marketplace": _SIGNALS_ADVERTISED_TOOLS,
    "signal-owned": _SIGNALS_ADVERTISED_TOOLS,
    # Audience.
    "audience-sync": _AUDIENCE_ADVERTISED_TOOLS,
    # Governance — spend-authority + delivery-monitor share the
    # CampaignGovernancePlatform Protocol surface.
    "governance-spend-authority": _GOVERNANCE_ADVERTISED_TOOLS,
    "governance-delivery-monitor": _GOVERNANCE_ADVERTISED_TOOLS,
    # Brand rights, content standards, lists — one slug per Protocol.
    "brand-rights": _BRAND_RIGHTS_ADVERTISED_TOOLS,
    "content-standards": _CONTENT_STANDARDS_ADVERTISED_TOOLS,
    "property-lists": _PROPERTY_LISTS_ADVERTISED_TOOLS,
    "collection-lists": _COLLECTION_LISTS_ADVERTISED_TOOLS,
}


#: Map each spec specialism slug to the wire-protocol values it
#: contributes to ``supported_protocols`` on the
#: ``get_adcp_capabilities`` response. Source of truth is the
#: ``supported_protocols`` enum in
#: ``schemas/cache/protocol/get-adcp-capabilities-response.json``
#: (``media_buy | signals | governance | sponsored_intelligence |
#: creative | brand``). Composes with
#: :data:`SPECIALISM_TO_ADVERTISED_TOOLS` — a specialism whose tools
#: cross protocol boundaries (e.g. ``audience-sync`` exposes
#: ``sync_audiences``, a media_buy tool) declares the relevant set
#: explicitly here.
#:
#: Specialisms that are pure meta-claims (``governance-aware-seller``,
#: ``signed-requests``) contribute no protocol — they compose with a
#: non-meta specialism that does.
SPECIALISM_TO_PROTOCOLS: dict[str, frozenset[str]] = {
    # Sales-* archetypes — all live under the media_buy protocol.
    "sales-non-guaranteed": frozenset({"media_buy"}),
    "sales-guaranteed": frozenset({"media_buy"}),
    "sales-broadcast-tv": frozenset({"media_buy"}),
    "sales-social": frozenset({"media_buy"}),
    "sales-catalog-driven": frozenset({"media_buy"}),
    "sales-proposal-mode": frozenset({"media_buy"}),
    # Creative — generative / template / ad-server all expose creative
    # tools; the ad-server variant additionally exposes
    # ``get_creative_delivery`` which is a media_buy companion read,
    # but the wire protocol is still ``creative``.
    "creative-generative": frozenset({"creative"}),
    "creative-template": frozenset({"creative"}),
    "creative-ad-server": frozenset({"creative"}),
    # Signals.
    "signal-marketplace": frozenset({"signals"}),
    "signal-owned": frozenset({"signals"}),
    # Audience-sync's ``sync_audiences`` is a media_buy tool.
    "audience-sync": frozenset({"media_buy"}),
    # Governance.
    "governance-spend-authority": frozenset({"governance"}),
    "governance-delivery-monitor": frozenset({"governance"}),
    # Brand-rights → brand protocol.
    "brand-rights": frozenset({"brand"}),
    # Content-standards / lists are governance-protocol tools per
    # ``HANDLER_TO_DOMAIN`` in ``adcp.server.builder``.
    "content-standards": frozenset({"governance"}),
    "property-lists": frozenset({"governance"}),
    "collection-lists": frozenset({"governance"}),
}


async def _resolve_buyer_agent(
    registry: BuyerAgentRegistry,
    auth_info: AuthInfo | None,
) -> BuyerAgent:
    """Resolve a :class:`BuyerAgent` from a wired registry.

    The framework's commercial-identity gate. Runs before
    :meth:`AccountStore.resolve` so a suspended / blocked / unknown
    agent is rejected with the correct structured error code instead
    of the rejection leaking into the account-resolution path as a
    confused ``ACCOUNT_NOT_FOUND``.

    Dispatches by credential kind:

    * :class:`HttpSigCredential` →
      :meth:`BuyerAgentRegistry.resolve_by_agent_url` with the
      cryptographically-verified ``agent_url``.
    * :class:`ApiKeyCredential` / :class:`OAuthCredential` →
      :meth:`BuyerAgentRegistry.resolve_by_credential`.
    * No credential at all (unauthenticated dev fixture, ``derived``
      auth) → ``PERMISSION_DENIED`` (no ``details.scope``). Adopters
      running a registry have implicitly opted out of unauthenticated
      traffic.

    All four denial paths surface as ``code="PERMISSION_DENIED"`` to
    match the spec enum and prevent the cross-tenant onboarding-oracle
    risk: an attacker watching the wire MUST NOT be able to
    distinguish "this agent_url is unrecognized at this seller" from
    "this agent_url is recognized but currently denied". The
    discriminator is in ``details``:

    * recognized + suspended →
      ``details = {scope: "agent", status: "suspended", agent_url: ...}``
    * recognized + blocked →
      ``details = {scope: "agent", status: "blocked", agent_url: ...}``
    * unrecognized (registry miss / no credential / unknown status) →
      ``details`` OMITTED — scope MUST NOT be set on the unestablished-
      identity path (omit-on-unestablished-identity rule).

    Note on parity: the *latency / headers / side-effects* parity
    contract between the recognized and unrecognized paths is tracked
    as a follow-up — the eager-raise pattern below still completes the
    unrecognized path on a different code path than the recognized
    one. Renaming closes the wire-code mismatch; folding all four
    paths through a common emit point with deliberate latency padding
    and identical audit/metric side-effects is the next step.

    :raises AdcpError: ``PERMISSION_DENIED`` (all four denial paths).
        Recovery is ``correctable`` per the spec's ``enumMetadata``
        for ``PERMISSION_DENIED``. The wire-level recovery hint is
        independent of the resolution channel: the buyer cannot
        auto-retry a commercial-identity rejection, but the
        ``details.scope == "agent"`` discriminator (when present) is
        the signal callers surface to a human operator rather than
        loop on the request.
    """
    from adcp.decisioning.registry import (
        ApiKeyCredential,
        HttpSigCredential,
        OAuthCredential,
    )
    from adcp.decisioning.types import AdcpError

    credential = auth_info.credential if auth_info is not None else None
    agent: BuyerAgent | None = None
    if credential is not None:
        if isinstance(credential, HttpSigCredential):
            agent = await registry.resolve_by_agent_url(credential.agent_url)
        elif isinstance(credential, (ApiKeyCredential, OAuthCredential)):
            agent = await registry.resolve_by_credential(credential)
        else:
            # Defensive: a future Credential variant lands and the
            # dispatch path doesn't know how to route it. Fail closed
            # with INTERNAL_ERROR rather than silently passing the
            # request through (which would skip the registry gate
            # entirely and leak the upgrade footgun into production).
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"BuyerAgentRegistry dispatch received an unknown "
                    f"Credential variant {type(credential).__name__!r}. "
                    "The framework's resolver doesn't know which registry "
                    "method to call. Update _resolve_buyer_agent in "
                    "adcp.decisioning.handler to dispatch the new variant."
                ),
                recovery="terminal",
            )

    # Generic message used on every denial path — MUST be identical
    # across the unrecognized and the recognized-but-denied paths so
    # the wire-level error.message is not itself a side channel
    # leaking which agent_urls are onboarded with which sellers. The
    # discriminator (when present at all) is in details, only on the
    # recognized-but-denied paths.
    _denied_message = (
        "Buyer agent is not authorized for this seller. The seller's "
        "commercial allowlist did not authorize this credential. "
        "Resolve out-of-band via the seller's onboarding contact; this "
        "is not a request-side error the buyer can correct."
    )

    if agent is None:
        # Registry miss / no credential. ``details`` is OMITTED — the
        # spec's omit-on-unestablished-identity rule says the
        # unrecognized-agent path MUST be indistinguishable on the
        # wire from the recognized-but-denied path, and ``scope``
        # would itself be the discriminator.
        raise AdcpError(
            "PERMISSION_DENIED",
            message=_denied_message,
            recovery="correctable",
        )

    if agent.status == "active":
        return agent
    if agent.status == "suspended":
        raise AdcpError(
            "PERMISSION_DENIED",
            message=_denied_message,
            recovery="correctable",
            details={
                "scope": "agent",
                "status": "suspended",
                "agent_url": agent.agent_url,
            },
        )
    if agent.status == "blocked":
        raise AdcpError(
            "PERMISSION_DENIED",
            message=_denied_message,
            recovery="correctable",
            details={
                "scope": "agent",
                "status": "blocked",
                "agent_url": agent.agent_url,
            },
        )
    # Default-reject any non-active status the framework doesn't
    # recognize (typo, future enum value, adopter-custom string). A
    # silent fall-through to "active" would leak commercial state
    # past the gate. ``details`` is OMITTED for the same reason as
    # the registry-miss branch — the framework treats unknown statuses
    # as the unrecognized-identity path (the row is in the registry
    # but the framework cannot interpret it, which is operationally
    # equivalent to "not authorized" without a defensible status
    # claim to project on the wire).
    raise AdcpError(
        "PERMISSION_DENIED",
        message=_denied_message,
        recovery="correctable",
    )


def _project_build_creative(result: Any) -> Any:
    """Project the adopter's ``build_creative`` return into the wire
    envelope shape.

    The :class:`CreativeBuilderPlatform.build_creative` Protocol
    declares the return as ``CreativeManifest | Sequence[CreativeManifest]
    | BuildCreativeSuccessResponse`` — three ergonomic arms. The wire
    envelope per ``schemas/cache/media-buy/build-creative-response.json``
    has only two success arms: ``{creative_manifest: ...}`` (single)
    or ``{creative_manifests: [...]}`` (multi). This helper wraps the
    bare-manifest and list cases.

    Mirrors the JS-side ``projectBuildCreativeReturn`` at
    ``src/lib/server/decisioning/runtime/from-platform.ts``. Without
    this, an adopter returning a bare :class:`CreativeManifest` (which
    the Protocol explicitly allows) would ship an unwrapped object that
    fails wire ``oneOf`` validation downstream.
    """
    # Already an envelope (has the wire field present).
    if hasattr(result, "creative_manifest") or hasattr(result, "creative_manifests"):
        return result
    if isinstance(result, dict) and (
        "creative_manifest" in result or "creative_manifests" in result
    ):
        return result
    # Sequence of manifests → multi-success envelope.
    if isinstance(result, list):
        return {
            "creative_manifests": [
                m.model_dump(mode="json") if hasattr(m, "model_dump") else m for m in result
            ]
        }
    # Bare CreativeManifest → single-success envelope.
    if hasattr(result, "model_dump"):
        return {"creative_manifest": result.model_dump(mode="json")}
    # Unknown shape — pass through and let wire validation surface.
    return result


def _project_sync_audiences(result: Any) -> Any:
    """Project the adopter's ``sync_audiences`` return into the wire
    envelope shape.

    The :class:`AudiencePlatform.sync_audiences` Protocol allows
    adopters to return either a list of audience-result rows (the
    JS-side ergonomic) or a fully-shaped
    :class:`SyncAudiencesSuccessResponse`. The wire envelope per
    ``schemas/cache/media-buy/sync-audiences-response.json`` is
    ``{audiences: [rows]}``. This helper wraps the list case.

    Mirrors the JS-side response wrapping at
    ``src/lib/server/decisioning/runtime/from-platform.ts:2242-2249``.
    """
    if isinstance(result, list):
        return {
            "audiences": [
                r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in result
            ]
        }
    return result


def _method_accepts_configs(platform: Any, method_name: str) -> bool:
    """Return True when the platform's ``method_name`` declares a ``configs`` parameter."""
    method = getattr(platform, method_name, None)
    if method is None:
        return False
    try:
        sig = inspect.signature(method)
        return "configs" in sig.parameters
    except (ValueError, TypeError):
        return False


def _extract_media_buy_id(result: Any) -> str | None:
    """Pull ``media_buy_id`` off a ``create_media_buy`` return — handles
    Pydantic models, plain dicts, and the ``Submitted`` envelope shape.

    Returns ``None`` for handoff returns (no media_buy_id yet) or when
    the field is missing — the caller skips ``mark_consumed`` in that
    case and the proposal stays in ``COMMITTED`` state until a
    subsequent successful create_media_buy hits the same ID.
    """
    if result is None:
        return None
    if isinstance(result, dict):
        # Submitted envelope path doesn't carry media_buy_id; the
        # standard success shape does.
        if result.get("status") == "submitted":
            return None
        value = result.get("media_buy_id")
    else:
        value = getattr(result, "media_buy_id", None)
    if value is None:
        return None
    return str(value)


class PlatformHandler(ADCPHandler[ToolContext]):
    """ADCPHandler subclass that routes wire requests to a
    :class:`DecisioningPlatform` via :func:`_invoke_platform_method`.

    Constructed by :func:`adcp.decisioning.serve.create_adcp_server_from_platform`
    — adopters never instantiate directly. The handler holds:

    * ``platform`` — the adopter's :class:`DecisioningPlatform` subclass
      instance. Method dispatches read/call this.
    * ``executor`` — the framework-allocated thread-pool for sync platform
      methods (D5).
    * ``registry`` — the :class:`TaskRegistry` for handoff lifecycle.
    * Optional ``state_reader`` / ``resource_resolver`` — Stage-3+ wiring
      for v6.1 backing-store impls; defaults to the v6.0 stubs.

    Per-method shims follow the same template:

    1. Extract ``account_ref`` from the typed request (when the tool
       carries ``account`` on the wire).
    2. Resolve via ``platform.accounts.resolve(ref, auth_info=...)``.
    3. Build :class:`RequestContext` via :func:`_build_request_context`.
    4. Invoke the platform method via :func:`_invoke_platform_method`.

    Adopters who don't override a given platform method get the framework's
    ``not_supported`` baseline (per ADCPHandler) on those tools — and the
    override-detection filter drops the tool from ``tools/list`` unless
    they pass ``advertise_all=True``.
    """

    #: Class-level union of every tool the shim CAN serve. Used by the
    #: framework's ``__init_subclass__`` registration so the class shows
    #: up in :data:`adcp.server.mcp_tools._HANDLER_TOOLS`. The actual
    #: per-instance advertisement is computed by
    #: :meth:`advertised_tools_for_instance` from the platform's claimed
    #: specialisms — without that intersection, a sales-only adopter
    #: would advertise all 40+ shims (Emma cross-cutting P1).
    advertised_tools: ClassVar[set[str]] = (
        set(_SALES_ADVERTISED_TOOLS)
        | set(_CREATIVE_ADVERTISED_TOOLS)
        | set(_SIGNALS_ADVERTISED_TOOLS)
        | set(_AUDIENCE_ADVERTISED_TOOLS)
        | set(_GOVERNANCE_ADVERTISED_TOOLS)
        | set(_BRAND_RIGHTS_ADVERTISED_TOOLS)
        | set(_CONTENT_STANDARDS_ADVERTISED_TOOLS)
        | set(_PROPERTY_LISTS_ADVERTISED_TOOLS)
        | set(_COLLECTION_LISTS_ADVERTISED_TOOLS)
    )

    _agent_type = "decisioning platform"

    def advertised_tools_for_instance(self) -> frozenset[str]:
        """Tools this handler advertises GIVEN ITS PLATFORM'S CLAIMED
        SPECIALISMS.

        Without this hook, ``get_tools_for_handler`` walks the class's
        MRO + ``_is_method_overridden`` filter — both keyed on
        ``PlatformHandler``, which defines all 40+ shims as concrete
        methods. Result: a sales-only adopter advertises
        ``acquire_rights``, ``build_creative``, every signals/audience
        tool, etc. Buyers see a giant menu of tools that 501 on call;
        Emma sales/creative/signals backend tests all flagged this as
        P1 ("advertising 42 of 42 tools").

        Per-instance advertisement intersects the universe of shim
        coverage with what the platform's claimed specialisms are
        responsible for via :data:`SPECIALISM_TO_ADVERTISED_TOOLS`.
        Specialisms not in that map (``signed-requests``,
        ``governance-aware-seller``) are meta-claims and contribute no
        tools — they compose with a non-meta claim that does.

        :returns: The intersection of ``advertised_tools`` (universe)
            with the per-specialism-allowed set. Empty when no
            recognized specialisms are claimed (e.g., adopter still
            piloting a novel slug not in the spec enum); transport
            layer should fall back to the class-level set in that case
            so the handler isn't accidentally muted.
        """
        claimed = self._platform.capabilities.specialisms
        serving: set[str] = set()
        for entry in claimed:
            # ``specialisms`` is ``list[Specialism | str]`` — spec-known
            # entries are coerced to enum by ``__post_init__``; novel /
            # pre-spec slugs pass through as strings.
            slug = entry.value if hasattr(entry, "value") else entry
            tools = SPECIALISM_TO_ADVERTISED_TOOLS.get(slug)
            if tools is not None:
                serving |= set(tools)
        return frozenset(serving)

    def get_advertised_tools(self, *, advertise_all: bool | None = None) -> frozenset[str]:
        """Names ``tools/list`` will return when this handler is served.

        The class-level :attr:`advertised_tools` set is the *universe*
        of tools the handler base supports across all specialisms (~50
        entries on :class:`PlatformHandler`). What buyers actually see
        on the wire is narrower:

        1. Per-instance specialism filter — :meth:`advertised_tools_for_instance`
           intersects the universe with the platform's claimed
           specialisms (a sales-only adopter drops audience/governance
           tools).
        2. Override-detection filter — tools whose handler method is
           still the SDK's ``not_supported`` default are dropped
           (``advertise_all=False``, the spec-aligned default).

        This method runs the same pipeline :func:`adcp.server.serve`
        runs at boot, so adopters can inspect the effective set without
        standing up a network port. The default ``advertise_all`` value
        is whatever was configured on
        :func:`adcp.decisioning.create_adcp_server_from_platform`
        (``False`` when not set).

        :param advertise_all: Override the configured value for this
            call. ``True`` returns the per-specialism set without the
            override filter; ``False`` applies the full filter.
        :returns: Frozen set of tool names.
        """
        from adcp.server.mcp_tools import get_tools_for_handler

        effective = self._advertise_all if advertise_all is None else advertise_all
        return frozenset(
            tool["name"] for tool in get_tools_for_handler(self, advertise_all=effective)
        )

    def __init__(
        self,
        platform: DecisioningPlatform,
        *,
        executor: ThreadPoolExecutor,
        registry: TaskRegistry,
        state_reader: StateReader | None = None,
        resource_resolver: ResourceResolver | None = None,
        webhook_sender: WebhookSender | None = None,
        webhook_supervisor: WebhookDeliverySupervisor | None = None,
        auto_emit_completion_webhooks: bool = True,
        buyer_agent_registry: BuyerAgentRegistry | None = None,
        config_store: ProductConfigStore | None = None,
        property_list_fetcher: PropertyListFetcher | None = None,
        advertise_all: bool = False,
    ) -> None:
        super().__init__()
        self._platform = platform
        self._executor = executor
        self._registry = registry
        self._state_reader = state_reader
        self._resource_resolver = resource_resolver
        self._webhook_sender = webhook_sender
        self._webhook_supervisor = webhook_supervisor
        self._auto_emit_completion_webhooks = auto_emit_completion_webhooks
        self._buyer_agent_registry = buyer_agent_registry
        self._config_store = config_store
        self._property_list_fetcher = property_list_fetcher
        self._advertise_all = advertise_all

        # Cache whether the platform's create_media_buy accepts 'configs'
        # so we only pay the inspect.signature cost at construction time.
        self._create_media_buy_accepts_configs = _method_accepts_configs(
            platform, "create_media_buy"
        )
        if config_store is None and self._create_media_buy_accepts_configs:
            warnings.warn(
                "create_media_buy declares a 'configs' parameter but no "
                "ProductConfigStore was wired — the framework will inject "
                "configs={} (empty dict) on every call. Wire a store via "
                "config_store= in create_adcp_server_from_platform to enable "
                "automatic implementation_config lookup.",
                UserWarning,
                stacklevel=2,
            )

    # ----- account resolution helper -----

    async def _resolve_account(
        self,
        ref: AccountReference | None,
        ctx: ToolContext,
    ) -> Account[Any]:
        """Resolve a wire :class:`AccountReference` to a typed
        :class:`Account` via the platform's :class:`AccountStore`.

        Pulls auth info from ``ctx.metadata['auth_info']`` when the
        operator's ``context_factory`` populates it; otherwise None.
        Adopter ``AccountStore`` impls handle missing-auth cases per
        their own resolution mode (``'derived'`` tolerates None;
        ``'implicit'`` raises ``AUTH_INVALID``; ``'explicit'`` resolves
        by ref).
        ``AccountStore.resolve`` takes a dict — convert the typed
        Pydantic ``AccountReference`` via ``model_dump()`` so adopter
        store impls see a normalized shape.

        When a :class:`adcp.decisioning.BuyerAgentRegistry` is wired,
        this method ALSO resolves the commercial buyer-agent identity
        BEFORE calling ``AccountStore.resolve`` and stashes the result
        on ``ctx.metadata['adcp.buyer_agent']`` for :meth:`_build_ctx`
        to read into the typed :class:`RequestContext`. Suspended /
        blocked / unrecognized agents are rejected here with
        ``PERMISSION_DENIED`` (recognized-but-denied paths carry
        ``details.scope="agent"`` + ``details.status``; the
        unrecognized-agent path omits ``details`` so the wire shape
        does not enumerate which ``agent_url``s are onboarded with
        this seller) instead of the registry miss leaking into the
        AccountStore as ``ACCOUNT_NOT_FOUND``.
        """
        auth_info = self._extract_auth_info(ctx)
        if self._buyer_agent_registry is not None:
            buyer_agent = await _resolve_buyer_agent(
                self._buyer_agent_registry,
                auth_info,
            )
            ctx.metadata["adcp.buyer_agent"] = buyer_agent
        # Handle both Pydantic AccountReference (typical wire path) and
        # raw dict (test fixtures using model_construct, custom dispatch
        # paths). Adopter stores implementing custom shapes are
        # responsible for whatever they accept.
        ref_dict: dict[str, Any] | None
        if ref is None:
            ref_dict = None
        elif hasattr(ref, "model_dump"):
            ref_dict = ref.model_dump()
        elif isinstance(ref, dict):
            ref_dict = ref
        else:
            ref_dict = cast("dict[str, Any]", ref)
        result = self._platform.accounts.resolve(ref_dict, auth_info=auth_info)
        if asyncio.iscoroutine(result):
            resolved = cast("Account[Any]", await result)
        else:
            resolved = cast("Account[Any]", result)
        # Phase 1 sandbox-authority — track explicit mode values for the
        # comply controller's env-fallback fail-closed guard. Implicit
        # default-live (resolver didn't populate mode) is intentionally
        # NOT recorded so pre-migration adopters keep working with
        # ADCP_SANDBOX=1.
        from adcp.decisioning.observed_modes import record_resolved_account_mode

        record_resolved_account_mode(resolved)
        return resolved

    @staticmethod
    def _extract_auth_info(ctx: ToolContext) -> AuthInfo | None:
        """Pull AuthInfo from ToolContext.metadata when present.

        The framework's existing auth integrations (BearerTokenAuthMiddleware,
        custom context_factory) populate ``ctx.metadata`` with
        principal/scope info. Adopter conventions vary; this helper checks
        for an ``adcp.auth_info`` key — Stage 3 ``serve()`` wiring sets
        this from the canonical principal. Returns None when no auth key
        is present (dev / ``'derived'`` fixtures).
        """
        raw = ctx.metadata.get("adcp.auth_info") if ctx.metadata else None
        if isinstance(raw, AuthInfo):
            return raw
        if isinstance(raw, dict):
            # Translate the legacy dict-shape into typed AuthInfo via
            # the framework-internal classmethod that pre-synthesizes
            # the bearer credential without firing the
            # DeprecationWarning. The warning's actionable target is
            # adopter code constructing AuthInfo directly — pointing
            # it at this framework shim every request would be noise
            # the adopter can't fix by changing their code.
            return AuthInfo._from_legacy_dict(raw)
        return None

    def _maybe_auto_emit_sync_completion(
        self,
        method_name: str,
        params: Any,
        result: Any,
    ) -> None:
        """Fire the F12 sync-completion webhook if applicable.

        Skips TaskHandoff projections — those go through the registry
        completion path which emits its own webhook on terminal state.
        The auto-emit fires on the sync-success arm only, mirroring the
        JS-side ``routeIfHandoff`` logic at
        ``src/lib/server/decisioning/runtime/from-platform.ts``.

        TaskHandoff projection returns the exact 2-key dict ``{"task_id":
        ..., "status": "submitted"}`` from ``_project_handoff``; we
        match the full key set rather than the loose ``status ==
        "submitted"`` predicate so an adopter who legitimately returns a
        sync ``{"status": "submitted", ...}`` (e.g., synchronous queue
        acceptance with extra metadata) still gets the auto-emit.
        """
        if (
            isinstance(result, dict)
            and set(result.keys()) == {"task_id", "status"}
            and result.get("status") == "submitted"
        ):
            # TaskHandoff projection — registry completion path emits
            # its own webhook on terminal state.
            return
        maybe_emit_sync_completion(
            sender=self._webhook_sender,
            supervisor=self._webhook_supervisor,
            enabled=self._auto_emit_completion_webhooks,
            method_name=method_name,
            params=params,
            result=result,
        )

    def _build_ctx(
        self,
        tool_ctx: ToolContext,
        account: Account[Any],
    ) -> Any:
        """Wrap :func:`_build_request_context` with the handler's
        wired StateReader / ResourceResolver overrides AND the
        platform's AccountStore (for D9 round-3 composite cache
        scope-key derivation).

        Reads the resolved :class:`BuyerAgent` from
        ``tool_ctx.metadata['adcp.buyer_agent']`` (stashed by
        :meth:`_resolve_account` when a registry is wired) and passes
        it through to the typed :class:`RequestContext`. Uses
        ``pop`` so the value is consumed once — protects against
        the pathological case where a misconfigured ``context_factory``
        returns the same ``ToolContext`` across requests, which would
        otherwise leak the prior request's resolved buyer-agent into
        the next dispatch.
        """
        auth_info = self._extract_auth_info(tool_ctx)
        buyer_agent = tool_ctx.metadata.pop("adcp.buyer_agent", None) if tool_ctx.metadata else None
        return _build_request_context(
            tool_ctx,
            account,
            auth_info,
            store=self._platform.accounts,
            state_reader=self._state_reader,
            resource_resolver=self._resource_resolver,
            buyer_agent=buyer_agent,
        )

    # ----- Protocol discovery -----

    async def get_adcp_capabilities(
        self,
        params: Any = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Project the platform's :class:`DecisioningCapabilities` into a
        spec-conformant ``get_adcp_capabilities`` response.

        The projection mirrors the wire spec block-by-block. Each
        top-level capability block (``account``, ``media_buy``,
        ``signals``, ``governance``, ``sponsored_intelligence``,
        ``brand``, ``creative``, ``request_signing``, ``webhook_signing``,
        ``identity``, ``compliance_testing``) is emitted via
        ``model_dump(mode="json", exclude_none=True)`` when the
        adopter has declared a value.

        Auto-derives:

        * ``adcp.idempotency`` from
          :attr:`DecisioningCapabilities.adcp` (when set) or defaults
          to ``{"supported": False}`` so the response stays spec-valid.
        * ``supported_protocols`` from
          :attr:`DecisioningCapabilities.supported_protocols` (override)
          or, when None, the union of :data:`SPECIALISM_TO_PROTOCOLS`
          over the platform's claimed specialisms.
        * Wire-level ``specialisms`` field from spec-known entries in
          :attr:`DecisioningCapabilities.specialisms` (novel / typo
          strings are filtered — only spec-defined slugs reach the wire).

        Legacy-field projection (deprecation warnings emitted):

        * ``account.supported_billing`` falls back to
          :attr:`DecisioningCapabilities.supported_billing` when
          ``account`` isn't set. ``DeprecationWarning``.
        * ``media_buy.supported_pricing_models`` falls back to
          :attr:`DecisioningCapabilities.pricing_models` when
          ``media_buy`` isn't set. ``DeprecationWarning``.
        * ``channels`` is no longer projected — the spec's
          ``portfolio.primary_channels`` requires
          ``portfolio.publisher_domains`` alongside, which the flat
          ``channels`` field can't supply. Adopters who set ``channels``
          get a ``DeprecationWarning`` pointing at
          ``media_buy.portfolio``.

        Adopters who need a custom projection (vendor-specific feature
        flags, hand-shaped sub-blocks) override
        ``get_adcp_capabilities`` on a :class:`PlatformHandler`
        subclass.
        """
        del params, context  # Discovery; no auth or input required.
        caps = self._platform.capabilities

        # ----- supported_protocols: explicit override > derive from specialisms -----
        supported_protocols: list[str]
        if caps.supported_protocols is not None:
            supported_protocols = [
                p.value if hasattr(p, "value") else str(p) for p in caps.supported_protocols
            ]
        else:
            protocols: set[str] = set()
            for entry in caps.specialisms:
                slug = entry.value if hasattr(entry, "value") else entry
                protocols.update(SPECIALISM_TO_PROTOCOLS.get(slug, frozenset()))
            # ``supported_protocols`` is the storyboard commitment per
            # spec. When no declared specialism resolves to a protocol
            # (e.g. only meta specialisms like ``governance-aware-seller``
            # claimed, or no specialisms at all), emit an empty list
            # rather than silently default to ``["media_buy"]`` — claiming
            # a protocol the adopter never declared forces them to fail
            # the baseline storyboard for that protocol. The boot-time
            # validator
            # (:func:`adcp.decisioning.validate_capabilities.validate_capabilities_response_shape`)
            # catches the empty list as an explicit configuration error,
            # giving adopters a clean failure pointing at the declaration
            # site rather than a wire-shaped lie. Adopters who claim a
            # protocol without an enumerated specialism set
            # ``supported_protocols`` explicitly.
            supported_protocols = sorted(protocols)

        # ----- adcp block: structured > default -----
        # When the adopter declares a full :class:`Adcp` block, take it
        # in its entirety so ``major_versions`` and any future fields
        # (``supported_versions``, ``build_version``) get carried
        # through to the wire. When unset, the envelope helper's default
        # of ``major_versions=[3]`` plus ``idempotency={"supported": False}``
        # keeps the response spec-valid (though buyers will treat the
        # seller as retry-unsafe).
        from adcp.server.responses import capabilities_response

        response = capabilities_response(
            supported_protocols,
            idempotency={"supported": False},
        )
        if caps.adcp is not None:
            response["adcp"] = caps.adcp.model_dump(mode="json", exclude_none=True)

        # ----- structured capability blocks (model_dump for each) -----
        # Each block emits only when the adopter has declared a value.
        # ``exclude_none=True`` keeps the wire shape minimal (every
        # nested optional collapses cleanly).
        if caps.account is not None:
            response["account"] = caps.account.model_dump(mode="json", exclude_none=True)
        if caps.media_buy is not None:
            response["media_buy"] = caps.media_buy.model_dump(mode="json", exclude_none=True)
        if caps.signals is not None:
            response["signals"] = caps.signals.model_dump(mode="json", exclude_none=True)
        if caps.governance is not None:
            response["governance"] = caps.governance.model_dump(mode="json", exclude_none=True)
        if caps.sponsored_intelligence is not None:
            response["sponsored_intelligence"] = caps.sponsored_intelligence.model_dump(
                mode="json", exclude_none=True
            )
        if caps.brand is not None:
            response["brand"] = caps.brand.model_dump(mode="json", exclude_none=True)
        if caps.creative is not None:
            response["creative"] = caps.creative.model_dump(mode="json", exclude_none=True)
        if caps.request_signing is not None:
            response["request_signing"] = caps.request_signing.model_dump(
                mode="json", exclude_none=True
            )
        if caps.webhook_signing is not None:
            response["webhook_signing"] = caps.webhook_signing.model_dump(
                mode="json", exclude_none=True
            )
        if caps.identity is not None:
            response["identity"] = caps.identity.model_dump(mode="json", exclude_none=True)
        if caps.compliance_testing is not None:
            response["compliance_testing"] = caps.compliance_testing.model_dump(
                mode="json", exclude_none=True
            )

        # ----- wire ``specialisms`` field: spec-known entries only -----
        # Only spec-defined enum members reach the wire — novel / typo
        # strings stay diagnostic-only at the dispatch layer and don't
        # leak into the capabilities response.
        wire_specialisms = [entry.value for entry in caps.specialisms if hasattr(entry, "value")]
        if wire_specialisms:
            response["specialisms"] = wire_specialisms

        # ----- legacy flat-field projection -----
        # Deprecation warnings for legacy fields fire at construction in
        # ``DecisioningCapabilities.__post_init__`` — they point at the
        # adopter's declaration site, not at the dispatcher. Here we only
        # project the legacy values when the structured equivalent isn't
        # set. ``channels`` doesn't project to anything because the spec's
        # ``portfolio.primary_channels`` requires ``publisher_domains``
        # alongside, which the flat field can't supply.
        if caps.supported_billing and caps.account is None:
            response["account"] = {"supported_billing": list(caps.supported_billing)}
        if caps.pricing_models and caps.media_buy is None and "media_buy" in supported_protocols:
            # Spec requires uniqueItems on supported_pricing_models;
            # dedupe via dict.fromkeys to preserve declaration order.
            response["media_buy"] = {
                "supported_pricing_models": list(dict.fromkeys(caps.pricing_models)),
            }

        return response

    # ----- Sales tools -----

    async def get_products(  # type: ignore[override]
        self,
        params: GetProductsRequest,
        context: ToolContext | None = None,
    ) -> GetProductsResponse:
        """Invoke the platform's ``get_products`` method and apply fields projection.

        When the platform is a :class:`PlatformRouter` with per-tenant
        ``proposal_managers`` wired, the router's own ``get_products``
        method handles the proposal-side dispatch (per-tenant
        :class:`ProposalManager` lookup, refine-mode selection,
        fall-through to the tenant's :class:`DecisioningPlatform`).
        The handler delegates uniformly via
        :func:`_invoke_platform_method`; the routing decision lives
        on the router.

        When ``params.fields`` is set the framework drops unrequested product
        fields after the platform method returns, always retaining the eight
        schema-required fields.  When ``params.fields`` is ``None`` the
        response passes through unchanged.
        """
        tool_ctx = context or ToolContext()
        # Pre-adapter: validate buying_mode against the wire spec's
        # mutual-exclusion rules (refine+brief, wholesale+brief, refine
        # without refine[]).
        assert_buying_mode_consistent(params)
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        # Refine flow: when buying_mode='refine' the framework dispatches
        # to refine_get_products() (when present) and projects the result
        # into the wire response — adopters return a RefineResult and
        # framework constructs position-matched refinement_applied[].
        buying_mode_attr = getattr(params, "buying_mode", None)
        mode = (
            (
                buying_mode_attr.value
                if hasattr(buying_mode_attr, "value")
                else str(buying_mode_attr)
            )
            if buying_mode_attr is not None
            else None
        )
        if mode == "refine":
            from adcp.decisioning.types import AdcpError

            # v1.5 finalize interception (D2 + D7). When the request
            # carries a refine[i].action='finalize' AND the resolved
            # tenant has a finalize-capable manager + wired store, the
            # framework intercepts before refine_products and runs the
            # finalize lifecycle: hydrate draft → call finalize_proposal →
            # commit → project wire response. No-op when finalize isn't
            # in scope; falls through to the v1 refine path.
            finalize_response = await maybe_intercept_finalize(
                self._platform,
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            )
            if finalize_response is not None:
                return cast("GetProductsResponse", finalize_response)

            if not has_refine_support(self._platform):
                raise AdcpError(
                    "INVALID_REQUEST",
                    message=(
                        "buying_mode='refine' is not supported by this "
                        "seller. The platform does not implement "
                        "refine_get_products(). Buyers should retry with "
                        "buying_mode='brief' or 'wholesale'."
                    ),
                    field="buying_mode",
                )
            refine_result = await _invoke_platform_method(
                self._platform,
                "refine_get_products",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            )
            # Two refine return shapes coexist:
            # - Direct platform.refine_get_products returns a RefineResult
            #   (typed object with per_refine_outcome) — framework projects
            #   to wire response.
            # - PlatformRouter.refine_get_products forwards to the per-tenant
            #   ProposalManager.refine_products which returns a wire-shaped
            #   GetProductsResponse directly. Skip projection in that case.
            if isinstance(refine_result, RefineResult):
                projected: GetProductsResponse = project_refine_response(
                    refine_result, params.refine or []
                )
                await maybe_persist_draft_after_get_products(self._platform, projected, ctx)
                return projected
            await maybe_persist_draft_after_get_products(self._platform, refine_result, ctx)
            return cast("GetProductsResponse", refine_result)
        # Resolve time_budget to a seconds deadline. _resolve_account and
        # _build_ctx are intentionally outside this try/except so their
        # AdcpErrors propagate unmodified; only the platform call is deadline-
        # wrapped.
        deadline = resolve_time_budget(params.time_budget)
        coro = _invoke_platform_method(
            self._platform,
            "get_products",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        try:
            result = await (
                asyncio.wait_for(coro, timeout=deadline) if deadline is not None else coro
            )
        except asyncio.TimeoutError:
            # Deadline expired. The platform coroutine is cancelled; for
            # sync adopters the underlying thread runs to completion but the
            # asyncio side has moved on (thread-pool slot leak documented in
            # adcp.decisioning.time_budget module header).
            tb = params.time_budget
            interval = tb.interval if tb is not None else 0
            unit_raw = tb.unit if tb is not None else None
            unit = (
                (unit_raw.value if hasattr(unit_raw, "value") else str(unit_raw))
                if unit_raw is not None
                else "unknown"
            )
            logger.warning(
                "[adcp.decisioning] get_products timed out after %ds "
                "(time_budget=%d %s); returning incomplete response. "
                "To avoid timeout cancellations, optimise get_products "
                "latency or reduce the platform's search scope.",
                deadline,
                interval,
                unit,
            )
            return GetProductsResponse.model_validate(
                project_incomplete_response(interval=interval, unit=unit)
            )
        response = cast("GetProductsResponse", result)
        # Post-adapter: capability-gated property-list filter.
        response = cast(
            "GetProductsResponse",
            await maybe_apply_property_list_filter(
                params=params,
                response=response,
                fetcher=self._property_list_fetcher,
                capability_enabled=property_list_capability_enabled(self._platform),
            ),
        )
        if self._platform.capabilities.auto_paginate and params.pagination is not None:
            response = cast(
                "GetProductsResponse",
                apply_framework_pagination(
                    response,
                    params.pagination,
                    _query_hash(params),
                ),
            )
        if params.fields:
            response = _project_product_fields(response, params.fields)
        # v1.5: persist draft proposals from brief / wholesale calls so
        # subsequent finalize / create_media_buy can hydrate from the
        # store. No-op when no proposal_store is wired for this tenant
        # or when the response carries no proposals.
        await maybe_persist_draft_after_get_products(self._platform, response, ctx)
        return response

    async def create_media_buy(  # type: ignore[override]
        self,
        params: CreateMediaBuyRequest,
        context: ToolContext | None = None,
    ) -> CreateMediaBuyResponse:
        from adcp.decisioning.types import AdcpError

        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)

        configs: dict[str, Any] = {}
        if self._config_store is not None:
            # proposal_id flows have packages=None — skip lookup, inject {}
            if params.packages:
                product_ids = list(dict.fromkeys(p.product_id for p in params.packages))
                try:
                    configs = await self._config_store.lookup_implementation_configs(
                        product_ids, ctx
                    )
                except AdcpError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "[adcp.decisioning] ProductConfigStore.lookup_implementation_configs "
                        "raised for create_media_buy — translating to SERVICE_UNAVAILABLE"
                    )
                    raise AdcpError(
                        "SERVICE_UNAVAILABLE",
                        message=(
                            "implementation_config lookup failed for "
                            f"{len(product_ids)} product(s). Retry the request; "
                            "if the problem persists contact the seller."
                        ),
                        recovery="transient",
                        details={"caused_by": {"type": type(exc).__name__}},
                    ) from exc

        # v1.5: when params.proposal_id is set AND a tenant store is
        # wired, hydrate ctx.recipes from the committed proposal +
        # validate expiry / capability overlap before the adapter runs.
        # Returns the ProposalRecord (state=CONSUMING) so we can
        # finalize_consumption on success or release_consumption on
        # failure. The two-phase commit prevents the inventory
        # double-spend race that a check-then-act sequence would expose.
        proposal_record = await maybe_hydrate_recipes_for_create_media_buy(
            self._platform, params, ctx
        )

        extra: dict[str, Any] | None = (
            {"configs": configs} if self._create_media_buy_accepts_configs else None
        )

        # Build the consumption-lifecycle hooks. Used inline on the
        # sync return path AND forwarded into _project_handoff on the
        # TaskHandoff path — same closure, two firing points. Single
        # source of truth for "what to do when create_media_buy lands"
        # regardless of whether it lands now or after HITL approval.
        on_complete: Callable[[Any], Awaitable[None]] | None = None
        on_failure: Callable[[BaseException], Awaitable[None]] | None = None
        if proposal_record is not None:
            captured_record = proposal_record
            captured_ctx = ctx
            captured_platform = self._platform

            async def _finalize_consumption_hook(create_result: Any) -> None:
                # Idempotent on re-call with the same media_buy_id
                # (idempotency_key replays land here too). For the
                # handoff path the create_result is the typed
                # CreateMediaBuySuccess from the bg task, NOT the
                # Submitted envelope — _extract_media_buy_id reads .id
                # off either shape.
                media_buy_id = _extract_media_buy_id(create_result)
                if media_buy_id is not None:
                    await mark_proposal_consumed(
                        captured_platform,
                        captured_record,
                        media_buy_id=media_buy_id,
                        ctx=captured_ctx,
                    )

            async def _release_reservation_hook(_exc: BaseException) -> None:
                # Adapter raised (sync) OR handoff fn raised (HITL) OR
                # finalize_consumption raised: release the reservation
                # so the buyer can retry without PROPOSAL_NOT_COMMITTED
                # blocking them.
                await release_proposal_reservation(captured_platform, captured_record, captured_ctx)

            on_complete = _finalize_consumption_hook
            on_failure = _release_reservation_hook

        result = await _invoke_platform_method(
            self._platform,
            "create_media_buy",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            extra_kwargs=extra,
            on_complete=on_complete,
            on_failure=on_failure,
        )
        self._maybe_auto_emit_sync_completion("create_media_buy", params, result)
        return cast("CreateMediaBuyResponse", result)

    async def update_media_buy(  # type: ignore[override]
        self,
        params: UpdateMediaBuyRequest,
        context: ToolContext | None = None,
    ) -> UpdateMediaBuySuccessResponse:
        """Wire shape carries ``media_buy_id`` + the patch fields at the
        same level on ``UpdateMediaBuyRequest``. The platform method
        signature is ``update_media_buy(media_buy_id, patch, ctx)`` —
        cleaner adopter ergonomics. Arg-projection per D1.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        # v1.5: hydrate ctx.recipes from the consumed proposal via the
        # ProposalStore reverse-index. Re-validates capability overlap
        # against any packages on the patch (Resolutions §5).
        await maybe_hydrate_recipes_for_media_buy_id(
            self._platform,
            params.media_buy_id,
            ctx,
            packages=list(getattr(params, "packages", None) or []),
        )
        result = await _invoke_platform_method(
            self._platform,
            "update_media_buy",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            arg_projector={"media_buy_id": params.media_buy_id, "patch": params},
        )
        self._maybe_auto_emit_sync_completion("update_media_buy", params, result)
        return cast("UpdateMediaBuySuccessResponse", result)

    async def sync_creatives(  # type: ignore[override]
        self,
        params: SyncCreativesRequest,
        context: ToolContext | None = None,
    ) -> SyncCreativesSuccessResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "sync_creatives",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("sync_creatives", params, result)
        return cast("SyncCreativesSuccessResponse", result)

    async def get_media_buy_delivery(  # type: ignore[override]
        self,
        params: GetMediaBuyDeliveryRequest,
        context: ToolContext | None = None,
    ) -> GetMediaBuyDeliveryResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        # v1.5: hydrate ctx.recipes for the consumed proposal — adapter
        # reads ``ctx.recipes[product_id]`` for per-product delivery
        # logic. Hydrates from the first media_buy_id on the request;
        # multi-buy responses re-hydrate per call.
        media_buy_ids = list(getattr(params, "media_buy_ids", None) or [])
        first_id = str(media_buy_ids[0]) if media_buy_ids else ""
        if first_id:
            await maybe_hydrate_recipes_for_media_buy_id(self._platform, first_id, ctx)
        return cast(
            "GetMediaBuyDeliveryResponse",
            await _invoke_platform_method(
                self._platform,
                "get_media_buy_delivery",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    # ----- Optional sales tools (gated by capabilities + override) -----

    async def get_media_buys(  # type: ignore[override]
        self,
        params: GetMediaBuysRequest,
        context: ToolContext | None = None,
    ) -> GetMediaBuysResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetMediaBuysResponse",
            await _invoke_platform_method(
                self._platform,
                "get_media_buys",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def provide_performance_feedback(  # type: ignore[override]
        self,
        params: ProvidePerformanceFeedbackRequest,
        context: ToolContext | None = None,
    ) -> ProvidePerformanceFeedbackResponse:
        """Wire request has no ``account`` field — resolve via auth only.
        Adopters in ``explicit`` resolution mode get an
        ``ACCOUNT_NOT_FOUND`` from their AccountStore unless they wire
        a derived/singleton path or extend ``AccountStore.resolve`` to
        handle the no-ref case (see python-port-v2 RFC TODO(rc.1))."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ProvidePerformanceFeedbackResponse",
            await _invoke_platform_method(
                self._platform,
                "provide_performance_feedback",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def list_creative_formats(  # type: ignore[override]
        self,
        params: ListCreativeFormatsRequest,
        context: ToolContext | None = None,
    ) -> ListCreativeFormatsResponse:
        """Wire request has no ``account`` field. See
        :meth:`provide_performance_feedback` for the no-ref account
        resolution caveat."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ListCreativeFormatsResponse",
            await _invoke_platform_method(
                self._platform,
                "list_creative_formats",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def list_creatives(  # type: ignore[override]
        self,
        params: ListCreativesRequest,
        context: ToolContext | None = None,
    ) -> ListCreativesResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ListCreativesResponse",
            await _invoke_platform_method(
                self._platform,
                "list_creatives",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    # ----- Optional-method gate -----

    def _require_platform_method(self, method_name: str) -> None:
        """Raise ``UNSUPPORTED_FEATURE`` if the adopter's platform
        doesn't implement ``method_name``.

        Used by shims for OPTIONAL Protocol methods (per
        ``_OPTIONAL_PLATFORM_METHODS``). Required methods are caught
        at server boot by ``validate_platform``; optional methods can
        legitimately be absent and need a runtime gate. Without this,
        a buyer calling an optional method on a platform that doesn't
        implement it would see ``INTERNAL_ERROR`` from the
        AttributeError wrapper in ``_invoke_platform_method`` —
        adopter contract violation, not buyer-fixable.
        """
        if not hasattr(self._platform, method_name):
            from adcp.decisioning.types import AdcpError

            raise AdcpError(
                "UNSUPPORTED_FEATURE",
                message=(
                    f"This platform doesn't implement {method_name!r}. "
                    "The method is optional on the per-specialism Protocol; "
                    "the adopter chose not to wire it."
                ),
                recovery="terminal",
            )

    # ----- CreativeBuilderPlatform / CreativeAdServerPlatform -----

    async def build_creative(  # type: ignore[override]
        self,
        params: BuildCreativeRequest,
        context: ToolContext | None = None,
    ) -> BuildCreativeResponse:
        """Build / retrieve a creative.

        Three discriminated return arms per the per-specialism
        Protocol: a single :class:`CreativeManifest`, a list of
        manifests (multi-format), or a fully-shaped
        :class:`BuildCreativeSuccessResponse`. The shim projects bare
        manifests / lists to the wire envelope shape so adopters can
        return the ergonomic form (per the JS-side
        ``projectBuildCreativeReturn`` parity).

        Wire envelope per
        ``schemas/cache/media-buy/build-creative-response.json``:
        ``{creative_manifest: ...}`` (single) or
        ``{creative_manifests: [...]}`` (multi).
        """
        self._require_platform_method("build_creative")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "build_creative",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        return cast("BuildCreativeResponse", _project_build_creative(result))

    async def preview_creative(  # type: ignore[override]
        self,
        params: PreviewCreativeRequest,
        context: ToolContext | None = None,
    ) -> PreviewCreativeResponse:
        """Optional on :class:`CreativeBuilderPlatform`; required on
        :class:`CreativeAdServerPlatform`. Surface
        ``UNSUPPORTED_FEATURE`` when the adopter's platform doesn't
        implement it (Builder adopters who don't render preview)."""
        self._require_platform_method("preview_creative")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "PreviewCreativeResponse",
            await _invoke_platform_method(
                self._platform,
                "preview_creative",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def get_creative_delivery(  # type: ignore[override]
        self,
        params: GetCreativeDeliveryRequest,
        context: ToolContext | None = None,
    ) -> GetCreativeDeliveryResponse:
        """Required on :class:`CreativeAdServerPlatform` — per-creative
        delivery actuals (impressions, spend, pacing)."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "get_creative_delivery",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("get_creative_delivery", params, result)
        return cast("GetCreativeDeliveryResponse", result)

    # ----- SignalsPlatform -----

    async def get_signals(  # type: ignore[override]
        self,
        params: GetSignalsRequest,
        context: ToolContext | None = None,
    ) -> GetSignalsResponse:
        """Catalog discovery for signal-marketplace / signal-owned."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "get_signals",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("get_signals", params, result)
        return cast("GetSignalsResponse", result)

    async def activate_signal(  # type: ignore[override]
        self,
        params: ActivateSignalRequest,
        context: ToolContext | None = None,
    ) -> ActivateSignalSuccessResponse:
        """Provision a signal onto destination platforms."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "activate_signal",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("activate_signal", params, result)
        return cast("ActivateSignalSuccessResponse", result)

    # ----- AudiencePlatform -----

    async def sync_audiences(  # type: ignore[override]
        self,
        params: SyncAudiencesRequest,
        context: ToolContext | None = None,
    ) -> SyncAudiencesSuccessResponse:
        """Push audiences to the platform.

        Wire shape carries ``audiences[]`` on the request; the platform
        method signature is ``sync_audiences(audiences, ctx)`` — adopter
        ergonomic per the JS reference. Arg-projection extracts the
        list.

        Two return arms per the per-specialism Protocol: a list of
        audience-result rows (the JS-side ergonomic) or a fully-shaped
        :class:`SyncAudiencesSuccessResponse`. The shim projects the
        list arm to the wire envelope ``{audiences: [...]}`` so adopters
        can return the ergonomic form.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "sync_audiences",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            arg_projector={"audiences": getattr(params, "audiences", []) or []},
        )
        projected = _project_sync_audiences(result)
        self._maybe_auto_emit_sync_completion("sync_audiences", params, projected)
        return cast("SyncAudiencesSuccessResponse", projected)

    # ----- CampaignGovernancePlatform -----

    async def check_governance(  # type: ignore[override]
        self,
        params: CheckGovernanceRequest,
        context: ToolContext | None = None,
    ) -> CheckGovernanceResponse:
        """Runtime governance decision (approved / denied / conditions).

        Wire request has no ``account`` field per
        ``schemas/cache/governance/check-governance-request.json``
        (``additionalProperties: false`` — account is forbidden);
        resolve via auth only. See :meth:`provide_performance_feedback`
        for the no-ref account resolution caveat.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "CheckGovernanceResponse",
            await _invoke_platform_method(
                self._platform,
                "check_governance",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def sync_plans(  # type: ignore[override]
        self,
        params: SyncPlansRequest,
        context: ToolContext | None = None,
    ) -> SyncPlansResponse:
        """Plan CRUD with delta upsert into governance agent.

        Wire request has no ``account`` field per
        ``schemas/cache/governance/sync-plans-request.json``
        (``additionalProperties: false``); resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "SyncPlansResponse",
            await _invoke_platform_method(
                self._platform,
                "sync_plans",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def report_plan_outcome(  # type: ignore[override]
        self,
        params: ReportPlanOutcomeRequest,
        context: ToolContext | None = None,
    ) -> ReportPlanOutcomeResponse:
        """Outcome reporting from sellers (delivery actuals).

        Wire request has no ``account`` field per
        ``schemas/cache/governance/report-plan-outcome-request.json``
        (``additionalProperties: false``); resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ReportPlanOutcomeResponse",
            await _invoke_platform_method(
                self._platform,
                "report_plan_outcome",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def get_plan_audit_logs(  # type: ignore[override]
        self,
        params: GetPlanAuditLogsRequest,
        context: ToolContext | None = None,
    ) -> GetPlanAuditLogsResponse:
        """Audit log read for governance decisions + outcomes.

        Wire request has no ``account`` field per
        ``schemas/cache/governance/get-plan-audit-logs-request.json``
        (``additionalProperties: false``); resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetPlanAuditLogsResponse",
            await _invoke_platform_method(
                self._platform,
                "get_plan_audit_logs",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    # ----- BrandRightsPlatform -----

    async def get_brand_identity(  # type: ignore[override]
        self,
        params: GetBrandIdentityRequest,
        context: ToolContext | None = None,
    ) -> GetBrandIdentitySuccessResponse:
        """Read brand identity record (catalog + identity record).

        Wire request has no ``account`` field per
        ``schemas/cache/brand-rights/get-brand-identity-request.json``
        (``additionalProperties: false``); resolve via auth only. See
        :meth:`provide_performance_feedback` for the no-ref account
        resolution caveat.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "get_brand_identity",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("get_brand_identity", params, result)
        return cast("GetBrandIdentitySuccessResponse", result)

    async def get_rights(  # type: ignore[override]
        self,
        params: GetRightsRequest,
        context: ToolContext | None = None,
    ) -> GetRightsSuccessResponse:
        """List rights matching a brand + use query.

        Wire request has no ``account`` field per
        ``schemas/cache/brand-rights/get-rights-request.json``
        (``additionalProperties: false``); resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "get_rights",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("get_rights", params, result)
        return cast("GetRightsSuccessResponse", result)

    async def acquire_rights(  # type: ignore[override]
        self,
        params: AcquireRightsRequest,
        context: ToolContext | None = None,
    ) -> AcquireRightsResponse:
        """Acquire rights — 4-arm discriminated success union
        (acquired / pending / rejected / error). Rejection-as-data per
        the Protocol; the ``error`` arm covers rights-system failures
        the buyer can retry against (vs. ``AdcpError`` for adopter
        infrastructure failures).

        Wire request has no ``account`` field per
        ``schemas/cache/brand-rights/acquire-rights-request.json``
        (``additionalProperties: false``); resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "acquire_rights",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("acquire_rights", params, result)
        return cast("AcquireRightsResponse", result)

    async def update_rights(  # type: ignore[override]
        self,
        params: UpdateRightsRequest,
        context: ToolContext | None = None,
    ) -> UpdateRightsResponse:
        """Mutate an existing rights acquisition (extend term, change
        usage scope, revoke).

        Wire request has no ``account`` field per
        ``schemas/cache/brand-rights/update-rights-request.json``
        (``additionalProperties: false``); resolve via auth only.

        Not currently in :data:`SPEC_WEBHOOK_TASK_TYPES` — buyers
        registering ``push_notification_config.url`` won't get an
        auto-emit; rely on ``publishStatusChange`` for long-running
        update state. Bump the spec enum and the
        ``SPEC_WEBHOOK_TASK_TYPES`` constant in lockstep when this
        joins the closed enum.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "UpdateRightsResponse",
            await _invoke_platform_method(
                self._platform,
                "update_rights",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    # ----- ContentStandardsPlatform -----

    async def list_content_standards(  # type: ignore[override]
        self,
        params: ListContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> ListContentStandardsResponse:
        """Discover content standards published by this agent.

        Wire request has no ``account`` field per
        ``schemas/cache/content-standards/list-content-standards-request.json``;
        resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ListContentStandardsResponse",
            await _invoke_platform_method(
                self._platform,
                "list_content_standards",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def get_content_standards(  # type: ignore[override]
        self,
        params: GetContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> GetContentStandardsResponse:
        """Wire request has no ``account`` field per
        ``schemas/cache/content-standards/get-content-standards-request.json``;
        resolve via auth only."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetContentStandardsResponse",
            await _invoke_platform_method(
                self._platform,
                "get_content_standards",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def create_content_standards(  # type: ignore[override]
        self,
        params: CreateContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> CreateContentStandardsResponse:
        """Wire request has no ``account`` field; resolve via auth only."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "CreateContentStandardsResponse",
            await _invoke_platform_method(
                self._platform,
                "create_content_standards",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def update_content_standards(  # type: ignore[override]
        self,
        params: UpdateContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> UpdateContentStandardsResponse:
        """Wire request has no ``account`` field; resolve via auth only."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "UpdateContentStandardsResponse",
            await _invoke_platform_method(
                self._platform,
                "update_content_standards",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def calibrate_content(  # type: ignore[override]
        self,
        params: CalibrateContentRequest,
        context: ToolContext | None = None,
    ) -> CalibrateContentResponse:
        """Calibrate content against published standards.

        Wire request has no ``account`` field per
        ``schemas/cache/content-standards/calibrate-content-request.json``;
        resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "CalibrateContentResponse",
            await _invoke_platform_method(
                self._platform,
                "calibrate_content",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def validate_content_delivery(  # type: ignore[override]
        self,
        params: ValidateContentDeliveryRequest,
        context: ToolContext | None = None,
    ) -> ValidateContentDeliveryResponse:
        """Post-flight conformance check.

        Wire request has no ``account`` field per
        ``schemas/cache/content-standards/validate-content-delivery-request.json``;
        resolve via auth only.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ValidateContentDeliveryResponse",
            await _invoke_platform_method(
                self._platform,
                "validate_content_delivery",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def get_media_buy_artifacts(  # type: ignore[override]
        self,
        params: GetMediaBuyArtifactsRequest,
        context: ToolContext | None = None,
    ) -> GetMediaBuyArtifactsResponse:
        """Optional analyzer read — adopters without artifact archival
        surface ``UNSUPPORTED_FEATURE``."""
        self._require_platform_method("get_media_buy_artifacts")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetMediaBuyArtifactsResponse",
            await _invoke_platform_method(
                self._platform,
                "get_media_buy_artifacts",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def get_creative_features(  # type: ignore[override]
        self,
        params: GetCreativeFeaturesRequest,
        context: ToolContext | None = None,
    ) -> GetCreativeFeaturesResponse:
        """Optional analyzer read — adopters without analyzer pipelines
        surface ``UNSUPPORTED_FEATURE``."""
        self._require_platform_method("get_creative_features")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetCreativeFeaturesResponse",
            await _invoke_platform_method(
                self._platform,
                "get_creative_features",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    # ----- PropertyListsPlatform -----

    async def create_property_list(  # type: ignore[override]
        self,
        params: CreatePropertyListRequest,
        context: ToolContext | None = None,
    ) -> CreatePropertyListResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "create_property_list",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("create_property_list", params, result)
        return cast("CreatePropertyListResponse", result)

    async def update_property_list(  # type: ignore[override]
        self,
        params: UpdatePropertyListRequest,
        context: ToolContext | None = None,
    ) -> UpdatePropertyListResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "update_property_list",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("update_property_list", params, result)
        return cast("UpdatePropertyListResponse", result)

    async def get_property_list(  # type: ignore[override]
        self,
        params: GetPropertyListRequest,
        context: ToolContext | None = None,
    ) -> GetPropertyListResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "get_property_list",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("get_property_list", params, result)
        return cast("GetPropertyListResponse", result)

    async def list_property_lists(  # type: ignore[override]
        self,
        params: ListPropertyListsRequest,
        context: ToolContext | None = None,
    ) -> ListPropertyListsResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "list_property_lists",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("list_property_lists", params, result)
        return cast("ListPropertyListsResponse", result)

    async def delete_property_list(  # type: ignore[override]
        self,
        params: DeletePropertyListRequest,
        context: ToolContext | None = None,
    ) -> DeletePropertyListResponse:
        """Security-critical: revokes the per-seller fetch_token and
        signals cache invalidation. Compromise-driven revocation MUST
        also trigger this path."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "delete_property_list",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("delete_property_list", params, result)
        return cast("DeletePropertyListResponse", result)

    # ----- CollectionListsPlatform -----

    async def create_collection_list(  # type: ignore[override]
        self,
        params: CreateCollectionListRequest,
        context: ToolContext | None = None,
    ) -> CreateCollectionListResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "CreateCollectionListResponse",
            await _invoke_platform_method(
                self._platform,
                "create_collection_list",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def update_collection_list(  # type: ignore[override]
        self,
        params: UpdateCollectionListRequest,
        context: ToolContext | None = None,
    ) -> UpdateCollectionListResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "UpdateCollectionListResponse",
            await _invoke_platform_method(
                self._platform,
                "update_collection_list",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def get_collection_list(  # type: ignore[override]
        self,
        params: GetCollectionListRequest,
        context: ToolContext | None = None,
    ) -> GetCollectionListResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetCollectionListResponse",
            await _invoke_platform_method(
                self._platform,
                "get_collection_list",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def list_collection_lists(  # type: ignore[override]
        self,
        params: ListCollectionListsRequest,
        context: ToolContext | None = None,
    ) -> ListCollectionListsResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ListCollectionListsResponse",
            await _invoke_platform_method(
                self._platform,
                "list_collection_lists",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def delete_collection_list(  # type: ignore[override]
        self,
        params: DeleteCollectionListRequest,
        context: ToolContext | None = None,
    ) -> DeleteCollectionListResponse:
        """Security-critical: revokes the fetch_token. See
        :meth:`delete_property_list` for the same security contract."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "DeleteCollectionListResponse",
            await _invoke_platform_method(
                self._platform,
                "delete_collection_list",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )


__all__ = ["PlatformHandler"]
