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
from adcp.decisioning.resolve import (
    CollectionList,
    Format,
    FormatReferenceStructuredObject,
    PropertyList,
    PropertyListReference,
    ResourceResolver,
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
)

__all__ = [
    "Account",
    "AccountStore",
    "AdcpError",
    "AuthInfo",
    "CollectionList",
    "DecisioningCapabilities",
    "DecisioningPlatform",
    "ExplicitAccounts",
    "Format",
    "FormatReferenceStructuredObject",
    "FromAuthAccounts",
    "GOVERNANCE_SPECIALISMS",
    "GovernanceContextJWS",
    "InMemoryTaskRegistry",
    "MaybeAsync",
    "Proposal",
    "PropertyList",
    "PropertyListReference",
    "RequestContext",
    "ResourceResolver",
    "SalesResult",
    "SingletonAccounts",
    "StateReader",
    "TaskHandoff",
    "TaskHandoffContext",
    "TaskRegistry",
    "TaskState",
    "WorkflowObjectType",
    "WorkflowStep",
]
