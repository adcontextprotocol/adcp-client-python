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
)
from adcp.decisioning.accounts import (
    AccountStore,
    ExplicitAccounts,
    FromAuthAccounts,
    SingletonAccounts,
)
from adcp.decisioning.context import (
    AuthInfo,
    RequestContext,
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
from adcp.decisioning.resolve import (
    CollectionList,
    Format,
    FormatReferenceStructuredObject,
    PropertyList,
    PropertyListReference,
    ResourceResolver,
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
from adcp.decisioning.task_registry import (
    InMemoryTaskRegistry,
    TaskHandoffContext,
    TaskRegistry,
    TaskState,
)
from adcp.decisioning.types import (
    Account,
    AdcpError,
    MaybeAsync,
    SalesResult,
    TaskHandoff,
    WorkflowHandoff,
)

__all__ = [
    "Account",
    "AccountStore",
    "AdcpError",
    "ApiKeyCredential",
    "AudiencePlatform",
    "AuthInfo",
    "BillingMode",
    "BrandRightsPlatform",
    "BuyerAgent",
    "BuyerAgentDefaultTerms",
    "BuyerAgentRegistry",
    "BuyerAgentStatus",
    "CampaignGovernancePlatform",
    "CollectionList",
    "CollectionListsPlatform",
    "ContentStandardsPlatform",
    "Credential",
    "CreativeAdServerPlatform",
    "CreativeBuilderPlatform",
    "DecisioningCapabilities",
    "DecisioningPlatform",
    "ExplicitAccounts",
    "Format",
    "FormatReferenceStructuredObject",
    "FromAuthAccounts",
    "GOVERNANCE_SPECIALISMS",
    "GovernanceContextJWS",
    "HttpSigCredential",
    "InMemoryTaskRegistry",
    "MaybeAsync",
    "OAuthCredential",
    "Proposal",
    "PropertyList",
    "PropertyListReference",
    "PropertyListsPlatform",
    "RequestContext",
    "ResourceResolver",
    "SalesPlatform",
    "SalesResult",
    "SignalsPlatform",
    "SingletonAccounts",
    "StateReader",
    "TaskHandoff",
    "TaskHandoffContext",
    "TaskRegistry",
    "TaskState",
    "WorkflowHandoff",
    "WorkflowObjectType",
    "WorkflowStep",
    "bearer_only_registry",
    "create_adcp_server_from_platform",
    "mixed_registry",
    "project_account_for_response",
    "project_business_entity_for_response",
    "serve",
    "signing_only_registry",
    "validate_billing_for_agent",
]
