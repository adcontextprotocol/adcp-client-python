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
from adcp.decisioning.account_projection import (
    strip_credentials_from_wire_result,
    to_wire_account,
    to_wire_sync_accounts_row,
)
from adcp.decisioning.accounts import ResolveContext, _call_with_optional_ctx
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.discovery_guards import (
    assert_account_resolved_for_async,
    assert_discovery_push_consistent,
    reject_hand_rolled_submitted,
    reject_wholesale_handoff,
    reject_wholesale_handoff_before_launch,
)
from adcp.decisioning.dispatch import (
    _build_request_context,
    _invoke_platform_method,
)
from adcp.decisioning.implementation_config import ProductConfigStore
from adcp.decisioning.pagination import _query_hash, apply_framework_pagination
from adcp.decisioning.platform import DecisioningCapabilities
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
    maybe_validate_refine_proposal_refs,
    release_proposal_reservation,
)
from adcp.decisioning.refine import (
    RefineResult,
    assert_buying_mode_consistent,
    has_refine_support,
    project_refine_response,
)
from adcp.decisioning.time_budget import project_incomplete_response, resolve_time_budget
from adcp.decisioning.types import (
    Account as _DecisioningAccount,
)
from adcp.decisioning.types import (
    SyncAccountsResultRow as _SyncAccountsResultRow,
)
from adcp.decisioning.webhook_emit import maybe_emit_sync_completion
from adcp.server.base import ADCPHandler, NotImplementedResponse, ToolContext

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
    GetAdcpCapabilitiesRequest,
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
    ListAccountsRequest,
    ListAccountsResponse,
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
    SyncAccountsRequest,
    SyncAccountsResponse,
    SyncAudiencesRequest,
    SyncAudiencesSuccessResponse,
    SyncCatalogsRequest,
    SyncCatalogsSuccessResponse,
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
    ValidateInputRequest,
    ValidateInputResponse,
    VerifyBrandClaimRequest,
    VerifyBrandClaimResponse,
    VerifyBrandClaimsRequest,
    VerifyBrandClaimsResponseBulk,
)

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning.brand_authz_gate import BrandAuthorizationGate
    from adcp.decisioning.media_buy_store import MediaBuyStore
    from adcp.decisioning.platform import DecisioningPlatform
    from adcp.decisioning.property_list import PropertyListFetcher
    from adcp.decisioning.registry import BuyerAgent, BuyerAgentRegistry
    from adcp.decisioning.resolve import ResourceResolver
    from adcp.decisioning.state import StateReader
    from adcp.decisioning.task_registry import TaskRegistry
    from adcp.decisioning.types import Account
    from adcp.webhook_sender import WebhookSender
    from adcp.webhook_supervisor import WebhookDeliverySupervisor


#: Metadata key the framework uses to stash the registry-resolved
#: :class:`BuyerAgent` on :class:`ToolContext.metadata`. Stashed by
#: :meth:`PlatformHandler._resolve_account` and consumed by
#: :meth:`PlatformHandler._build_ctx` (typed :class:`RequestContext`
#: dispatch path) and :meth:`PlatformHandler._make_resolve_context`
#: (:class:`AccountStore` dispatch path). Module-level constant so
#: handler subclasses and adopter-side middleware can reference the
#: same key without repeating the string literal.
_BUYER_AGENT_METADATA_KEY = "adcp.buyer_agent"

