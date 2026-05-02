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
from typing import TYPE_CHECKING, Any, ClassVar, cast

from adcp.decisioning.context import AuthInfo
from adcp.decisioning.dispatch import (
    _build_request_context,
    _invoke_platform_method,
)
from adcp.decisioning.webhook_emit import maybe_emit_sync_completion
from adcp.server.base import ADCPHandler, ToolContext

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
    CreateMediaBuySuccessResponse,
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
      auth) → ``REQUEST_AUTH_UNRECOGNIZED_AGENT``. Adopters running
      a registry have implicitly opted out of unauthenticated traffic.

    :raises AdcpError: ``REQUEST_AUTH_UNRECOGNIZED_AGENT`` (registry
        miss / no credential), ``AGENT_SUSPENDED`` (status=suspended),
        or ``AGENT_BLOCKED`` (status=blocked). All ``recovery=terminal``
        — the buyer cannot retry their way out of a commercial-state
        rejection.
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

    if agent is None:
        raise AdcpError(
            "REQUEST_AUTH_UNRECOGNIZED_AGENT",
            message=(
                "BuyerAgentRegistry returned no match for the request's "
                "credential. The registry is the seller's commercial "
                "allowlist — adopters reject auth that's cryptographically "
                "valid but not commercially recognized (no onboarding row, "
                "revoked, or wrong credential kind for the registry's "
                "posture). Check that the agent has been onboarded into the "
                "registry's backing store."
            ),
            recovery="terminal",
        )

    if agent.status == "active":
        return agent
    if agent.status == "suspended":
        raise AdcpError(
            "AGENT_SUSPENDED",
            message=(
                f"Buyer agent {agent.agent_url!r} is suspended. Suspension "
                "is a temporary commercial pause (credit, compliance review, "
                "ops hold) — the seller restores it via their durable "
                "store. Retry once the seller restores the agent; escalate "
                "through the account contact if the pause is unexpected."
            ),
            recovery="transient",
            details={"agent_url": agent.agent_url, "status": agent.status},
        )
    if agent.status == "blocked":
        raise AdcpError(
            "AGENT_BLOCKED",
            message=(
                f"Buyer agent {agent.agent_url!r} is blocked. Blocked is "
                "a hard cutoff (terms violation, fraud, enforcement) — "
                "no retry path. Buyer must contact the seller directly."
            ),
            recovery="terminal",
            details={"agent_url": agent.agent_url, "status": agent.status},
        )
    # Default-reject any non-active status the framework doesn't
    # recognize (typo, future enum value, adopter-custom string). A
    # silent fall-through to "active" would leak commercial state
    # past the gate.
    raise AdcpError(
        "REQUEST_AUTH_UNRECOGNIZED_AGENT",
        message=(
            f"Buyer agent {agent.agent_url!r} has unrecognized status "
            f"{agent.status!r}. The framework only treats ``active`` as "
            "live; ``suspended`` / ``blocked`` raise their own structured "
            "errors. Unknown statuses are rejected by default to prevent "
            "silent fall-through past the commercial-identity gate."
        ),
        recovery="terminal",
        details={"agent_url": agent.agent_url, "status": agent.status},
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
        for slug in claimed:
            tools = SPECIALISM_TO_ADVERTISED_TOOLS.get(slug)
            if tools is not None:
                serving |= set(tools)
        return frozenset(serving)

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
        blocked agents are rejected here with structured error codes
        — buyers see ``AGENT_SUSPENDED`` / ``AGENT_BLOCKED`` /
        ``REQUEST_AUTH_UNRECOGNIZED_AGENT`` instead of the registry
        miss leaking into the AccountStore as ``ACCOUNT_NOT_FOUND``.
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
            return cast("Account[Any]", await result)
        return cast("Account[Any]", result)

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

    # ----- Sales tools -----

    async def get_products(  # type: ignore[override]
        self,
        params: GetProductsRequest,
        context: ToolContext | None = None,
    ) -> GetProductsResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetProductsResponse",
            await _invoke_platform_method(
                self._platform,
                "get_products",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def create_media_buy(  # type: ignore[override]
        self,
        params: CreateMediaBuyRequest,
        context: ToolContext | None = None,
    ) -> CreateMediaBuySuccessResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "create_media_buy",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("create_media_buy", params, result)
        return cast("CreateMediaBuySuccessResponse", result)

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
