"""Tests for backward compatibility aliases.

Validates that deprecated type names still import and resolve to the correct
new types. This is the core safety net for the schema sync — consumers using
old names must not get ImportError or silently wrong types.
"""

from __future__ import annotations


class TestPricingTypeRenames:
    """Upstream merged auction/fixed variants into single types."""

    def test_cpm_auction_imports_from_adcp(self):
        from adcp import CpmAuctionPricingOption, CpmPricingOption

        assert CpmAuctionPricingOption is CpmPricingOption

    def test_cpm_fixed_rate_imports_from_adcp(self):
        from adcp import CpmFixedRatePricingOption, CpmPricingOption

        assert CpmFixedRatePricingOption is CpmPricingOption

    def test_vcpm_auction_imports_from_adcp(self):
        from adcp import VcpmAuctionPricingOption, VcpmPricingOption

        assert VcpmAuctionPricingOption is VcpmPricingOption

    def test_vcpm_fixed_rate_imports_from_adcp(self):
        from adcp import VcpmFixedRatePricingOption, VcpmPricingOption

        assert VcpmFixedRatePricingOption is VcpmPricingOption

    def test_cpm_auction_imports_from_types(self):
        from adcp.types import CpmAuctionPricingOption, CpmPricingOption

        assert CpmAuctionPricingOption is CpmPricingOption

    def test_vcpm_auction_imports_from_types(self):
        from adcp.types import VcpmAuctionPricingOption, VcpmPricingOption

        assert VcpmAuctionPricingOption is VcpmPricingOption


class TestActivationKeyRenames:
    """Upstream renamed property_id/property_tag to segment_id/key_value."""

    def test_property_id_activation_key_imports(self):
        from adcp import PropertyIdActivationKey, SegmentIdActivationKey

        assert PropertyIdActivationKey is SegmentIdActivationKey

    def test_property_tag_activation_key_imports(self):
        from adcp import KeyValueActivationKey, PropertyTagActivationKey

        assert PropertyTagActivationKey is KeyValueActivationKey

    def test_property_id_imports_from_types(self):
        from adcp.types import PropertyIdActivationKey, SegmentIdActivationKey

        assert PropertyIdActivationKey is SegmentIdActivationKey

    def test_property_tag_imports_from_types(self):
        from adcp.types import KeyValueActivationKey, PropertyTagActivationKey

        assert PropertyTagActivationKey is KeyValueActivationKey


class TestPreviewAliasRenames:
    """PreviewCreativeRequest collapsed into a single class with a request_type
    enum; the per-mode Request aliases (Format/Manifest/Static/Interactive, Single/
    Batch/Variant) are no longer distinct types and their aliases were removed.
    The remaining response aliases are exposed only through the explicit
    legacy surface."""

    def test_preview_creative_static_response(self):
        from adcp import LegacyPreviewCreativeResponse1, LegacyPreviewCreativeSingleResponse

        assert LegacyPreviewCreativeSingleResponse is LegacyPreviewCreativeResponse1

    def test_preview_creative_interactive_response(self):
        from adcp import LegacyPreviewCreativeBatchResponse, LegacyPreviewCreativeResponse2

        assert LegacyPreviewCreativeBatchResponse is LegacyPreviewCreativeResponse2


class TestStatusTypeBackwardCompat:
    """Status must resolve to delivery status, not invoice status."""

    def test_status_is_delivery_status(self):
        """Status from adcp.types should be the media buy delivery status."""
        from adcp.types import MediaBuyDeliveryStatus, Status

        assert Status is MediaBuyDeliveryStatus

    def test_status_has_delivery_values(self):
        """Status should accept delivery status enum values."""
        from adcp.types import Status

        # Media buy delivery status values
        for value in ["pending", "active", "paused", "completed", "failed"]:
            status = Status(value)
            assert status.value == value


class TestCatalogGroupBinding:
    """CatalogFieldBinding1 should have a semantic alias."""

    def test_catalog_group_binding_imports(self):
        from adcp.types import CatalogGroupBinding

        assert CatalogGroupBinding is not None

    def test_catalog_group_binding_is_correct_type(self):
        from adcp.types import CatalogGroupBinding
        from adcp.types._generated import CatalogFieldBinding1

        assert CatalogGroupBinding is CatalogFieldBinding1

    def test_catalog_group_binding_has_correct_kind(self):
        from adcp.types import CatalogGroupBinding

        assert "kind" in CatalogGroupBinding.__annotations__


class TestAllBackwardCompatInAll:
    """All backward-compat aliases must appear in __all__."""

    def test_all_compat_aliases_in_types_all(self):
        import adcp.types

        compat_aliases = [
            "CpmAuctionPricingOption",
            "CpmFixedRatePricingOption",
            "VcpmAuctionPricingOption",
            "VcpmFixedRatePricingOption",
            "PropertyIdActivationKey",
            "PropertyTagActivationKey",
        ]
        for alias in compat_aliases:
            assert alias in adcp.types.__all__, f"{alias} missing from types.__all__"

    def test_all_compat_aliases_in_adcp_all(self):
        import adcp

        compat_aliases = [
            "CpmAuctionPricingOption",
            "CpmFixedRatePricingOption",
            "VcpmAuctionPricingOption",
            "VcpmFixedRatePricingOption",
            "PropertyIdActivationKey",
            "PropertyTagActivationKey",
        ]
        for alias in compat_aliases:
            assert alias in adcp.__all__, f"{alias} missing from adcp.__all__"
