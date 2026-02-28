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
    """Upstream changed format/manifest → single/batch/variant discriminator."""

    def test_preview_creative_format_request(self):
        from adcp import PreviewCreativeFormatRequest, PreviewCreativeSingleRequest

        assert PreviewCreativeFormatRequest is PreviewCreativeSingleRequest

    def test_preview_creative_manifest_request(self):
        from adcp import PreviewCreativeBatchRequest, PreviewCreativeManifestRequest

        assert PreviewCreativeManifestRequest is PreviewCreativeBatchRequest

    def test_preview_creative_static_response(self):
        from adcp import PreviewCreativeSingleResponse, PreviewCreativeStaticResponse

        assert PreviewCreativeStaticResponse is PreviewCreativeSingleResponse

    def test_preview_creative_interactive_response(self):
        from adcp import PreviewCreativeBatchResponse, PreviewCreativeInteractiveResponse

        assert PreviewCreativeInteractiveResponse is PreviewCreativeBatchResponse

    def test_format_request_from_types(self):
        from adcp.types import PreviewCreativeFormatRequest, PreviewCreativeSingleRequest

        assert PreviewCreativeFormatRequest is PreviewCreativeSingleRequest

    def test_manifest_request_from_types(self):
        from adcp.types import PreviewCreativeBatchRequest, PreviewCreativeManifestRequest

        assert PreviewCreativeManifestRequest is PreviewCreativeBatchRequest


class TestRemovedTypeStubs:
    """Types removed from upstream schemas that have backward-compat stubs."""

    def test_list_authorized_properties_request_imports(self):
        from adcp import ListAuthorizedPropertiesRequest

        assert ListAuthorizedPropertiesRequest is not None

    def test_list_authorized_properties_response_imports(self):
        from adcp import ListAuthorizedPropertiesResponse

        assert ListAuthorizedPropertiesResponse is not None

    def test_package_status_imports(self):
        from adcp import PackageStatus

        assert PackageStatus is not None

    def test_deliver_to_imports(self):
        from adcp import DeliverTo

        assert DeliverTo is not None

    def test_pricing_imports(self):
        from adcp import Pricing

        assert Pricing is not None

    def test_measurement_imports(self):
        from adcp import Measurement
        from adcp.types import OutcomeMeasurement

        assert Measurement is OutcomeMeasurement

    def test_stubs_accept_extra_fields(self):
        """Stubs should be open models that accept arbitrary fields."""
        from adcp import ListAuthorizedPropertiesRequest

        # These stubs should not raise on unknown fields
        stub = ListAuthorizedPropertiesRequest(some_field="value")
        assert stub.some_field == "value"

    def test_package_status_is_enum(self):
        """PackageStatus should be an importable enum."""
        from adcp import PackageStatus

        assert hasattr(PackageStatus, "__members__")


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
            "PreviewCreativeFormatRequest",
            "PreviewCreativeManifestRequest",
            "PreviewCreativeStaticResponse",
            "PreviewCreativeInteractiveResponse",
            "ListAuthorizedPropertiesRequest",
            "ListAuthorizedPropertiesResponse",
            "PackageStatus",
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
            "PreviewCreativeFormatRequest",
            "PreviewCreativeManifestRequest",
            "PreviewCreativeStaticResponse",
            "PreviewCreativeInteractiveResponse",
            "ListAuthorizedPropertiesRequest",
            "ListAuthorizedPropertiesResponse",
            "PackageStatus",
            "DeliverTo",
            "Measurement",
            "Pricing",
        ]
        for alias in compat_aliases:
            assert alias in adcp.__all__, f"{alias} missing from adcp.__all__"
