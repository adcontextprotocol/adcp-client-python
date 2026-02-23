"""Tests for catalog types added in sync_catalogs support.

These types enable catalog feed management: syncing product catalogs,
store inventory, offerings, and other data feeds with platforms.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_catalog_types_importable_from_adcp():
    """All catalog types are importable from the main adcp package."""
    from adcp import (
        AssetSelectors,
        Catalog,
        CatalogAction,
        CatalogItemStatus,
        CatalogRequirements,
        CatalogType,
        ContentIdType,
        FeedFormat,
        Gtin,
        OfferingAssetConstraint,
        OfferingAssetGroup,
        SyncCatalogResult,
        SyncCatalogsErrorResponse,
        SyncCatalogsInputRequired,
        SyncCatalogsRequest,
        SyncCatalogsResponse,
        SyncCatalogsSubmitted,
        SyncCatalogsSuccessResponse,
        SyncCatalogsWorking,
        UpdateFrequency,
    )

    assert Catalog is not None
    assert CatalogType is not None
    assert CatalogAction is not None
    assert CatalogItemStatus is not None
    assert CatalogRequirements is not None
    assert ContentIdType is not None
    assert FeedFormat is not None
    assert Gtin is not None
    assert OfferingAssetConstraint is not None
    assert OfferingAssetGroup is not None
    assert UpdateFrequency is not None
    assert SyncCatalogsRequest is not None
    assert SyncCatalogsResponse is not None
    assert SyncCatalogsInputRequired is not None
    assert SyncCatalogsSubmitted is not None
    assert SyncCatalogsWorking is not None
    assert SyncCatalogsSuccessResponse is not None
    assert SyncCatalogsErrorResponse is not None
    assert SyncCatalogResult is not None
    assert AssetSelectors is not None


def test_catalog_type_enum_values():
    """CatalogType enum has all spec-defined values."""
    from adcp import CatalogType

    expected = {
        "offering",
        "product",
        "inventory",
        "store",
        "promotion",
        "hotel",
        "flight",
        "job",
        "vehicle",
        "real_estate",
        "education",
        "destination",
        "app",
    }
    assert {e.value for e in CatalogType} == expected


def test_catalog_action_enum_values():
    """CatalogAction enum has all spec-defined values."""
    from adcp import CatalogAction

    expected = {"created", "updated", "unchanged", "failed", "deleted"}
    assert {e.value for e in CatalogAction} == expected


def test_catalog_item_status_enum_values():
    """CatalogItemStatus enum has all spec-defined values."""
    from adcp import CatalogItemStatus

    expected = {"approved", "pending", "rejected", "warning"}
    assert {e.value for e in CatalogItemStatus} == expected


def test_feed_format_enum_values():
    """FeedFormat enum has all spec-defined values."""
    from adcp import FeedFormat

    expected = {"google_merchant_center", "facebook_catalog", "shopify", "linkedin_jobs", "custom"}
    assert {e.value for e in FeedFormat} == expected


def test_update_frequency_enum_values():
    """UpdateFrequency enum has all spec-defined values."""
    from adcp import UpdateFrequency

    expected = {"realtime", "hourly", "daily", "weekly"}
    assert {e.value for e in UpdateFrequency} == expected


def test_catalog_with_url():
    """Catalog accepts URL-based feed configuration."""
    from adcp import Catalog, CatalogType, FeedFormat, UpdateFrequency

    catalog = Catalog.model_validate(
        {
            "type": "product",
            "catalog_id": "my-products",
            "name": "My Product Feed",
            "url": "https://feeds.example.com/products.xml",
            "feed_format": "google_merchant_center",
            "update_frequency": "daily",
        }
    )
    assert catalog.type == CatalogType.product
    assert catalog.catalog_id == "my-products"
    assert catalog.feed_format == FeedFormat.google_merchant_center
    assert catalog.update_frequency == UpdateFrequency.daily


def test_catalog_type_required():
    """Catalog requires the type field."""
    from adcp import Catalog

    with pytest.raises(ValidationError):
        Catalog.model_validate({})


def test_catalog_offering_type_inline():
    """Catalog accepts inline items for offering type."""
    from adcp import Catalog, CatalogType

    catalog = Catalog.model_validate(
        {
            "type": "offering",
            "items": [{"offering_id": "promo-1", "name": "Summer Sale"}],
        }
    )
    assert catalog.type == CatalogType.offering
    assert catalog.items is not None
    assert len(catalog.items) == 1


def test_sync_catalogs_request_basic():
    """SyncCatalogsRequest accepts account_id with catalogs array."""
    from adcp import SyncCatalogsRequest

    req = SyncCatalogsRequest.model_validate(
        {
            "account_id": "acct_123",
            "catalogs": [
                {
                    "type": "product",
                    "catalog_id": "main-feed",
                    "url": "https://feeds.example.com/products.xml",
                    "feed_format": "google_merchant_center",
                    "update_frequency": "daily",
                }
            ],
        }
    )
    assert req.account_id == "acct_123"
    assert req.catalogs is not None
    assert len(req.catalogs) == 1


def test_sync_catalogs_request_discovery_only():
    """SyncCatalogsRequest accepts discovery-only mode (no catalogs)."""
    from adcp import SyncCatalogsRequest

    req = SyncCatalogsRequest.model_validate({"account_id": "acct_123"})
    assert req.account_id == "acct_123"
    assert req.catalogs is None


def test_sync_catalogs_response_success():
    """SyncCatalogsSuccessResponse validates from spec example."""
    from adcp import CatalogAction, SyncCatalogResult, SyncCatalogsSuccessResponse

    data = {
        "catalogs": [
            {
                "catalog_id": "main-feed",
                "action": "created",
                "item_count": 1250,
                "items_pending": 1250,
            }
        ]
    }
    resp = SyncCatalogsSuccessResponse.model_validate(data)
    assert resp.catalogs is not None
    assert len(resp.catalogs) == 1
    assert resp.catalogs[0].catalog_id == "main-feed"
    assert resp.catalogs[0].action == CatalogAction.created
    assert isinstance(resp.catalogs[0], SyncCatalogResult)


def test_sync_catalogs_response_error():
    """SyncCatalogsErrorResponse validates from spec example."""
    from adcp import SyncCatalogsErrorResponse

    data = {
        "errors": [
            {
                "code": "UNAUTHORIZED",
                "message": "Authentication failed",
            }
        ]
    }
    resp = SyncCatalogsErrorResponse.model_validate(data)
    assert resp.errors is not None
    assert len(resp.errors) == 1


def test_catalog_requirements_validates():
    """CatalogRequirements validates from spec example."""
    from adcp import CatalogRequirements, CatalogType

    req = CatalogRequirements.model_validate(
        {
            "catalog_type": "product",
            "required": True,
            "min_items": 3,
            "required_fields": ["title", "price", "image_url"],
        }
    )
    assert req.catalog_type == CatalogType.product
    assert req.required is True
    assert req.min_items == 3


def test_catalog_requirements_offering_with_constraints():
    """CatalogRequirements for offering type accepts offering_asset_constraints."""
    from adcp import CatalogRequirements

    req = CatalogRequirements.model_validate(
        {
            "catalog_type": "offering",
            "offering_asset_constraints": [
                {
                    "asset_group_id": "headlines",
                    "asset_type": "text",
                    "min_count": 3,
                    "max_count": 15,
                    "required": True,
                }
            ],
        }
    )
    assert req.offering_asset_constraints is not None
    assert len(req.offering_asset_constraints) == 1
    assert req.offering_asset_constraints[0].asset_group_id == "headlines"


def test_backward_compat_removed_types_still_importable():
    """Types removed from upstream schemas still importable as permissive stubs."""
    from adcp import (
        AssetSelectors,
        PromotedOfferings,
        PromotedOfferingsAssetRequirements,
        PromotedOfferingsRequirement,
        PromotedProducts,
    )

    # Model stubs accept any payload (extra="allow")
    po = PromotedOfferings.model_validate({"any_field": "any_value"})
    assert po is not None

    par = PromotedOfferingsAssetRequirements.model_validate({"requires": ["brand.logos"]})
    assert par is not None

    # PromotedOfferingsRequirement is preserved as an Enum so attribute/iteration access works
    assert PromotedOfferingsRequirement.si_agent_url.value == "si_agent_url"
    assert PromotedOfferingsRequirement.brand_logos.value == "brand.logos"
    assert {e.value for e in PromotedOfferingsRequirement} == {
        "si_agent_url",
        "offerings",
        "brand.logos",
        "brand.colors",
        "brand.tone",
        "brand.assets",
        "brand.product_catalog",
    }

    pp = PromotedProducts.model_validate({"product_id": "prod-123"})
    assert pp is not None

    asel = AssetSelectors.model_validate({"selectors": ["hero_image"]})
    assert asel is not None


def test_signal_catalog_type_unaffected():
    """SignalCatalogType (signals domain) is unaffected by catalog type changes."""
    from adcp import SignalCatalogType

    expected = {"marketplace", "custom", "owned"}
    assert {e.value for e in SignalCatalogType} == expected


def test_catalog_type_not_signal_catalog_type():
    """CatalogType (feed catalogs) is distinct from SignalCatalogType (signals)."""
    from adcp import CatalogType, SignalCatalogType

    assert CatalogType is not SignalCatalogType
    assert CatalogType.offering.value == "offering"
    assert SignalCatalogType.marketplace.value == "marketplace"


def test_creative_catalogs_field_accepts_list():
    """Creative in list_creatives_response accepts catalogs as a non-empty list."""
    from adcp import Catalog
    from adcp.types.generated_poc.media_buy.list_creatives_response import Creative as ListCreative

    creative = ListCreative.model_validate(
        {
            "creative_id": "c1",
            "name": "Test Creative",
            "created_date": "2026-01-01T00:00:00Z",
            "updated_date": "2026-01-01T00:00:00Z",
            "format_id": {"agent_url": "https://creative.adcontextprotocol.org", "id": "banner"},
            "status": "approved",
            "catalogs": [{"type": "product", "catalog_id": "feed-1"}],
        }
    )
    assert creative.catalogs is not None
    assert len(creative.catalogs) == 1
    assert isinstance(creative.catalogs[0], Catalog)


def test_creative_catalogs_field_rejects_empty_list():
    """Creative in list_creatives_response rejects empty catalogs list (min_length=1)."""
    from adcp.types.generated_poc.media_buy.list_creatives_response import Creative as ListCreative

    with pytest.raises(ValidationError):
        ListCreative.model_validate(
            {
                "creative_id": "c1",
                "name": "Test Creative",
                "created_date": "2026-01-01T00:00:00Z",
                "updated_date": "2026-01-01T00:00:00Z",
                "format_id": {
                    "agent_url": "https://creative.adcontextprotocol.org",
                    "id": "banner",
                },
                "status": "approved",
                "catalogs": [],
            }
        )


def test_creative_asset_catalogs_field():
    """CreativeAsset accepts catalogs as a non-empty list."""
    from adcp import Catalog
    from adcp.types import CreativeAsset

    asset = CreativeAsset.model_validate(
        {
            "creative_id": "c1",
            "name": "Test Creative",
            "format_id": {"agent_url": "https://creative.adcontextprotocol.org", "id": "banner"},
            "assets": {},
            "catalogs": [{"type": "product", "catalog_id": "feed-1"}],
        }
    )
    assert asset.catalogs is not None
    assert len(asset.catalogs) == 1
    assert isinstance(asset.catalogs[0], Catalog)


def test_creative_manifest_catalogs_field():
    """CreativeManifest accepts catalogs as a non-empty list."""
    from adcp import Catalog
    from adcp.types import CreativeManifest

    manifest = CreativeManifest.model_validate(
        {
            "format_id": {"agent_url": "https://creative.adcontextprotocol.org", "id": "banner"},
            "assets": {},
            "catalogs": [{"type": "offering", "items": [{"offering_id": "o1", "name": "Deal"}]}],
        }
    )
    assert manifest.catalogs is not None
    assert len(manifest.catalogs) == 1
    assert isinstance(manifest.catalogs[0], Catalog)


def test_universal_macro_catalog_values():
    """UniversalMacro contains catalog-context macros added in schema sync."""
    from adcp.types._generated import UniversalMacro

    catalog_macros = {
        "CATALOG_ID",
        "SKU",
        "GTIN",
        "OFFERING_ID",
        "JOB_ID",
        "HOTEL_ID",
        "FLIGHT_ID",
        "VEHICLE_ID",
        "LISTING_ID",
        "STORE_ID",
        "PROGRAM_ID",
        "DESTINATION_ID",
        "CREATIVE_VARIANT_ID",
    }
    assert catalog_macros.issubset({e.value for e in UniversalMacro})