#: Metadata key the framework uses to stash verified :class:`AuthInfo`.
#: Populated by :class:`adcp.server.serve` from the canonical principal;
#: consumed by :meth:`PlatformHandler._extract_auth_info`.
_AUTH_INFO_METADATA_KEY = "adcp.auth_info"


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
#: Account roster surface unioned into every sales-* claim. Per the
#: ``sync_accounts`` / ``list_accounts`` spec, every sales adopter MUST
#: expose an account roster so buyers can declare implicit accounts
#: (``sync_accounts``) or discover explicit ones (``list_accounts``).
#: The per-instance filter at :meth:`PlatformHandler.advertised_tools_for_instance`
#: drops the tool when the platform's :class:`AccountStore` doesn't
#: expose the corresponding optional Protocol method, so adopters who
#: haven't wired :class:`AccountStoreUpsert` / :class:`AccountStoreList`
#: don't accidentally over-advertise.
_ACCOUNT_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "sync_accounts",
        "list_accounts",
    }
)
_CREATIVE_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "build_creative",
        "preview_creative",
        "get_creative_delivery",
        "validate_input",
    }
)
_SIGNALS_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "get_signals",
        "activate_signal",
    }
)
_OWNED_SIGNALS_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "get_signals",
    }
)
_AUDIENCE_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "sync_audiences",
    }
)
_CATALOG_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "sync_catalogs",
    }
)
_SPONSORED_INTELLIGENCE_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "si_get_offering",
        "si_initiate_session",
        "si_send_message",
        "si_terminate_session",
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
        "verify_brand_claim",
        "verify_brand_claims",
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
        "validate_input",
        # BrandRightsPlatform optional verification reads.
        "verify_brand_claim",
        "verify_brand_claims",
        # ContentStandardsPlatform optional analyzer reads
        "get_media_buy_artifacts",
        "get_creative_features",
        # signal-owned platforms expose discovery-only owned signals;
        # activate_signal remains required for signal-marketplace.
        "activate_signal",
        # AudiencePlatform adopter-internal helper (not wire-served, but
        # listed here for symmetry should a future shim wire it)
        "poll_audience_statuses",
        # Required for sales-catalog-driven; absent on all other sales-* platforms
        "sync_catalogs",
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
    # Every sales-* claim implies an account roster (``sync_accounts``
    # / ``list_accounts``); the per-instance filter drops them when
    # the platform's AccountStore doesn't expose the optional Protocols.
    "sales-non-guaranteed": _SALES_ADVERTISED_TOOLS | _ACCOUNT_ADVERTISED_TOOLS,
    "sales-guaranteed": _SALES_ADVERTISED_TOOLS | _ACCOUNT_ADVERTISED_TOOLS,
    "sales-broadcast-tv": _SALES_ADVERTISED_TOOLS | _ACCOUNT_ADVERTISED_TOOLS,
    "sales-social": _SALES_ADVERTISED_TOOLS | _ACCOUNT_ADVERTISED_TOOLS,
    "sales-catalog-driven": (
        _SALES_ADVERTISED_TOOLS | _ACCOUNT_ADVERTISED_TOOLS | _CATALOG_ADVERTISED_TOOLS
    ),
    "sales-proposal-mode": _SALES_ADVERTISED_TOOLS | _ACCOUNT_ADVERTISED_TOOLS,
    # Creative — Builder + Transformers + AdServer. Builder claims expose
    # build_creative + optional preview_creative; AdServer adds
    # get_creative_delivery (per CreativeAdServerPlatform Protocol).
    # The rc.8 transformer slug currently shares the creative builder
    # surface in the decisioning framework; the standalone wire
    # ``list_transformers`` tool is present on ADCPHandler but does not
    # have a decisioning shim yet. The per-method override filter
    # (``_is_method_overridden``) drops unimplemented optionals.
    "creative-generative": _CREATIVE_ADVERTISED_TOOLS,
    "creative-template": _CREATIVE_ADVERTISED_TOOLS,
    "creative-transformers": _CREATIVE_ADVERTISED_TOOLS,
    "creative-ad-server": _CREATIVE_ADVERTISED_TOOLS,
    # Signals — marketplace/provisioned signals need activation;
    # seller-owned signals are discovery-only because they are already
    # usable on that seller's inventory.
    "signal-marketplace": _SIGNALS_ADVERTISED_TOOLS,
    "signal-owned": _OWNED_SIGNALS_ADVERTISED_TOOLS,
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
    "sponsored-intelligence": _SPONSORED_INTELLIGENCE_ADVERTISED_TOOLS,
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
    # Creative — generative / template / transformers / ad-server all expose creative
    # tools; the ad-server variant additionally exposes
    # ``get_creative_delivery`` which is a media_buy companion read,
    # but the wire protocol is still ``creative``.
    "creative-generative": frozenset({"creative"}),
    "creative-template": frozenset({"creative"}),
    "creative-transformers": frozenset({"creative"}),
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

    Per-agent commercial-status rejections surface as dedicated codes
    (``AGENT_SUSPENDED`` / ``AGENT_BLOCKED``) per AdCP 3.1; the
    unrecognized-identity path surfaces as ``PERMISSION_DENIED`` with
    no ``details.scope``. This split prevents the cross-tenant
    onboarding-oracle risk on the unrecognized path (no discriminator
    that would let an attacker distinguish "this agent_url is
    unrecognized at this seller" from a credential-shaped failure
    against a known agent), while the dedicated codes carry their own
    discriminator without needing a ``details`` payload.

    * recognized + suspended → ``code="AGENT_SUSPENDED"``,
      ``recovery="terminal"``, no ``details`` payload.
    * recognized + blocked → ``code="AGENT_BLOCKED"``,
      ``recovery="terminal"``, no ``details`` payload.
    * unrecognized (registry miss / no credential / unknown status) →
      ``code="PERMISSION_DENIED"``, no ``details`` — scope MUST NOT be
      set on the unestablished-identity path
      (omit-on-unestablished-identity rule).

    Timing-oracle defense on ``PERMISSION_DENIED``: both branches that
    surface ``PERMISSION_DENIED`` (registry-miss and unknown-status
    default-reject) flow through
    :class:`adcp.decisioning._permission_denied_budget.PermissionDeniedBudget`
    so registry I/O variance between the cache-hit-returning-None and
    cache-hit-returning-real-row paths is absorbed into a fixed budget
    (default 50 ms, tunable via ``ADCP_PERMISSION_DENIED_BUDGET_MS``).
    The dedicated-code branches (``AGENT_SUSPENDED`` / ``AGENT_BLOCKED``)
    intentionally skip the budget — the code itself is the discriminator,
    so latency parity carries no additional bit.

    :raises AdcpError: ``AGENT_SUSPENDED`` / ``AGENT_BLOCKED`` /
        ``PERMISSION_DENIED`` depending on path (see above). The
        dedicated per-agent codes carry ``recovery="terminal"``: the
        buyer cannot auto-retry a commercial-identity rejection, and
        the code itself is the discriminator surfaced to a human
        operator. ``PERMISSION_DENIED`` on the unrecognized path
        carries ``recovery="correctable"`` per the spec's
        ``enumMetadata``.
    """
    from adcp.decisioning._permission_denied_budget import PermissionDeniedBudget
    from adcp.decisioning.registry import (
        ApiKeyCredential,
        HttpSigCredential,
        OAuthCredential,
    )
    from adcp.decisioning.types import AdcpError

    budget = PermissionDeniedBudget()
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
        await budget.enforce()
        raise AdcpError(
            "PERMISSION_DENIED",
            message=_denied_message,
            recovery="correctable",
        )

    if agent.status == "active":
        return agent
    if agent.status == "suspended":
        # AdCP 3.1 dedicated code — the code itself is the discriminator,
        # no ``details`` payload. ``recovery="terminal"`` per the spec
        # (the placeholder ``PERMISSION_DENIED + details.status`` shape
        # in 3.0.5 inherited ``correctable`` from PERMISSION_DENIED,
        # which contradicted the no-retry MUST for this path).
        raise AdcpError(
            "AGENT_SUSPENDED",
            message=_denied_message,
            recovery="terminal",
        )
    if agent.status == "blocked":
        raise AdcpError(
            "AGENT_BLOCKED",
            message=_denied_message,
            recovery="terminal",
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
    await budget.enforce()
    raise AdcpError(
        "PERMISSION_DENIED",
        message=_denied_message,
        recovery="correctable",
    )


async def _enforce_brand_authorization(
    gate: BrandAuthorizationGate,
    account: Account[Any],
    buyer_agent: BuyerAgent | None,
) -> None:
    """Tier 3 per-brand authorization gate.

    Runs after Tier 2 (:func:`_resolve_buyer_agent`) and
    :meth:`AccountStore.resolve` — by this point the framework has a
    verified, active buyer-agent and a resolved account. The gate asks
    the adopter's :class:`BrandAuthorizationResolver`: is this agent
    authorized to act *for this brand*?

    Sourcing the brand identity is the adopter's call — the framework
    delegates via :attr:`BrandAuthorizationGate.extract_identity`. The
    extractor reads whatever signal the seller's account model carries
    (``account.metadata['brand_domain']``, the natural-key reference's
    ``brand.domain``, an internal mapping, etc.) and returns a
    :class:`BrandIdentity` or ``None``. ``None`` means "this request
    isn't brand-scoped" — the gate skips, the platform method runs.

    The extractor MAY be async. Both sync (``-> BrandIdentity | None``)
    and async (``-> Awaitable[BrandIdentity | None]``) shapes are
    supported so adopters can fetch from a remote registry without
    wrapping in :func:`asyncio.to_thread`.

    **No buyer-agent → no gate.** When Tier 2 isn't wired (the
    framework has no ``buyer_agent``), the gate skips: brand
    authorization without a buyer-agent identity has no subject to
    authorize. Adopters wiring Tier 3 without Tier 2 see the gate
    silently pass — the boot validation in :func:`serve` doesn't
    enforce the Tier-2-before-Tier-3 pairing because some adopters
    legitimately want brand identity flowing through dispatch as a
    capability without commercial-identity gating (read-only audit
    paths, etc.). They get a no-op gate, not a rejection.

    **Denial surface.** Rejection emits the generic
    ``PERMISSION_DENIED`` (``recovery="correctable"``) with the same
    cross-tenant-safe message as Tier 2's unrecognized-agent rejection.
    Spec-shape ``request_signature_brand_origin_mismatch`` /
    ``request_signature_agent_not_in_brand_json`` codes belong to the
    verifier-side wire path (issue #776 will plumb the
    ``BrandJsonJwksResolver`` source discriminant through to the
    verifier — this dispatch-layer gate emits the
    framework-cross-cutting denial code).

    Timing-oracle defense: same :class:`PermissionDeniedBudget` shape
    as Tier 2. The brand.json fetch path's natural variance (cache hit
    vs miss vs stale-on-error) is larger than the registry-lookup
    variance Tier 2 absorbs, so the budget here is a floor, not a
    ceiling — it prevents the cache-hit-authorized vs
    cache-hit-rejected paths from leaking timing-distinguishable
    decisions on the happy-cache path, which is the common case.

    :raises AdcpError: ``PERMISSION_DENIED`` when the resolver denies
        authorization. ``recovery="correctable"`` per the spec's
        ``enumMetadata`` for ``PERMISSION_DENIED``.
    """
    import inspect

    from adcp.decisioning._permission_denied_budget import PermissionDeniedBudget
    from adcp.decisioning.types import AdcpError

    if buyer_agent is None:
        return

    maybe_identity = gate.extract_identity(account, buyer_agent)
    if inspect.isawaitable(maybe_identity):
        identity = await maybe_identity
    else:
        identity = maybe_identity
    if identity is None:
        return

    budget = PermissionDeniedBudget()
    authorized = await gate.resolver.is_authorized(
        agent_url=buyer_agent.agent_url,
        brand_domain=identity.domain,
        brand_id=identity.id,
    )
    if authorized:
        return

    # Same generic message as the Tier 2 unrecognized-agent rejection.
    # Identical wire-level error.message across both gates so the
    # message itself is not a side channel discriminating
    # commercial-identity rejection from brand-authorization rejection.
    _denied_message = (
        "Buyer agent is not authorized for this seller. The seller's "
        "commercial allowlist did not authorize this credential. "
        "Resolve out-of-band via the seller's onboarding contact; this "
        "is not a request-side error the buyer can correct."
    )
    await budget.enforce()
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


def _project_sync_catalogs(result: Any) -> Any:
    """Project the adopter's ``sync_catalogs`` return into the wire envelope shape.

    Adopters may return a list of catalog-result rows (ergonomic form) or a
    fully-shaped :class:`SyncCatalogsSuccessResponse`. The wire envelope per
    ``schemas/cache/media-buy/sync-catalogs-response.json`` is
    ``{catalogs: [rows]}``. This helper wraps the list case.
    """
    if isinstance(result, list):
        return {
            "catalogs": [
                r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in result
            ]
        }
    return result


def _project_sync_accounts(result: Any) -> Any:
    """Project the adopter's ``upsert`` return into the
    ``sync_accounts`` wire envelope.

    :class:`AccountStoreUpsert` returns ``list[SyncAccountsResultRow]``
    per the documented Protocol contract; the wire response per
    ``schemas/cache/account/sync-accounts-response.json`` is
    ``{accounts: [rows]}``. Adopters returning a pre-shaped envelope
    (Pydantic ``SyncAccountsResponse`` or a dict carrying ``accounts``)
    are projected through ``model_dump`` so the credential scrubber
    sees a uniform dict shape.

    Each row passes through :func:`to_wire_sync_accounts_row` (applying
    the ``billing_entity.bank`` write-only strip) when it's a typed
    :class:`SyncAccountsResultRow`; loose dicts and Pydantic
    ``extra='allow'`` rows are scrubbed by
    :func:`strip_credentials_from_wire_result` on the final envelope.
    """
    if isinstance(result, list):
        rows: list[Any] = []
        for row in result:
            if isinstance(row, _SyncAccountsResultRow):
                rows.append(to_wire_sync_accounts_row(row))
            elif hasattr(row, "model_dump"):
                rows.append(row.model_dump(mode="json"))
            else:
                rows.append(row)
        envelope: Any = {"accounts": rows}
    elif hasattr(result, "model_dump"):
        # Adopter returned a pre-shaped Pydantic envelope
        # (``SyncAccountsResponse(...)``) — dump to a dict so the
        # credential scrubber's dict-walker reaches every nested
        # ``governance_agents[i].authentication`` and
        # ``billing_entity.bank``. Without the dump the scrubber
        # passes the Pydantic instance through unchanged (response-side
        # codegen schemas don't define those keys, but ``extra='allow'``
        # adopter rows can smuggle them).
        envelope = result.model_dump(mode="json", exclude_none=True)
    elif isinstance(result, dict):
        envelope = result
    else:
        # Adopter returned an unexpected shape (``None``, a tuple, a
        # bare string). Pass through so the wire validator can surface
        # a precise mis-shape error, but warn loudly — the credential
        # scrubber relies on dict-or-list shape, and an unexpected
        # return is an adopter-contract violation that would otherwise
        # fail silently.
        logger.warning(
            "AccountStore.upsert returned an unexpected shape %s; "
            "expected list[SyncAccountsResultRow] or a dict envelope. "
            "Passing through unchanged — credential scrubber may not "
            "reach nested fields.",
            type(result).__name__,
        )
        envelope = result
    return strip_credentials_from_wire_result("sync_accounts", envelope)


def _project_list_accounts(result: Any) -> Any:
    """Project the adopter's ``list`` return into the ``list_accounts``
    wire envelope.

    :class:`AccountStoreList` returns ``list[Account[TMeta]]`` per the
    documented Protocol contract; the wire response per
    ``schemas/cache/account/list-accounts-response.json`` is
    ``{accounts: [accounts]}``. Each :class:`Account` passes through
    :func:`to_wire_account` (stripping framework-internal fields and
    write-only credentials). Pre-shaped Pydantic envelopes are
    projected via ``model_dump``; see :func:`_project_sync_accounts`
    for the rationale.
    """
    if isinstance(result, list):
        accounts: list[Any] = []
        for account in result:
            if isinstance(account, _DecisioningAccount):
                accounts.append(to_wire_account(account))
            elif hasattr(account, "model_dump"):
                accounts.append(account.model_dump(mode="json"))
            else:
                accounts.append(account)
        envelope: Any = {"accounts": accounts}
    elif hasattr(result, "model_dump"):
        envelope = result.model_dump(mode="json", exclude_none=True)
    elif isinstance(result, dict):
        envelope = result
    else:
        logger.warning(
            "AccountStore.list returned an unexpected shape %s; "
            "expected list[Account] or a dict envelope. Passing "
            "through unchanged — credential scrubber may not reach "
            "nested fields.",
            type(result).__name__,
        )
        envelope = result
    return strip_credentials_from_wire_result("list_accounts", envelope)


def _build_list_accounts_filter(params: Any) -> dict[str, Any]:
    """Build the filter dict :meth:`AccountStoreList.list` expects from
    a parsed :class:`ListAccountsRequest`. Strips ``None`` so adopter
    impls can pattern-match present-vs-absent without explicit None
    checks. Mirrors the JS-side ``buildListAccountsFilter`` shape.
    """
    filter_dict: dict[str, Any] = {}
    status = getattr(params, "status", None)
    if status is not None:
        filter_dict["status"] = status.value if hasattr(status, "value") else status
    sandbox = getattr(params, "sandbox", None)
    if sandbox is not None:
        filter_dict["sandbox"] = sandbox
    pagination = getattr(params, "pagination", None)
    if pagination is not None:
        filter_dict["pagination"] = (
            pagination.model_dump(mode="json", exclude_none=True)
            if hasattr(pagination, "model_dump")
            else pagination
        )
    return filter_dict


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


def _specialism_slugs(caps: DecisioningCapabilities) -> tuple[str, ...]:
    return tuple(
        entry.value if hasattr(entry, "value") else str(entry) for entry in caps.specialisms
    )


def _effective_supported_protocols(caps: DecisioningCapabilities) -> tuple[str, ...]:
    if caps.supported_protocols is not None:
        return tuple(
            sorted(p.value if hasattr(p, "value") else str(p) for p in caps.supported_protocols)
        )
    protocols: set[str] = set()
    for slug in _specialism_slugs(caps):
        protocols.update(SPECIALISM_TO_PROTOCOLS.get(slug, frozenset()))
    return tuple(sorted(protocols))


def _validate_scoped_capabilities_static_contract(
    *,
    static_caps: DecisioningCapabilities,
    scoped_caps: DecisioningCapabilities,
) -> None:
    """Reject scoped overrides that would desynchronize discovery from dispatch."""
    static_specialisms = _specialism_slugs(static_caps)
    scoped_specialisms = _specialism_slugs(scoped_caps)
    if scoped_specialisms != static_specialisms:
        from adcp.decisioning.types import AdcpError

        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform.get_adcp_capabilities_for_request() may "
                "enrich request-scoped capability blocks, but it must not "
                "change specialisms. Tool advertisement and platform method "
                "validation are derived from the static capabilities "
                "declaration at server boot."
            ),
            recovery="terminal",
            details={
                "field": "specialisms",
                "static_specialisms": list(static_specialisms),
                "scoped_specialisms": list(scoped_specialisms),
            },
        )

    static_protocols = _effective_supported_protocols(static_caps)
    scoped_protocols = _effective_supported_protocols(scoped_caps)
    if scoped_protocols != static_protocols:
        from adcp.decisioning.types import AdcpError

        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform.get_adcp_capabilities_for_request() may "
                "enrich request-scoped capability blocks, but it must not "
                "change supported_protocols. Discovery must stay aligned with "
                "the handler's advertised tools and boot-time method "
                "validation."
            ),
            recovery="terminal",
            details={
                "field": "supported_protocols",
                "static_supported_protocols": list(static_protocols),
                "scoped_supported_protocols": list(scoped_protocols),
            },
        )


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


def _to_store_dict(value: Any) -> Any:
    """Normalize a Pydantic model OR plain dict to a JSON-compatible
    dict for the :class:`MediaBuyStore` adopter contract.

    The store Protocol uses ``Any`` typing, but the reference impl (and
    the JS-side port) expects dict-shaped payloads — ``.get("packages")``,
    ``["media_buy_id"]``, etc. Adopters writing against the Protocol
    benefit from a stable shape regardless of whether their platform
    method returned a typed Pydantic response or a hand-built dict.

    ``mode='json'`` serializes ``AnyUrl`` to ``str`` and ``datetime``
    to ISO-8601 ``str`` so the dict matches what would go over the wire
    — adopters serializing to JSON for persistence don't have to
    re-normalize. ``exclude_none=False`` preserves explicit nulls (the
    merge contract treats ``None`` as "clear this field"; dropping them
    would silently change semantics).
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False, by_alias=False)
    return value


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
        | set(_ACCOUNT_ADVERTISED_TOOLS)
        | set(_CREATIVE_ADVERTISED_TOOLS)
        | set(_SIGNALS_ADVERTISED_TOOLS)
        | set(_AUDIENCE_ADVERTISED_TOOLS)
        | set(_CATALOG_ADVERTISED_TOOLS)
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
        # Drop sync_accounts / list_accounts when the platform's
        # AccountStore doesn't expose the corresponding optional
        # Protocol method. ``sales-*`` claims union both tools in by
        # default (account roster is required by spec) but adopters who
        # haven't wired :class:`AccountStoreUpsertRequest`,
        # :class:`AccountStoreUpsert`, or :class:`AccountStoreList`
        # would otherwise advertise tools that always answer
        # OPERATION_NOT_SUPPORTED.
        #
        # Log once per (handler, dropped-tool) when the claim asked for
        # a tool the store can't serve — actionable signal for adopters
        # whose storyboard scenarios stay ``skipped (missing_tool)``
        # without the polyfill.
        accounts = getattr(self._platform, "accounts", None)
        can_sync_accounts = accounts is not None and (
            callable(getattr(accounts, "upsert_request", None))
            or callable(getattr(accounts, "upsert", None))
        )
        if "sync_accounts" in serving and not can_sync_accounts:
            serving.discard("sync_accounts")
            self._log_account_tool_dropped("sync_accounts", "upsert")
        if "list_accounts" in serving and (
            accounts is None or not callable(getattr(accounts, "list", None))
        ):
            serving.discard("list_accounts")
            self._log_account_tool_dropped("list_accounts", "list")
        return frozenset(serving)

    def _log_account_tool_dropped(self, tool_name: str, method_name: str) -> None:
        """Log once per dropped account tool, then suppress.

        Adopter signal: a ``sales-*`` specialism implies an account
        roster (``sync_accounts`` / ``list_accounts``) but the
        platform's :class:`AccountStore` doesn't expose the optional
        Protocol method. Without this log, a downstream storyboard
        scenario stuck on ``skipped (missing_tool)`` has no
        breadcrumb pointing at the missing optional Protocol.

        Dedupe state lives on the handler instance, so deployments
        wiring a per-tenant handler via :class:`LazyPlatformRouter`
        emit one log per (tenant, dropped tool) — intentional, since
        different tenants can be wired with different stores and
        adopters need the per-tenant signal.
        """
        seen: set[str] = self.__dict__.setdefault("_account_tool_drop_logged", set())
        if tool_name in seen:
            return
        seen.add(tool_name)
        logger.info(
            "PlatformHandler dropped %r from advertised_tools — "
            "platform.accounts does not implement %r. "
            "Implement the optional AccountStore%s Protocol method "
            "(see adcp.decisioning.accounts) to surface this tool on the wire.",
            tool_name,
            method_name,
            "Upsert" if method_name == "upsert" else "List",
        )

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
        brand_authorization_gate: BrandAuthorizationGate | None = None,
        config_store: ProductConfigStore | None = None,
        property_list_fetcher: PropertyListFetcher | None = None,
        media_buy_store: MediaBuyStore | None = None,
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
        self._brand_authorization_gate = brand_authorization_gate
        self._config_store = config_store
        self._property_list_fetcher = property_list_fetcher
        self._media_buy_store = media_buy_store
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

    async def _prime_auth_context(self, ctx: ToolContext) -> None:
        """Resolve the registry buyer-agent (if a registry is wired) and
        stash it on ``ctx.metadata`` so the :class:`AccountStore`
        dispatch path's :class:`ResolveContext` can read it without
        invoking :meth:`AccountStore.resolve`.

        Used by tools that operate above the per-account scope —
        ``sync_accounts`` and ``list_accounts``, which the spec uses to
        bootstrap or enumerate the roster. ``explicit``-mode stores
        raise ``ACCOUNT_NOT_FOUND`` on a no-ref resolve, which would
        deadlock the bootstrap path: every account is unknown until
        ``sync_accounts`` creates it, but ``sync_accounts`` requires an
        already-resolved account.

        The suspended / blocked / unrecognized buyer-agent gate still
        runs (via :func:`_resolve_buyer_agent`) so commercial-identity
        rejection happens before the adopter's store ever sees the
        request — same surface as :meth:`_resolve_account`.
        """
        auth_info = self._extract_auth_info(ctx)
        if self._buyer_agent_registry is not None:
            buyer_agent = await _resolve_buyer_agent(
                self._buyer_agent_registry,
                auth_info,
            )
            ctx.metadata[_BUYER_AGENT_METADATA_KEY] = buyer_agent

    async def _resolve_account(
        self,
        ref: object | None,
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
        blocked / unrecognized agents are rejected here with the
        dedicated per-agent codes per AdCP 3.1:
        suspended → ``AGENT_SUSPENDED``, blocked → ``AGENT_BLOCKED``
        (both ``recovery="terminal"``, no ``details`` payload);
        unrecognized → ``PERMISSION_DENIED`` with no ``details.scope``
        so the wire shape does not enumerate which ``agent_url``s are
        onboarded with this seller. This prevents the registry miss
        from leaking into AccountStore as ``ACCOUNT_NOT_FOUND``.
        """
        await self._prime_auth_context(ctx)
        auth_info = self._extract_auth_info(ctx)
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

        # Tier 3 brand-authorization gate (issue #350 stage 5). Opt-in
        # via ``serve(brand_authz_resolver=..., brand_identity_resolver=...)``.
        # Runs AFTER ``accounts.resolve`` so the gate has the resolved
        # :class:`Account` available — adopters source the brand identity
        # from whatever signal their account model carries (the wire
        # :class:`AccountReference` natural-key shape, ``account.metadata``,
        # an internal mapping, etc.). The gate is a no-op when no buyer
        # agent has been resolved (Tier 2 not wired) — see
        # :func:`_enforce_brand_authorization` for the rationale.
        if self._brand_authorization_gate is not None:
            buyer_agent_for_gate = cast(
                "BuyerAgent | None",
                ctx.metadata.get(_BUYER_AGENT_METADATA_KEY) if ctx.metadata else None,
            )
            await _enforce_brand_authorization(
                self._brand_authorization_gate,
                resolved,
                buyer_agent_for_gate,
            )
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
        raw = ctx.metadata.get(_AUTH_INFO_METADATA_KEY) if ctx.metadata else None
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

        Skips TaskHandoff projections — on the async (handoff) arm the
        terminal completion / failure webhook is delivered from the
        background completion path in
        :func:`adcp.decisioning.dispatch._project_handoff`
        (:func:`adcp.decisioning.webhook_emit.emit_terminal_completion_webhook`),
        fired exactly once after ``registry.complete`` / ``registry.fail``
        when the buyer registered ``push_notification_config``. This gate
        fires on the sync-success arm only; skipping the submitted
        projection here is what keeps the two paths from double-delivering.
        Mirrors the JS-side ``routeIfHandoff`` logic at
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
            # TaskHandoff projection — the background completion path in
            # _project_handoff owns the terminal webhook for this task
            # (delivered once when push config is present); skipping here
            # prevents a double-delivery.
            return
        maybe_emit_sync_completion(
            sender=self._webhook_sender,
            supervisor=self._webhook_supervisor,
            enabled=self._auto_emit_completion_webhooks,
            method_name=method_name,
            params=params,
            result=result,
        )

    def _handoff_webhook_kwargs(self) -> dict[str, Any]:
        """Webhook delivery kwargs threaded into :func:`_invoke_platform_method`
        for the async (handoff) completion path.

        The supervisor takes precedence over the bare sender (retry +
        circuit breaker), matching the resolution order
        :func:`maybe_emit_sync_completion` uses on the sync arm. When a
        request hands off AND the buyer registered
        ``push_notification_config``, the background completion path
        delivers the terminal completion / failure webhook to that
        target. The sync arm's auto-emit gate is wired separately via
        :meth:`_maybe_auto_emit_sync_completion`; both honor the same
        ``auto_emit_completion_webhooks`` flag so an adopter emitting
        manually never gets a framework double-delivery on either arm.
        """
        return {
            "webhook_target": self._webhook_supervisor or self._webhook_sender,
            "webhook_auto_emit": self._auto_emit_completion_webhooks,
        }

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
        # Pop adcp.auth_info after extraction so the AuthInfo object doesn't
        # survive into RequestContext.metadata, where it would be opaque to
        # downstream serializers. Mirrors adcp.buyer_agent on the next line.
        if tool_ctx.metadata:
            tool_ctx.metadata.pop(_AUTH_INFO_METADATA_KEY, None)
        buyer_agent = (
            tool_ctx.metadata.pop(_BUYER_AGENT_METADATA_KEY, None) if tool_ctx.metadata else None
        )
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
        params: GetAdcpCapabilitiesRequest | dict[str, Any] | None = None,
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

        Adopters who need request-scoped capability blocks (for
        example tenant-specific publisher domains or webhook-signing
        support) override
        :meth:`DecisioningPlatform.get_adcp_capabilities_for_request`.
        That hook returns a typed :class:`DecisioningCapabilities`
        override; this method remains responsible for the canonical
        wire projection.
        """
        from adcp.decisioning.types import AdcpError

        caps = self._platform.capabilities
        try:
            scoped_caps = self._platform.get_adcp_capabilities_for_request(params, context)
            if inspect.isawaitable(scoped_caps):
                scoped_caps = await scoped_caps
        except AdcpError:
            raise
        except Exception as exc:
            logger.exception(
                "Unhandled exception in platform.get_adcp_capabilities_for_request — "
                "wrapping to INTERNAL_ERROR"
            )
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    "Unhandled exception in platform.get_adcp_capabilities_for_request: "
                    f"{type(exc).__name__}: {exc}"
                ),
                recovery="terminal",
                details={
                    "caused_by": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                },
            ) from exc
        has_scoped_caps = scoped_caps is not None
        if scoped_caps is not None:
            caps = scoped_caps
        if not isinstance(caps, DecisioningCapabilities):
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "DecisioningPlatform.get_adcp_capabilities_for_request() "
                    "must return DecisioningCapabilities or None; got "
                    f"{type(caps).__name__}"
                ),
                recovery="terminal",
            )
        if has_scoped_caps:
            _validate_scoped_capabilities_static_contract(
                static_caps=self._platform.capabilities,
                scoped_caps=caps,
            )
            from adcp.decisioning.validate_idempotency import (
                validate_idempotency_wiring_for_capabilities,
            )
            from adcp.decisioning.webhook_emit import validate_webhook_signing_for_capabilities

            validate_idempotency_wiring_for_capabilities(self._platform, caps)
            validate_webhook_signing_for_capabilities(
                capabilities=caps,
                sender=self._webhook_sender,
                supervisor=self._webhook_supervisor,
            )

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
        # Route structured overrides through the response helper so its
        # supported_versions defaults stay aligned with major_versions.
        # A custom Adcp(major_versions=[2, 3]) without exact
        # supported_versions must not inherit the SDK's 3.x exact list.
        from adcp.server.responses import capabilities_response

        adcp = caps.adcp.model_dump(mode="json", exclude_none=True) if caps.adcp is not None else {}
        response = capabilities_response(
            supported_protocols,
            major_versions=adcp.get("major_versions"),
            supported_versions=adcp.get("supported_versions"),
            build_version=adcp.get("build_version"),
            idempotency=adcp.get("idempotency") or {"supported": False},
        )

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
        if caps.measurement is not None:
            response["measurement"] = caps.measurement.model_dump(mode="json", exclude_none=True)
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
        if caps.experimental_features:
            response["experimental_features"] = [
                feature.model_dump(mode="json") if hasattr(feature, "model_dump") else feature
                for feature in caps.experimental_features
            ]

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

        if has_scoped_caps:
            from adcp.decisioning.validate_capabilities import _validate_response_dict

            _validate_response_dict(response)

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
        # Guard (a): wholesale + push_notification_config is rejected
        # before the platform method runs — wholesale discovery is a
        # synchronous rate-card read with no async lifecycle. The
        # platform method is never invoked on this path.
        assert_discovery_push_consistent(params, mode_field="buying_mode")
        account = await self._resolve_account(params.account, tool_ctx)
        # Guard (c) pre-dispatch: a buyer-supplied push_notification_config
        # makes the request async up front. If the account is unresolved
        # (sentinel/empty id) the eventual task_id would be unreachable —
        # reject before invoking the platform method.
        _has_push = getattr(params, "push_notification_config", None) is not None
        if _has_push:
            assert_account_resolved_for_async(None, account_id=account.id, has_push=True)
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

            await maybe_validate_refine_proposal_refs(self._platform, params, ctx)

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

        # Terminal side-effect (persist draft proposals) threaded as an
        # on_complete hook so it fires on COMPLETION in BOTH the sync and
        # handoff paths — never at submit. On the handoff path the bg task
        # awaits the adopter's coroutine, then fires this hook with the
        # terminal GetProductsResponse before registry.complete; on the
        # sync path it fires inline with the adapter's return. This mirrors
        # create_media_buy's consumption-finalize hook (handler.py
        # create_media_buy). The hook persists the raw adapter result;
        # buyer-presentation projections (property-list, pagination,
        # fields) shape only the wire response, not the stored draft.
        captured_platform = self._platform
        captured_ctx = ctx

        async def _persist_draft_hook(get_products_result: Any) -> None:
            await maybe_persist_draft_after_get_products(
                captured_platform, get_products_result, captured_ctx
            )

        # Guard (b) pre-launch: a wholesale call that hands off is rejected
        # the instant dispatch detects the TaskHandoff — before any registry
        # row, persist-draft, background coroutine, or completion webhook.
        # Belt-and-braces post-dispatch check stays below.
        pre_handoff_reject = (
            (lambda: reject_wholesale_handoff_before_launch("buying_mode"))
            if mode == "wholesale"
            else None
        )

        coro = _invoke_platform_method(
            self._platform,
            "get_products",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            on_complete=_persist_draft_hook,
            pre_handoff_reject=pre_handoff_reject,
            **self._handoff_webhook_kwargs(),
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
        # Post-dispatch discovery guards (run on the raw result before any
        # success-shape projection). ``result`` is either a typed
        # GetProductsResponse (sync) or the framework's submitted
        # projection dict (handoff). Both reject with
        # INVALID_REQUEST / correctable.
        #   (d) hand-rolled submitted: adopter built {'status':'submitted'}
        #       by hand instead of ctx.handoff_to_task — no registry row.
        #   (b) wholesale + handoff: wholesale MUST be synchronous. The
        #       pre-launch arm (pre_handoff_reject above) already rejected
        #       it before any task/draft/webhook side effect; this is the
        #       belt-and-braces post-dispatch check.
        # Guard (c) — async + unresolved account — is NOT re-checked here.
        # The push arm is rejected pre-dispatch above (correctable
        # field='account'); the no-push handoff arm is owned by
        # compose_caller_identity, which fails closed inside _build_ctx
        # (terminal INVALID_REQUEST) BEFORE the platform method runs, so no
        # task row is ever minted against an unresolved account. A
        # post-dispatch (c) re-check would be unreachable: reaching this
        # line means _build_ctx succeeded, i.e. the account resolved.
        reject_hand_rolled_submitted(result)
        reject_wholesale_handoff(result, mode=mode, mode_field="buying_mode")
        # Handoff path: the submitted envelope is returned verbatim, the
        # same as create_media_buy. Success-shape projections (property
        # list, pagination, fields, draft persist) apply only to the sync
        # success arm. When the buyer registered push_notification_config,
        # the terminal completion / failure webhook is delivered from the
        # background completion path in _project_handoff exactly once; with
        # no push config the buyer polls tasks/get. Either way nothing fires
        # here at submit time.
        if isinstance(result, dict) and result.get("status") == "submitted":
            return cast("GetProductsResponse", result)
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
        # Draft proposals are persisted by the _persist_draft_hook
        # on_complete seam (threaded above) so the same side-effect fires
        # on both the sync and handoff completion paths. The hook ran
        # inline on the sync path before this point.
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

        # MediaBuyStore persist hook — fires on the same on_complete seam
        # so both sync and HITL completions persist the per-package
        # ``targeting_overlay`` for later echo on ``get_media_buys``.
        # Chains with the proposal hook (if any) so both side effects run.
        if self._media_buy_store is not None:
            prior_on_complete = on_complete
            captured_store = self._media_buy_store
            captured_account_id = account.id

            async def _persist_overlay_hook(create_result: Any) -> None:
                if prior_on_complete is not None:
                    await prior_on_complete(create_result)
                await captured_store.persist_from_create(
                    captured_account_id,
                    _to_store_dict(params),
                    _to_store_dict(create_result),
                )

            on_complete = _persist_overlay_hook

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
            **self._handoff_webhook_kwargs(),
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

        # MediaBuyStore merge hook — fires on the on_complete seam so the
        # patch is recorded for both sync and HITL completions. Spec:
        # ``targeting_overlay`` is echoed from the most recent
        # ``create_media_buy`` OR ``update_media_buy`` per the
        # ``get-media-buys-response`` schema.
        on_complete: Callable[[Any], Awaitable[None]] | None = None
        if self._media_buy_store is not None:
            captured_store = self._media_buy_store
            captured_account_id = account.id
            captured_media_buy_id = params.media_buy_id

            async def _merge_overlay_hook(_update_result: Any) -> None:
                await captured_store.merge_from_update(
                    captured_account_id,
                    captured_media_buy_id,
                    _to_store_dict(params),
                )

            on_complete = _merge_overlay_hook

        result = await _invoke_platform_method(
            self._platform,
            "update_media_buy",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            arg_projector={"media_buy_id": params.media_buy_id, "patch": params},
            on_complete=on_complete,
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
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
        result = await _invoke_platform_method(
            self._platform,
            "get_media_buys",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        # MediaBuyStore backfill — fill in ``packages[].targeting_overlay``
        # from the persisted store for sellers claiming
        # ``property-lists`` / ``collection-lists``. Packages the
        # platform already echoed are left untouched (the in-memory
        # reference impl checks ``pkg.get("targeting_overlay") is not None``
        # before injecting). Normalize to dict before the store sees it
        # so the adopter contract is consistent regardless of whether the
        # platform returned a typed Pydantic response or a dict.
        if self._media_buy_store is not None:
            result = await self._media_buy_store.backfill(account.id, _to_store_dict(result))
        return cast("GetMediaBuysResponse", result)

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

    # ----- Account roster (AccountStoreUpsert / AccountStoreList) -----

    def _make_resolve_context(self, tool_ctx: ToolContext, tool_name: str) -> ResolveContext:
        """Build a :class:`ResolveContext` for the account-store dispatch path.
        Carries the caller's verified :class:`AuthInfo` and resolved
        :class:`BuyerAgent` (when a registry is wired) so adopter
        ``upsert`` / ``list`` impls can implement principal-keyed gates
        (e.g. spec ``BILLING_NOT_PERMITTED_FOR_AGENT``) off the same
        canonical context :meth:`AccountStore.resolve` already reads.
        """
        auth_info = self._extract_auth_info(tool_ctx)
        buyer_agent = (
            tool_ctx.metadata.get(_BUYER_AGENT_METADATA_KEY) if tool_ctx.metadata else None
        )
        return ResolveContext(
            auth_info=auth_info,
            tool_name=tool_name,
            agent=buyer_agent,
        )

    async def sync_accounts(  # type: ignore[override]
        self,
        params: SyncAccountsRequest,
        context: ToolContext | None = None,
    ) -> SyncAccountsResponse | NotImplementedResponse:
        """Route ``sync_accounts`` through the account-store upsert surface.

        ``sync_accounts`` lives on the AccountStore Protocol surface,
        not on per-specialism platform methods —
        :data:`adcp.decisioning.platform_router._ACCOUNT_STORE_METHODS`
        already excludes it from per-tenant delegation. Surface
        ``OPERATION_NOT_SUPPORTED`` (via :meth:`_not_supported`) when
        the store doesn't expose either the optional full-request
        ``upsert_request`` hook or the legacy
        :class:`AccountStoreUpsert` Protocol — distinct from
        ``AttributeError`` (which is what an unguarded
        ``getattr().()`` would produce).

        ``ResolveContext`` carries the caller's :class:`AuthInfo` and
        resolved :class:`BuyerAgent` so adopter impls implementing
        principal-keyed gates (e.g. spec
        ``BILLING_NOT_PERMITTED_FOR_AGENT``) read the principal off the
        canonical context — same threading
        :meth:`AccountStore.resolve` already uses.
        """
        upsert_request = getattr(self._platform.accounts, "upsert_request", None)
        upsert = getattr(self._platform.accounts, "upsert", None)
        if not callable(upsert_request) and not callable(upsert):
            return self._not_supported("sync_accounts")
        tool_ctx = context or ToolContext()
        # Prime auth-context only — DON'T call ``_resolve_account``.
        # ``sync_accounts`` is the bootstrap tool buyers use to
        # populate an explicit-mode store; routing through the store's
        # no-ref ``resolve()`` would deadlock the bootstrap path
        # (``ACCOUNT_NOT_FOUND`` until the account exists, but the
        # account exists only after this call succeeds).
        await self._prime_auth_context(tool_ctx)
        resolve_ctx = self._make_resolve_context(tool_ctx, "sync_accounts")
        if callable(upsert_request):
            result = _call_with_optional_ctx(upsert_request, params, ctx=resolve_ctx)
        else:
            assert callable(upsert)
            refs = list(getattr(params, "accounts", []) or [])
            result = _call_with_optional_ctx(upsert, refs, ctx=resolve_ctx)
        if inspect.isawaitable(result):
            result = await result
        return cast("SyncAccountsResponse", _project_sync_accounts(result))

    async def list_accounts(  # type: ignore[override]
        self,
        params: ListAccountsRequest,
        context: ToolContext | None = None,
    ) -> ListAccountsResponse | NotImplementedResponse:
        """Route ``list_accounts`` through :meth:`AccountStore.list`.

        See :meth:`sync_accounts` for the OPERATION_NOT_SUPPORTED gate
        and ``ResolveContext`` threading rationale.
        """
        listing = getattr(self._platform.accounts, "list", None)
        if not callable(listing):
            return self._not_supported("list_accounts")
        tool_ctx = context or ToolContext()
        # Prime auth-context only — see :meth:`sync_accounts` for the
        # explicit-mode-bootstrap rationale. ``list_accounts`` is also
        # used to enumerate accounts in stores keyed solely by auth
        # principal; a no-ref ``resolve()`` is the wrong shape there.
        await self._prime_auth_context(tool_ctx)
        resolve_ctx = self._make_resolve_context(tool_ctx, "list_accounts")
        filter_dict = _build_list_accounts_filter(params)
        result = _call_with_optional_ctx(listing, filter_dict, ctx=resolve_ctx)
        if inspect.isawaitable(result):
            result = await result
        return cast("ListAccountsResponse", _project_list_accounts(result))

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
            **self._handoff_webhook_kwargs(),
        )
        self._maybe_auto_emit_sync_completion("get_creative_delivery", params, result)
        return cast("GetCreativeDeliveryResponse", result)

    async def validate_input(  # type: ignore[override]
        self,
        params: ValidateInputRequest,
        context: ToolContext | None = None,
    ) -> ValidateInputResponse:
        """Optional creative preflight validation for beta 3 inputs."""
        self._require_platform_method("validate_input")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ValidateInputResponse",
            await _invoke_platform_method(
                self._platform,
                "validate_input",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    # ----- SignalsPlatform -----

    async def get_signals(  # type: ignore[override]
        self,
        params: GetSignalsRequest,
        context: ToolContext | None = None,
    ) -> GetSignalsResponse:
        """Catalog discovery for signal-marketplace / signal-owned.

        Synchronous by default; ``discovery_mode='brief'`` MAY hand off to
        a background task (submitted / working arms — no input_required).
        ``discovery_mode='wholesale'`` MUST stay synchronous.
        """
        tool_ctx = context or ToolContext()
        # Guard (a): wholesale + push_notification_config is rejected
        # before the platform method runs. Wholesale signal discovery is a
        # synchronous catalog read with no async lifecycle.
        assert_discovery_push_consistent(params, mode_field="discovery_mode")
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        # Guard (c) pre-dispatch: push_notification_config makes the request
        # async up front; reject against an unresolved account before
        # _build_ctx (whose compose_caller_identity would otherwise raise a
        # less-specific terminal error on the sentinel id) and before the
        # platform method runs.
        _has_push = getattr(params, "push_notification_config", None) is not None
        if _has_push:
            assert_account_resolved_for_async(None, account_id=account.id, has_push=True)
        ctx = self._build_ctx(tool_ctx, account)
        mode = getattr(params, "discovery_mode", None)
        if mode is None:
            mode_str = None
        else:
            mode_str = mode.value if hasattr(mode, "value") else str(mode)
        # Guard (b) pre-launch: a wholesale call that hands off is rejected
        # before any registry row / background coroutine / completion
        # webhook. Belt-and-braces post-dispatch check stays below.
        pre_handoff_reject = (
            (lambda: reject_wholesale_handoff_before_launch("discovery_mode"))
            if mode_str == "wholesale"
            else None
        )
        result = await _invoke_platform_method(
            self._platform,
            "get_signals",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            pre_handoff_reject=pre_handoff_reject,
            **self._handoff_webhook_kwargs(),
        )
        # Post-dispatch discovery guards (mirror get_products):
        #   (d) hand-rolled submitted, (b) wholesale + handoff. Both
        #       INVALID_REQUEST / correctable. Guard (b) here is the
        #       belt-and-braces check; the pre-launch arm above already
        #       rejected a wholesale handoff before any side effect.
        # Guard (c) — async + unresolved account — is NOT re-checked here:
        # the push arm is rejected pre-dispatch above (correctable
        # field='account'), and the no-push handoff arm is owned by
        # compose_caller_identity, which fails closed terminally inside
        # _build_ctx before the platform method runs. Reaching this line
        # means the account resolved, so a post-dispatch (c) check is
        # unreachable.
        reject_hand_rolled_submitted(result)
        reject_wholesale_handoff(result, mode=mode_str, mode_field="discovery_mode")
        # On the handoff arm the terminal completion / failure webhook is
        # delivered from _project_handoff's background path when the buyer
        # registered push_notification_config; the sync auto-emit gate below
        # skips the {task_id, status} submitted projection so there is no
        # double-delivery.
        self._maybe_auto_emit_sync_completion("get_signals", params, result)
        return cast("GetSignalsResponse", result)

    async def activate_signal(  # type: ignore[override]
        self,
        params: ActivateSignalRequest,
        context: ToolContext | None = None,
    ) -> ActivateSignalSuccessResponse:
        """Provision a signal onto destination platforms."""
        self._require_platform_method("activate_signal")
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
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
        )
        projected = _project_sync_audiences(result)
        self._maybe_auto_emit_sync_completion("sync_audiences", params, projected)
        return cast("SyncAudiencesSuccessResponse", projected)

    async def sync_catalogs(  # type: ignore[override]
        self,
        params: SyncCatalogsRequest,
        context: ToolContext | None = None,
    ) -> SyncCatalogsSuccessResponse:
        """Sync product catalogs with the platform.

        The platform method receives the full :class:`SyncCatalogsRequest`
        so adopters can inspect ``req.catalogs``, ``req.delete_missing``,
        ``req.dry_run``, and ``req.validation_mode``. Discovery mode
        (``req.catalogs is None``) returns existing synced catalogs without
        modification — the platform method must handle ``req.catalogs is None``
        as a read-only path.

        Two return arms per the per-specialism Protocol: a list of
        :class:`SyncCatalogResult` rows (ergonomic form) or a fully-shaped
        :class:`SyncCatalogsSuccessResponse`. The shim projects the list arm
        to the wire envelope ``{catalogs: [...]}`` so adopters can return
        the ergonomic form.
        """
        self._require_platform_method("sync_catalogs")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(getattr(params, "account", None), tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "sync_catalogs",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            **self._handoff_webhook_kwargs(),
        )
        projected = _project_sync_catalogs(result)
        self._maybe_auto_emit_sync_completion("sync_catalogs", params, projected)
        return cast("SyncCatalogsSuccessResponse", projected)

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
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
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

    async def verify_brand_claim(  # type: ignore[override]
        self,
        params: VerifyBrandClaimRequest,
        context: ToolContext | None = None,
    ) -> VerifyBrandClaimResponse:
        """Optional brand claim verification for beta 3 brand agents."""
        self._require_platform_method("verify_brand_claim")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "VerifyBrandClaimResponse",
            await _invoke_platform_method(
                self._platform,
                "verify_brand_claim",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def verify_brand_claims(  # type: ignore[override]
        self,
        params: VerifyBrandClaimsRequest,
        context: ToolContext | None = None,
    ) -> VerifyBrandClaimsResponseBulk:
        """Optional bulk brand claim verification for beta 3 brand agents."""
        self._require_platform_method("verify_brand_claims")
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "VerifyBrandClaimsResponseBulk",
            await _invoke_platform_method(
                self._platform,
                "verify_brand_claims",
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
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
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
            **self._handoff_webhook_kwargs(),
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
