# ruff: noqa: E501
"""Base classes for ADCP server implementations.

Defines the ADCPHandler base class and utilities for building ADCP-compliant agents.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

# Request types are imported at module scope (NOT under TYPE_CHECKING)
# so that ``typing.get_type_hints(method)`` resolves every ADCPHandler
# baseline method's ``params`` annotation at runtime. The dispatcher's
# ``adcp.server.mcp_tools._resolve_params_pydantic_model`` walks these
# hints to deserialise wire-shape dicts into typed Pydantic models;
# without runtime visibility, ``get_type_hints`` raises ``NameError``,
# the resolver swallows the exception (warning + dict-fallback), and
# the framework's own ``get_adcp_capabilities`` warns on every cold
# boot about an annotation NO ADOPTER CAN FIX (Emma cross-cutting P1
# from the post-#340 matrix run — sales/signals/stability all flagged
# this).
#
# Same root cause as PR #338's handler.py fix; we missed base.py
# because the framework methods inherit ``not_supported`` defaults
# and the wire-dispatch failure mode wasn't visible until the
# resolver-warning bump in #338 surfaced it as console noise.
from adcp.types import (
    AcquireRightsRequest,
    ActivateSignalRequest,
    BuildCreativeRequest,
    CalibrateContentRequest,
    CheckGovernanceRequest,
    ComplyTestControllerRequest,
    ContextMatchRequest,
    CreateCollectionListRequest,
    CreateContentStandardsRequest,
    CreateMediaBuyRequest,
    CreatePropertyListRequest,
    DeleteCollectionListRequest,
    DeletePropertyListRequest,
    Error,
    GetAccountFinancialsRequest,
    GetAdcpCapabilitiesRequest,
    GetBrandIdentityRequest,
    GetCollectionListRequest,
    GetContentStandardsRequest,
    GetCreativeDeliveryRequest,
    GetCreativeFeaturesRequest,
    GetMediaBuyArtifactsRequest,
    GetMediaBuyDeliveryRequest,
    GetMediaBuysRequest,
    GetPlanAuditLogsRequest,
    GetProductsRequest,
    GetPropertyListRequest,
    GetRightsRequest,
    GetSignalsRequest,
    IdentityMatchRequest,
    ListAccountsRequest,
    ListCollectionListsRequest,
    ListContentStandardsRequest,
    ListCreativeFormatsRequest,
    ListCreativesRequest,
    ListPropertyListsRequest,
    LogEventRequest,
    PreviewCreativeRequest,
    ProvidePerformanceFeedbackRequest,
    ReportPlanOutcomeRequest,
    ReportUsageRequest,
    SiGetOfferingRequest,
    SiInitiateSessionRequest,
    SiSendMessageRequest,
    SiTerminateSessionRequest,
    SyncAccountsRequest,
    SyncAudiencesRequest,
    SyncCatalogsRequest,
    SyncCreativesRequest,
    SyncEventSourcesRequest,
    SyncGovernanceRequest,
    SyncPlansRequest,
    UpdateCollectionListRequest,
    UpdateContentStandardsRequest,
    UpdateMediaBuyRequest,
    UpdatePropertyListRequest,
    UpdateRightsRequest,
    ValidateContentDeliveryRequest,
)


@dataclass
class ToolContext:
    """Context passed to tool handlers.

    Contains metadata about the current request that may be useful
    for logging, authorization, or other cross-cutting concerns.

    Subclassing is supported. Multi-tenant agents commonly define a
    subclass carrying typed tenant + adapter fields (see
    ``docs/handler-authoring.md``) and populate it from a
    ``context_factory`` passed to :func:`create_mcp_server`.

    :param caller_identity: The authenticated principal making the request.
        **MUST** be a stable, globally-unique identifier within the seller's
        tenant — never an email, display name, or any other mutable handle.
        The server-side idempotency middleware keys its cache by
        ``(caller_identity, idempotency_key)`` — reuse of the same string for
        two distinct principals (e.g. email reuse after account deletion)
        causes cross-principal replay (confidentiality leak). Populated by
        the transport layer (A2A: ``ServerCallContext.user.user_name``; MCP:
        seller's FastMCP auth middleware).
    :param tenant_id: Multi-tenant agents may populate this with the tenant
        the request is scoped to. Typed as a first-class field so
        multi-tenant handlers don't have to smuggle it through ``metadata``.
        The server-side idempotency middleware composes the cache scope key
        from ``(tenant_id, caller_identity)`` when ``tenant_id`` is set —
        sellers whose principal IDs are only unique *within* a tenant (Okta
        group-scoped, SCIM per-tenant, seller-internal employee IDs) **MUST**
        populate this so cross-tenant response replay can't happen. When
        unset, the scope collapses to ``caller_identity`` alone (safe for
        single-tenant deployments).
    :param metadata: Open extension point for transport-specific or
        agent-specific fields (e.g. adapter instance handles, request
        headers, testing hooks). Downstream agents may subclass
        :class:`ToolContext` for typed fields; ``metadata`` is the escape
        hatch when subclassing isn't worth it.
    """

    request_id: str | None = None
    caller_identity: str | None = None
    tenant_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountAwareToolContext(ToolContext):
    """ToolContext subclass carrying a resolved account scope.

    AdCP is account-aware: many operations accept an ``account`` field
    (:class:`~adcp.types.AccountReference`) that the seller resolves to
    a concrete account before executing the request. Handlers that need
    ``account_id`` throughout their business logic shouldn't have to
    re-derive it on every call — this subclass carries the resolved
    result on the context itself.

    The typical flow::

        class MyAgent(ADCPHandler[AccountAwareToolContext]):
            async def get_products(self, params, context=None):
                err = await resolve_account_into_context(
                    params, context, my_resolver,
                )
                if err:
                    return err  # ACCOUNT_NOT_FOUND / SUSPENDED / etc.
                # context.account_id is now populated
                return products_response(self.catalog.for_account(context.account_id))

    Sellers whose account scope is fixed by the authenticated principal
    (e.g. per-tenant API keys that map 1:1 to an account) can populate
    ``account_id`` directly in their ``context_factory`` and skip the
    per-call resolution entirely.

    :param account_id: The resolved, stable account identifier. Safe to
        use as a cache key, audit log field, or authorization scope.
    :param account: The resolver's opaque account object — whatever the
        seller's :func:`resolve_account` resolver returned. Typed as
        ``Any`` so sellers aren't forced to match the SDK's shape.
    """

    account_id: str | None = None
    account: Any | None = None


class NotImplementedResponse(BaseModel):
    """Standard response for operations not supported by this handler."""

    supported: bool = False
    reason: str = "This operation is not supported by this agent"
    error: Error | None = None


def not_supported(
    reason: str = "This operation is not supported by this agent",
) -> NotImplementedResponse:
    """Create a standard 'not supported' response.

    Use this to return from operations that your agent does not implement.

    Args:
        reason: Human-readable explanation of why the operation is not supported

    Returns:
        NotImplementedResponse with supported=False
    """
    return NotImplementedResponse(
        supported=False,
        reason=reason,
        error=Error(
            code="NOT_SUPPORTED",
            message=reason,
        ),
    )


TContext = TypeVar("TContext", bound="ToolContext")
"""TypeVar bound to :class:`ToolContext` for parameterising
:class:`ADCPHandler` over a caller-defined context subclass.

Multi-tenant agents typically subclass :class:`ToolContext` to carry
typed tenant/adapter/testing fields the base doesn't name. Declaring
``class MyAgent(ADCPHandler[MyContext])`` makes that subclass visible to
every handler method signature — callers get the typed subclass on the
``context`` parameter without casting::

    @dataclass
    class MyContext(ToolContext):
        adapter: MyPlatformAdapter

    class MyAgent(ADCPHandler[MyContext]):
        async def get_products(self, params, context: MyContext | None = None):
            if context is not None:
                adapter = context.adapter  # typed, no cast

Handlers that don't subclass ``ToolContext`` can still write
``class MyAgent(ADCPHandler)`` — unparameterised Generic resolves to
``ADCPHandler[ToolContext]`` at runtime (the ``TypeVar`` bound), so
existing subclasses keep working without edits.
"""


class ADCPHandler(ABC, Generic[TContext]):
    """Base class for ADCP operation handlers.

    Subclass this to implement ADCP operations. All operations have default
    implementations that return 'not supported', allowing you to implement
    only the operations your agent supports.

    Parameterise over a :class:`ToolContext` subclass — ``class MyAgent(ADCPHandler[MyContext])``
    — to get typed ``context`` arguments on every method signature. See
    :data:`TContext` for the pattern.

    For protocol-specific handlers, use:
    - ContentStandardsHandler: For content standards agents
    - SponsoredIntelligenceHandler: For sponsored intelligence agents
    - GovernanceHandler: For governance agents

    **Tool advertisement** (`advertised_tools` class attribute):

    A subclass that introduces a new specialism — i.e., a custom base
    that needs its own ``tools/list`` filter rather than inheriting one
    from a built-in handler — declares the tool set on the class body::

        class PlatformHandler(ADCPHandler):
            advertised_tools: ClassVar[set[str]] = {
                "get_products",
                "create_media_buy",
                ...
            }

    The framework registers ``PlatformHandler -> advertised_tools`` with
    :func:`adcp.server.mcp_tools.register_handler_tools` at class
    definition time. Subclasses that DON'T introduce a new specialism
    (a custom ``MyContentAgent(ContentStandardsHandler)``, for example)
    inherit their parent's tool set unchanged — no class attr needed.

    Hand-written equivalent (no ``advertised_tools`` declaration)::

        from adcp.server.mcp_tools import register_handler_tools
        register_handler_tools("PlatformHandler", {...})

    Either path is fine; codegen targets emit the class attribute so the
    declaration sits next to the class definition.
    """

    _agent_type: str = "this agent"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-register subclass-declared tool advertisement.

        Reads ``cls.__dict__["advertised_tools"]`` (subclass-defined-only
        — inherited values don't trigger re-registration) and routes
        through :func:`adcp.server.mcp_tools.register_handler_tools`.
        Only fires when the subclass declares the attribute on its own
        class body; intermediate subclasses (multi-level hierarchy)
        register at the level that introduces the attribute.

        The lazy import avoids a base.py ↔ mcp_tools.py circular —
        mcp_tools imports ADCPHandler at module load, so register is
        looked up only when a subclass is actually being created.
        """
        super().__init_subclass__(**kwargs)
        if "advertised_tools" in cls.__dict__:
            from adcp.server.mcp_tools import register_handler_tools

            register_handler_tools(cls.__name__, cls.__dict__["advertised_tools"])

    def _not_supported(self, operation: str) -> NotImplementedResponse:
        """Create a not-supported response that includes the agent type."""
        return not_supported(f"{operation} is not supported by {self._agent_type}")

    # ========================================================================
    # Core Catalog Operations
    # ========================================================================

    async def get_products(
        self, params: GetProductsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get advertising products.

        Override this to provide product catalog functionality.
        """
        return self._not_supported("get_products")

    async def list_creative_formats(
        self,
        params: ListCreativeFormatsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """List supported creative formats.

        Override this to provide creative format information.
        """
        return self._not_supported("list_creative_formats")

    # ========================================================================
    # Creative Operations
    # ========================================================================

    async def sync_creatives(
        self, params: SyncCreativesRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Sync creatives.

        Override this to handle creative synchronization.
        """
        return self._not_supported("sync_creatives")

    async def list_creatives(
        self, params: ListCreativesRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """List creatives.

        Override this to list synced creatives.
        """
        return self._not_supported("list_creatives")

    async def build_creative(
        self, params: BuildCreativeRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Build a creative.

        Override this to build creatives from assets.
        """
        return self._not_supported("build_creative")

    async def preview_creative(
        self, params: PreviewCreativeRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Preview a creative rendering.

        Override this to provide creative preview functionality.
        """
        return self._not_supported("preview_creative")

    async def get_creative_delivery(
        self,
        params: GetCreativeDeliveryRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Get creative delivery metrics.

        Override this to provide functionality.
        """
        return self._not_supported("get_creative_delivery")

    # ========================================================================
    # Media Buy Operations
    # ========================================================================

    async def create_media_buy(
        self, params: CreateMediaBuyRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Create a media buy.

        Override this to handle media buy creation.
        """
        return self._not_supported("create_media_buy")

    async def update_media_buy(
        self, params: UpdateMediaBuyRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Update a media buy.

        Override this to handle media buy updates.
        """
        return self._not_supported("update_media_buy")

    async def get_media_buy_delivery(
        self,
        params: GetMediaBuyDeliveryRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Get media buy delivery metrics.

        Override this to provide delivery reporting.
        """
        return self._not_supported("get_media_buy_delivery")

    async def get_media_buys(
        self, params: GetMediaBuysRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get media buys with status and optional delivery snapshots.

        Override this to provide media buy listing functionality.
        """
        return self._not_supported("get_media_buys")

    # ========================================================================
    # Signal Operations
    # ========================================================================

    async def get_signals(
        self, params: GetSignalsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get available signals.

        Override this to provide signal catalog.
        """
        return self._not_supported("get_signals")

    async def activate_signal(
        self, params: ActivateSignalRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Activate a signal.

        Override this to handle signal activation.
        """
        return self._not_supported("activate_signal")

    # ========================================================================
    # Feedback Operations
    # ========================================================================

    async def provide_performance_feedback(
        self,
        params: ProvidePerformanceFeedbackRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Provide performance feedback.

        Override this to handle performance feedback ingestion.
        """
        return self._not_supported("provide_performance_feedback")

    # ========================================================================
    # Account Operations
    # ========================================================================

    async def list_accounts(
        self, params: ListAccountsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """List accounts.

        Override this to provide functionality.
        """
        return self._not_supported("list_accounts")

    async def sync_accounts(
        self, params: SyncAccountsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Sync accounts.

        Override this to provide functionality.
        """
        return self._not_supported("sync_accounts")

    async def get_account_financials(
        self,
        params: GetAccountFinancialsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Get account financials.

        Override this to provide account financial reporting.
        """
        return self._not_supported("get_account_financials")

    async def report_usage(
        self, params: ReportUsageRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Report account usage.

        Override this to ingest account usage.
        """
        return self._not_supported("report_usage")

    # ========================================================================
    # Event Operations
    # ========================================================================

    async def log_event(
        self, params: LogEventRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Log event.

        Override this to provide functionality.
        """
        return self._not_supported("log_event")

    async def sync_event_sources(
        self, params: SyncEventSourcesRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Sync event sources.

        Override this to provide functionality.
        """
        return self._not_supported("sync_event_sources")

    async def sync_audiences(
        self, params: SyncAudiencesRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Sync audiences.

        Override this to provide audience synchronization.
        """
        return self._not_supported("sync_audiences")

    async def sync_governance(
        self, params: SyncGovernanceRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Sync governance agents for accounts.

        Override this to handle governance agent registration.
        """
        return self._not_supported("sync_governance")

    async def sync_catalogs(
        self, params: SyncCatalogsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Sync catalogs.

        Override this to provide catalog synchronization.
        """
        return self._not_supported("sync_catalogs")

    # ========================================================================
    # V3 Protocol Discovery
    # ========================================================================

    async def get_adcp_capabilities(
        self,
        params: GetAdcpCapabilitiesRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Get ADCP capabilities.

        Override this to advertise your agent's capabilities.
        """
        return self._not_supported("get_adcp_capabilities")

    # ========================================================================
    # V3 Content Standards Operations
    # ========================================================================

    async def create_content_standards(
        self,
        params: CreateContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Create content standards configuration.

        Override this in ContentStandardsHandler subclasses.
        """
        return self._not_supported("create_content_standards")

    async def get_content_standards(
        self,
        params: GetContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Get content standards configuration.

        Override this in ContentStandardsHandler subclasses.
        """
        return self._not_supported("get_content_standards")

    async def list_content_standards(
        self,
        params: ListContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """List content standards configurations.

        Override this in ContentStandardsHandler subclasses.
        """
        return self._not_supported("list_content_standards")

    async def update_content_standards(
        self,
        params: UpdateContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Update content standards configuration.

        Override this in ContentStandardsHandler subclasses.
        """
        return self._not_supported("update_content_standards")

    async def calibrate_content(
        self, params: CalibrateContentRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Calibrate content against standards.

        Override this in ContentStandardsHandler subclasses.
        """
        return self._not_supported("calibrate_content")

    async def validate_content_delivery(
        self,
        params: ValidateContentDeliveryRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Validate content delivery against standards.

        Override this in ContentStandardsHandler subclasses.
        """
        return self._not_supported("validate_content_delivery")

    async def get_media_buy_artifacts(
        self,
        params: GetMediaBuyArtifactsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Get artifacts associated with a media buy.

        Override this in ContentStandardsHandler subclasses.
        """
        return self._not_supported("get_media_buy_artifacts")

    # ========================================================================
    # V3 Sponsored Intelligence Operations
    # ========================================================================

    async def si_get_offering(
        self, params: SiGetOfferingRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get sponsored intelligence offering.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return self._not_supported("si_get_offering")

    async def si_initiate_session(
        self, params: SiInitiateSessionRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Initiate sponsored intelligence session.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return self._not_supported("si_initiate_session")

    async def si_send_message(
        self, params: SiSendMessageRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Send message in sponsored intelligence session.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return self._not_supported("si_send_message")

    async def si_terminate_session(
        self, params: SiTerminateSessionRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Terminate sponsored intelligence session.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return self._not_supported("si_terminate_session")

    # ========================================================================
    # V3 Governance Operations
    # ========================================================================

    async def get_creative_features(
        self,
        params: GetCreativeFeaturesRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Evaluate governance features for a creative.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("get_creative_features")

    async def sync_plans(
        self, params: SyncPlansRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Sync campaign governance plans.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("sync_plans")

    async def check_governance(
        self, params: CheckGovernanceRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Check an action against campaign governance.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("check_governance")

    async def report_plan_outcome(
        self, params: ReportPlanOutcomeRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Report the outcome of a governed action.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("report_plan_outcome")

    async def get_plan_audit_logs(
        self, params: GetPlanAuditLogsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Retrieve governance audit logs for plans.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("get_plan_audit_logs")

    async def create_property_list(
        self, params: CreatePropertyListRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Create a property list for governance filtering.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("create_property_list")

    async def get_property_list(
        self, params: GetPropertyListRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get a property list with optional resolution.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("get_property_list")

    async def list_property_lists(
        self, params: ListPropertyListsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """List property lists.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("list_property_lists")

    async def update_property_list(
        self, params: UpdatePropertyListRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Update a property list.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("update_property_list")

    async def delete_property_list(
        self, params: DeletePropertyListRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Delete a property list.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("delete_property_list")

    # ========================================================================
    # V3 Governance (Collection Lists) Operations
    # ========================================================================

    async def create_collection_list(
        self,
        params: CreateCollectionListRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Create a collection list for governance filtering.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("create_collection_list")

    async def get_collection_list(
        self, params: GetCollectionListRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get a collection list with optional resolution.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("get_collection_list")

    async def list_collection_lists(
        self,
        params: ListCollectionListsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """List collection lists.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("list_collection_lists")

    async def update_collection_list(
        self,
        params: UpdateCollectionListRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Update a collection list.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("update_collection_list")

    async def delete_collection_list(
        self,
        params: DeleteCollectionListRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Delete a collection list.

        Override this in GovernanceHandler subclasses.
        """
        return self._not_supported("delete_collection_list")

    # ========================================================================
    # V3 TMP Operations
    # ========================================================================

    async def context_match(
        self, params: ContextMatchRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Match ad context to buyer packages.

        Override this to provide TMP context matching.
        """
        return self._not_supported("context_match")

    async def identity_match(
        self, params: IdentityMatchRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Match user identity for package eligibility.

        Override this to provide TMP identity matching.
        """
        return self._not_supported("identity_match")

    # ========================================================================
    # V3 Brand Rights Operations
    # ========================================================================

    async def get_brand_identity(
        self, params: GetBrandIdentityRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get brand identity information.

        Override this in BrandHandler subclasses.
        """
        return self._not_supported("get_brand_identity")

    async def get_rights(
        self, params: GetRightsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Get available rights for licensing.

        Override this in BrandHandler subclasses.
        """
        return self._not_supported("get_rights")

    async def acquire_rights(
        self, params: AcquireRightsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Acquire rights for brand content usage.

        Override this in BrandHandler subclasses.
        """
        return self._not_supported("acquire_rights")

    async def update_rights(
        self, params: UpdateRightsRequest | dict[str, Any], context: TContext | None = None
    ) -> Any:
        """Update terms of an existing rights acquisition.

        Override this in BrandHandler subclasses. Partial update: the
        request carries ``rights_id`` plus any subset of the mutable fields
        (``end_date``, ``impression_cap``, ``pricing_option_id``, ``paused``).

        Seller responsibilities you own when implementing this:

        * Reject updates on expired or revoked acquisitions with an
          appropriate error code — do not partial-commit.
        * Reject ``pricing_option_id`` swaps to incompatible options — the
          new option's terms must be a strict superset of the original.
        * Apply all accepted fields atomically — callers should never
          observe a half-applied update on failure.
        """
        return self._not_supported("update_rights")

    # ========================================================================
    # V3 Compliance Operations
    # ========================================================================

    async def comply_test_controller(
        self,
        params: ComplyTestControllerRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> Any:
        """Compliance test controller (sandbox only).

        Override this in ComplianceHandler subclasses.
        """
        return self._not_supported("comply_test_controller")
