"""Decisioning Platform v6.0 — Protocol-driven adopter framework.

The successor to ``adcp.server.ADCPHandler`` for adopters who want a
hybrid sync/handoff return shape and per-specialism Protocol classes
instead of inheriting + overriding methods on a base ABC. Lives inside
the existing ``adcp`` package so adopters reuse the foundation primitives
in ``adcp.signing`` / ``adcp._idempotency`` / ``adcp.server`` rather than
spinning up parallel implementations.

Quickstart::

    from adcp.decisioning import (
        DecisioningPlatform,
        DecisioningCapabilities,
        SingletonAccounts,
        SalesPlatform,
        create_adcp_server_from_platform,
        serve,
    )
    from adcp.types import (
        GetProductsRequest, GetProductsResponse,
        CreateMediaBuyRequest, CreateMediaBuySuccess,
    )


    class HelloSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            channels=["display"],
            pricing_models=["cpm"],
        )
        accounts = SingletonAccounts(account_id="hello")

        def get_products(self, req: GetProductsRequest, ctx) -> GetProductsResponse:
            return GetProductsResponse(products=[...])

        def create_media_buy(
            self, req: CreateMediaBuyRequest, ctx,
        ) -> CreateMediaBuySuccess:
            return CreateMediaBuySuccess(media_buy_id=f"mb_{req.idempotency_key}", ...)


    serve(create_adcp_server_from_platform(
        platform=HelloSeller(), name="hello-seller", version="0.0.1",
    ))

See ``examples/hello_seller.py`` for the runnable version.
"""

from __future__ import annotations

from adcp.decisioning.account_projection import (
    project_account_for_response,
    project_business_entity_for_response,
    to_wire_account,
    to_wire_sync_accounts_row,
    to_wire_sync_governance_row,
)
from adcp.decisioning.accounts import (
    AccountStore,
    AccountStoreList,
    AccountStoreSyncGovernance,
    AccountStoreUpsert,
    ExplicitAccounts,
    FromAuthAccounts,
    ResolveContext,
    SingletonAccounts,
)
from adcp.decisioning.compose import (
    ShortCircuit,
    compose_method,
    require_account_match,
    require_advertiser_match,
    require_org_scope,
)
from adcp.decisioning.context import (
    AuthInfo,
    RequestContext,
)
from adcp.decisioning.errors import (
    AccountNotFoundError,
    AuthRequiredError,
    BillingNotPermittedForAgentError,
    MediaBuyNotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ServiceUnavailableError,
    UnsupportedFeatureError,
    ValidationError,
)
from adcp.decisioning.helpers import ref_account_id
from adcp.decisioning.media_buy_store import (
    MediaBuyStore,
    create_media_buy_store,
)
from adcp.decisioning.mock_ad_server import (
    InMemoryMockAdServer,
    MockAdServer,
)
from adcp.decisioning.oauth_passthrough import (
    create_oauth_passthrough_resolver,
)
from adcp.decisioning.platform import (
    GOVERNANCE_SPECIALISMS,
    DecisioningCapabilities,
    DecisioningPlatform,
)
from adcp.decisioning.registry import (
    ApiKeyCredential,
    BillingMode,
    BuyerAgent,
    BuyerAgentDefaultTerms,
    BuyerAgentRegistry,
    BuyerAgentStatus,
    Credential,
    HttpSigCredential,
    OAuthCredential,
    bearer_only_registry,
    mixed_registry,
    signing_only_registry,
    validate_billing_for_agent,
)
from adcp.decisioning.registry_cache import (
    AuditingBuyerAgentRegistry,
    CachingBuyerAgentRegistry,
    RateLimitedBuyerAgentRegistry,
)
from adcp.decisioning.resolve import (
    CollectionList,
    Format,
    FormatReferenceStructuredObject,
    PropertyList,
    PropertyListReference,
    ResourceResolver,
)
from adcp.decisioning.roster_store import (
    create_roster_account_store,
)
from adcp.decisioning.serve import (
    create_adcp_server_from_platform,
    serve,
)
from adcp.decisioning.specialisms import (
    AudiencePlatform,
    BrandRightsPlatform,
    CampaignGovernancePlatform,
    CollectionListsPlatform,
    ContentStandardsPlatform,
    CreativeAdServerPlatform,
    CreativeBuilderPlatform,
    PropertyListsPlatform,
    SalesPlatform,
    SignalsPlatform,
)
from adcp.decisioning.state import (
    GovernanceContextJWS,
    Proposal,
    StateReader,
    WorkflowObjectType,
    WorkflowStep,
)
from adcp.decisioning.state_machines import (
    CREATIVE_ASSET_TRANSITIONS,
    MEDIA_BUY_TRANSITIONS,
    assert_creative_transition,
    assert_media_buy_transition,
)
from adcp.decisioning.task_registry import (
    InMemoryTaskRegistry,
    TaskHandoffContext,
    TaskRegistry,
    TaskState,
)
from adcp.decisioning.tenant_store import create_tenant_store
from adcp.decisioning.translation import (
    TranslationMap,
    create_translation_map,
)
from adcp.decisioning.types import (
    Account,
    AdcpError,
    MaybeAsync,
    SalesResult,
    SyncAccountsResultRow,
    SyncGovernanceEntry,
    SyncGovernanceResultRow,
    TaskHandoff,
    WorkflowHandoff,
)
from adcp.decisioning.upstream import (
    ApiKey,
    AuthContext,
    DynamicBearer,
    NoAuth,
    StaticBearer,
    UpstreamAuth,
    UpstreamHttpClient,
    create_upstream_http_client,
)

