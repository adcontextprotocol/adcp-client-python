"""AdCP seller types — curated partial surface.

Sell-side (SSP / publisher) surface — products / offerings, properties and
property lists, content standards, governance, catalog sync, account financials.

A stable, narrow alternative to importing the whole :mod:`adcp.types`
namespace. Every name here is also exported from :mod:`adcp.types`; this
module simply groups the ones a seller integration reaches for, and never
exposes the internal generated layer.

This module is for curation and discoverability, not a separate
performance tier: importing it is cheap, but the first access to *any* AdCP
type (here or via :mod:`adcp.types` / :mod:`adcp`) realizes the full generated
Pydantic graph — there is no per-domain graph. Use it for a smaller, focused
import surface.

    from adcp.types.seller import Product
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "Product",
    "Offering",
    "OfferingAssetGroup",
    "OfferingAssetConstraint",
    "Property",
    "PropertyType",
    "PropertyList",
    "PropertyListReference",
    "PropertyListFilters",
    "CreatePropertyListRequest",
    "CreatePropertyListResponse",
    "GetPropertyListRequest",
    "GetPropertyListResponse",
    "ListPropertyListsRequest",
    "ListPropertyListsResponse",
    "UpdatePropertyListRequest",
    "DeletePropertyListRequest",
    "AuthorizedAgents",
    "AuthorizedAgent",
    "PublisherProperties",
    "ContentStandards",
    "CreateContentStandardsRequest",
    "GetContentStandardsRequest",
    "ListContentStandardsRequest",
    "UpdateContentStandardsRequest",
    "ValidateContentDeliveryRequest",
    "GovernanceAgent",
    "CheckGovernanceRequest",
    "CheckGovernanceResponse",
    "SyncGovernanceRequest",
    "ReportPlanOutcomeRequest",
    "GetPlanAuditLogsRequest",
    "Catalog",
    "CatalogType",
    "CatalogRequirements",
    "SyncCatalogsRequest",
    "SyncCatalogsResponse",
    "SyncCatalogResult",
    "Account",
    "GetAccountFinancialsRequest",
    "GetAccountFinancialsResponse",
    "CreditLimit",
    "PaymentTerms",
    "Setup",
    "ReportingCapabilities",
    "SellerAgentReference",
    "ListAccountsRequest",
    "SyncAccountsRequest",
]


if not TYPE_CHECKING:
    # Lazy runtime resolution (shared with the other partial modules). Defined
    # under ``not TYPE_CHECKING`` so type checkers see the surface only via the
    # explicit ``TYPE_CHECKING`` re-export block below — a typo'd import is
    # flagged rather than silently typed as ``object``.
    from adcp.types._partial import lazy_partial_surface

    __getattr__, __dir__ = lazy_partial_surface(__name__, __all__, globals())


if TYPE_CHECKING:
    # Eager re-export so type checkers and IDEs see the surface; resolved
    # lazily through ``__getattr__`` at runtime.
    from adcp.types import (  # noqa: F401
        Account,
        AuthorizedAgent,
        AuthorizedAgents,
        Catalog,
        CatalogRequirements,
        CatalogType,
        CheckGovernanceRequest,
        CheckGovernanceResponse,
        ContentStandards,
        CreateContentStandardsRequest,
        CreatePropertyListRequest,
        CreatePropertyListResponse,
        CreditLimit,
        DeletePropertyListRequest,
        GetAccountFinancialsRequest,
        GetAccountFinancialsResponse,
        GetContentStandardsRequest,
        GetPlanAuditLogsRequest,
        GetPropertyListRequest,
        GetPropertyListResponse,
        GovernanceAgent,
        ListAccountsRequest,
        ListContentStandardsRequest,
        ListPropertyListsRequest,
        ListPropertyListsResponse,
        Offering,
        OfferingAssetConstraint,
        OfferingAssetGroup,
        PaymentTerms,
        Product,
        Property,
        PropertyList,
        PropertyListFilters,
        PropertyListReference,
        PropertyType,
        PublisherProperties,
        ReportingCapabilities,
        ReportPlanOutcomeRequest,
        SellerAgentReference,
        Setup,
        SyncAccountsRequest,
        SyncCatalogResult,
        SyncCatalogsRequest,
        SyncCatalogsResponse,
        SyncGovernanceRequest,
        UpdateContentStandardsRequest,
        UpdatePropertyListRequest,
        ValidateContentDeliveryRequest,
    )