# Conditional import: PgTaskRegistry needs the [pg] extra. Always expose
# the name — when psycopg isn't installed we fall through to a stub class whose
# constructor raises ImportError with the install hint. Matches the pattern
# used by adcp.signing for PgReplayStore.
#
# ``PostgresTaskRegistry`` is the pre-4.4 name and remains as a deprecated
# alias through the 4.4.x line; renamed to ``PgTaskRegistry`` to match the
# ``Pg*`` convention shared with PgReplayStore / PgBuyerAgentRegistry /
# PgWebhookDeliverySupervisor.
try:
    from adcp.decisioning.pg import PgTaskRegistry, PostgresTaskRegistry  # noqa: F401
except ImportError:  # pragma: no cover — exercised by the [pg] extra tests
    from typing import ClassVar as _ClassVar

    class PgTaskRegistry:  # type: ignore[no-redef]
        """Stub raised when ``adcp[pg]`` isn't installed.

        Attempting to instantiate raises :class:`ImportError` with the
        install-hint text from :mod:`adcp.decisioning.pg.task_registry`.
        """

        is_durable: _ClassVar[bool] = True

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ImportError(
                "PgTaskRegistry requires psycopg3 and psycopg-pool. "
                "Install the 'pg' extra: `pip install 'adcp[pg]'` "
                "(Poetry: `poetry add 'adcp[pg]'`)."
            )

    # Deprecated alias preserved through 4.4.x.
    PostgresTaskRegistry: type[PgTaskRegistry] = PgTaskRegistry  # type: ignore[no-redef]


__all__ = [
    "Account",
    "AccountNotFoundError",
    "AccountStore",
    "AccountStoreList",
    "AccountStoreSyncGovernance",
    "AccountStoreUpsert",
    "AdcpError",
    "ApiKey",
    "ApiKeyCredential",
    "AuthContext",
    "AudiencePlatform",
    "AuditingBuyerAgentRegistry",
    "AuthInfo",
    "AuthRequiredError",
    "BillingMode",
    "BillingNotPermittedForAgentError",
    "BrandRightsPlatform",
    "BuyerAgent",
    "BuyerAgentDefaultTerms",
    "BuyerAgentRegistry",
    "BuyerAgentStatus",
    "CachingBuyerAgentRegistry",
    "CampaignGovernancePlatform",
    "CREATIVE_ASSET_TRANSITIONS",
    "CollectionList",
    "CollectionListsPlatform",
    "ContentStandardsPlatform",
    "Credential",
    "CreativeAdServerPlatform",
    "CreativeBuilderPlatform",
    "DecisioningCapabilities",
    "DecisioningPlatform",
    "DynamicBearer",
    "ExplicitAccounts",
    "Format",
    "FormatReferenceStructuredObject",
    "FromAuthAccounts",
    "GOVERNANCE_SPECIALISMS",
    "GovernanceContextJWS",
    "HttpSigCredential",
    "InMemoryMockAdServer",
    "InMemoryTaskRegistry",
    "MEDIA_BUY_TRANSITIONS",
    "MaybeAsync",
    "MediaBuyNotFoundError",
    "MediaBuyStore",
    "MockAdServer",
    "NoAuth",
    "OAuthCredential",
    "PermissionDeniedError",
    "PgTaskRegistry",
    "PostgresTaskRegistry",
    "Proposal",
    "PropertyList",
    "PropertyListReference",
    "PropertyListsPlatform",
    "RateLimitedBuyerAgentRegistry",
    "RateLimitedError",
    "RequestContext",
    "ResolveContext",
    "ResourceResolver",
    "SalesPlatform",
    "SalesResult",
    "ServiceUnavailableError",
    "SignalsPlatform",
    "SingletonAccounts",
    "StateReader",
    "StaticBearer",
    "SyncAccountsResultRow",
    "SyncGovernanceEntry",
    "SyncGovernanceResultRow",
    "TaskHandoff",
    "TaskHandoffContext",
    "TaskRegistry",
    "TaskState",
    "TranslationMap",
    "UnsupportedFeatureError",
    "UpstreamAuth",
    "UpstreamHttpClient",
    "ValidationError",
    "WorkflowHandoff",
    "WorkflowObjectType",
    "WorkflowStep",
    "ShortCircuit",
    "assert_creative_transition",
    "assert_media_buy_transition",
    "bearer_only_registry",
    "compose_method",
    "create_adcp_server_from_platform",
    "create_media_buy_store",
    "create_oauth_passthrough_resolver",
    "create_roster_account_store",
    "create_tenant_store",
    "create_translation_map",
    "create_upstream_http_client",
    "require_account_match",
    "require_advertiser_match",
    "require_org_scope",
    "mixed_registry",
    "project_account_for_response",
    "project_business_entity_for_response",
    "ref_account_id",
    "serve",
    "signing_only_registry",
    "to_wire_account",
    "to_wire_sync_accounts_row",
    "to_wire_sync_governance_row",
    "validate_billing_for_agent",
]
