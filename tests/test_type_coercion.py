"""Tests for type coercion ergonomics.

These tests verify that request types accept flexible input (strings for enums,
dicts for models) while maintaining type safety. This addresses GitHub issue #102.

Reference: https://github.com/adcontextprotocol/adcp-client-python/issues/102
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.types import (
    AssetContentType,
    FormatCategory,
    GetProductsRequest,
    ListCreativeFormatsRequest,
    ListCreativesRequest,
    PackageRequest,
)
from adcp.types.generated_poc.core.context import ContextObject
from adcp.types.generated_poc.core.ext import ExtensionObject
from adcp.types.generated_poc.enums.creative_sort_field import CreativeSortField
from adcp.types.generated_poc.enums.sort_direction import SortDirection
from adcp.types.generated_poc.media_buy.list_creatives_request import FieldModel, Sort


class TestEnumStringCoercion:
    """Test that enum fields accept string values."""

    def test_format_category_accepts_string(self):
        """ListCreativeFormatsRequest.type accepts string 'video'."""
        req = ListCreativeFormatsRequest(type="video")
        assert req.type == FormatCategory.video
        assert isinstance(req.type, FormatCategory)

    def test_format_category_accepts_enum(self):
        """ListCreativeFormatsRequest.type still accepts enum values."""
        req = ListCreativeFormatsRequest(type=FormatCategory.display)
        assert req.type == FormatCategory.display

    def test_format_category_accepts_none(self):
        """ListCreativeFormatsRequest.type accepts None."""
        req = ListCreativeFormatsRequest(type=None)
        assert req.type is None

    def test_all_format_categories_coerce(self):
        """All FormatCategory values coerce from strings."""
        for category in FormatCategory:
            req = ListCreativeFormatsRequest(type=category.value)
            assert req.type == category

    def test_invalid_format_category_raises(self):
        """Invalid string values raise ValidationError."""
        with pytest.raises(ValidationError):
            ListCreativeFormatsRequest(type="invalid_category")


class TestEnumListCoercion:
    """Test that list[Enum] fields accept lists of strings."""

    def test_asset_types_accepts_string_list(self):
        """ListCreativeFormatsRequest.asset_types accepts ['image', 'video']."""
        req = ListCreativeFormatsRequest(asset_types=["image", "video", "html"])
        assert len(req.asset_types) == 3
        assert req.asset_types[0] == AssetContentType.image
        assert req.asset_types[1] == AssetContentType.video
        assert req.asset_types[2] == AssetContentType.html
        assert all(isinstance(x, AssetContentType) for x in req.asset_types)

    def test_asset_types_accepts_enum_list(self):
        """ListCreativeFormatsRequest.asset_types still accepts enum list."""
        req = ListCreativeFormatsRequest(
            asset_types=[AssetContentType.image, AssetContentType.audio]
        )
        assert req.asset_types == [AssetContentType.image, AssetContentType.audio]

    def test_asset_types_accepts_mixed_list(self):
        """ListCreativeFormatsRequest.asset_types accepts mixed string/enum list."""
        req = ListCreativeFormatsRequest(asset_types=["image", AssetContentType.video, "html"])
        assert req.asset_types == [
            AssetContentType.image,
            AssetContentType.video,
            AssetContentType.html,
        ]

    def test_asset_types_accepts_none(self):
        """ListCreativeFormatsRequest.asset_types accepts None."""
        req = ListCreativeFormatsRequest(asset_types=None)
        assert req.asset_types is None


class TestDictToModelCoercion:
    """Test that model fields accept dict values."""

    def test_context_accepts_dict(self):
        """ListCreativeFormatsRequest.context accepts dict."""
        req = ListCreativeFormatsRequest(context={"user_id": "123", "session": "abc"})
        assert isinstance(req.context, ContextObject)
        assert req.context.user_id == "123"
        assert req.context.session == "abc"

    def test_context_accepts_model(self):
        """ListCreativeFormatsRequest.context still accepts ContextObject."""
        ctx = ContextObject(user_id="456")
        req = ListCreativeFormatsRequest(context=ctx)
        assert req.context is ctx

    def test_context_accepts_none(self):
        """ListCreativeFormatsRequest.context accepts None."""
        req = ListCreativeFormatsRequest(context=None)
        assert req.context is None

    def test_ext_accepts_dict(self):
        """ListCreativeFormatsRequest.ext accepts dict."""
        req = ListCreativeFormatsRequest(ext={"custom_field": "value"})
        assert isinstance(req.ext, ExtensionObject)
        assert req.ext.custom_field == "value"

    def test_get_products_request_context_accepts_dict(self):
        """GetProductsRequest.context accepts dict."""
        req = GetProductsRequest(context={"key": "value"})
        assert isinstance(req.context, ContextObject)
        assert req.context.key == "value"


class TestFieldModelStringCoercion:
    """Test that FieldModel lists accept string lists."""

    def test_fields_accepts_string_list(self):
        """ListCreativesRequest.fields accepts ['creative_id', 'name']."""
        req = ListCreativesRequest(fields=["creative_id", "name", "format"])
        assert len(req.fields) == 3
        assert req.fields[0] == FieldModel.creative_id
        assert req.fields[1] == FieldModel.name
        assert req.fields[2] == FieldModel.format
        assert all(isinstance(x, FieldModel) for x in req.fields)

    def test_fields_accepts_enum_list(self):
        """ListCreativesRequest.fields still accepts FieldModel list."""
        req = ListCreativesRequest(fields=[FieldModel.creative_id, FieldModel.status])
        assert req.fields == [FieldModel.creative_id, FieldModel.status]

    def test_fields_accepts_mixed_list(self):
        """ListCreativesRequest.fields accepts mixed string/enum list."""
        req = ListCreativesRequest(fields=["creative_id", FieldModel.name])
        assert req.fields == [FieldModel.creative_id, FieldModel.name]

    def test_all_field_models_coerce(self):
        """All FieldModel values coerce from strings."""
        all_fields = [f.value for f in FieldModel]
        req = ListCreativesRequest(fields=all_fields)
        assert len(req.fields) == len(FieldModel)
        for expected, actual in zip(FieldModel, req.fields):
            assert actual == expected


class TestSortEnumCoercion:
    """Test that Sort nested model accepts string enums."""

    def test_sort_field_accepts_string(self):
        """Sort.field accepts string value."""
        sort = Sort(field="name", direction=SortDirection.asc)
        assert sort.field == CreativeSortField.name
        assert isinstance(sort.field, CreativeSortField)

    def test_sort_direction_accepts_string(self):
        """Sort.direction accepts string value."""
        sort = Sort(field=CreativeSortField.created_date, direction="desc")
        assert sort.direction == SortDirection.desc
        assert isinstance(sort.direction, SortDirection)

    def test_sort_accepts_all_strings(self):
        """Sort accepts all fields as strings."""
        sort = Sort(field="updated_date", direction="asc")
        assert sort.field == CreativeSortField.updated_date
        assert sort.direction == SortDirection.asc

    def test_list_creatives_with_string_sort(self):
        """ListCreativesRequest works with string sort parameters."""
        req = ListCreativesRequest(
            sort=Sort(field="name", direction="asc"),
            fields=["creative_id"],
        )
        assert req.sort.field == CreativeSortField.name
        assert req.sort.direction == SortDirection.asc


class TestPackageRequestCoercion:
    """Test PackageRequest field coercion."""

    def test_ext_accepts_dict(self):
        """PackageRequest.ext accepts dict."""
        req = PackageRequest(
            budget=1000.0,
            buyer_ref="ref123",
            pricing_option_id="opt1",
            product_id="prod1",
            ext={"custom": "data"},
        )
        assert isinstance(req.ext, ExtensionObject)
        assert req.ext.custom == "data"


class TestBackwardCompatibility:
    """Test that existing code using explicit types still works."""

    def test_explicit_enum_values_work(self):
        """Existing code using enum values still works."""
        req = ListCreativeFormatsRequest(
            type=FormatCategory.video,
            asset_types=[AssetContentType.image, AssetContentType.video],
        )
        assert req.type == FormatCategory.video
        assert req.asset_types == [AssetContentType.image, AssetContentType.video]

    def test_explicit_model_values_work(self):
        """Existing code using explicit model construction still works."""
        ctx = ContextObject(user_id="123")
        ext = ExtensionObject(custom="value")
        req = ListCreativeFormatsRequest(context=ctx, ext=ext)
        assert req.context is ctx
        assert req.ext is ext

    def test_explicit_field_model_values_work(self):
        """Existing code using FieldModel enums still works."""
        req = ListCreativesRequest(
            fields=[FieldModel.creative_id, FieldModel.name],
        )
        assert req.fields == [FieldModel.creative_id, FieldModel.name]


class TestListVariance:
    """Test that subclass instances work without cast() due to coercion."""

    def test_subclass_creatives_work_without_cast(self):
        """PackageRequest accepts CreativeAsset subclass instances without cast().

        Due to the coerce_subclass_list validator, users no longer need to use
        cast() when passing lists of extended types. The validator accepts Any
        as input, which satisfies the type checker for subclass lists.
        """
        from pydantic import Field

        from adcp.types import CreativeAsset, FormatId, PackageRequest

        # Create an extended creative type
        class ExtendedCreative(CreativeAsset):
            """Extended with internal tracking fields."""

            internal_id: str | None = Field(None, exclude=True)

        # Create a subclass instance
        creative = ExtendedCreative(
            creative_id="c1",
            name="Test Creative",
            format_id=FormatId(agent_url="https://example.com", id="banner-300x250"),
            assets={},
            internal_id="internal-123",
        )

        # No cast() needed! The coercion validator accepts Any input
        package = PackageRequest(
            budget=1000.0,
            buyer_ref="ref123",
            pricing_option_id="opt1",
            product_id="prod1",
            creatives=[creative],  # type: ignore[list-item]
        )

        assert len(package.creatives) == 1
        assert package.creatives[0].creative_id == "c1"
        # Internal field is preserved at runtime
        assert package.creatives[0].internal_id == "internal-123"  # type: ignore[attr-defined]

    def test_subclass_serialization_excludes_internal_fields(self):
        """Extended fields marked exclude=True are not serialized."""
        from pydantic import Field

        from adcp.types import CreativeAsset, FormatId

        class ExtendedCreative(CreativeAsset):
            internal_id: str | None = Field(None, exclude=True)

        creative = ExtendedCreative(
            creative_id="c1",
            name="Test Creative",
            format_id=FormatId(agent_url="https://example.com", id="banner-300x250"),
            assets={},
            internal_id="should-not-appear",
        )

        data = creative.model_dump(mode="json")
        assert "internal_id" not in data
        assert data["creative_id"] == "c1"

    def test_create_media_buy_accepts_extended_packages(self):
        """CreateMediaBuyRequest.packages accepts PackageRequest subclass instances."""
        from datetime import datetime, timezone

        from pydantic import AnyUrl, Field

        from adcp.types import CreateMediaBuyRequest, PackageRequest

        # Create an extended package type
        class ExtendedPackage(PackageRequest):
            """Extended with internal tracking fields."""

            campaign_id: str | None = Field(None, exclude=True)

        package = ExtendedPackage(
            budget=1000.0,
            buyer_ref="ref123",
            pricing_option_id="opt1",
            product_id="prod1",
            campaign_id="internal-campaign-456",
        )

        # No cast() needed!
        request = CreateMediaBuyRequest(
            brand_manifest=AnyUrl("https://example.com/manifest.json"),  # URL reference
            buyer_ref="buyer-ref",
            start_time=datetime.now(timezone.utc),
            end_time=datetime(2025, 12, 31, tzinfo=timezone.utc),
            packages=[package],  # type: ignore[list-item]
        )

        assert len(request.packages) == 1
        assert request.packages[0].buyer_ref == "ref123"
        # Internal field is preserved at runtime
        assert request.packages[0].campaign_id == "internal-campaign-456"  # type: ignore[attr-defined]

    def test_update_packages_accepts_extended_creatives(self):
        """UpdateMediaBuyRequest Packages types accept extended CreativeAsset."""
        from pydantic import Field

        from adcp.types import CreativeAsset, FormatId
        from adcp.types.generated_poc.media_buy.update_media_buy_request import Packages

        class ExtendedCreative(CreativeAsset):
            internal_id: str | None = Field(None, exclude=True)

        creative = ExtendedCreative(
            creative_id="c1",
            name="Test Creative",
            format_id=FormatId(agent_url="https://example.com", id="banner-300x250"),
            assets={},
            internal_id="internal-123",
        )

        # No cast() needed!
        packages = Packages(
            package_id="pkg1",
            creatives=[creative],  # type: ignore[list-item]
        )

        assert len(packages.creatives) == 1
        assert packages.creatives[0].creative_id == "c1"


class TestSerializationRoundtrip:
    """Test that coerced values serialize correctly."""

    def test_enum_serializes_as_string(self):
        """Coerced enum values serialize as strings in JSON."""
        req = ListCreativeFormatsRequest(type="video")
        data = req.model_dump(mode="json")
        assert data["type"] == "video"  # Enum serializes to its value

    def test_enum_list_serializes_as_strings(self):
        """Coerced enum list values serialize as string list in JSON."""
        req = ListCreativeFormatsRequest(asset_types=["image", "video"])
        data = req.model_dump(mode="json")
        assert data["asset_types"] == ["image", "video"]

    def test_context_serializes_correctly(self):
        """Coerced context dict serializes correctly."""
        req = ListCreativeFormatsRequest(context={"user_id": "123"})
        data = req.model_dump()
        assert data["context"] == {"user_id": "123"}

    def test_full_request_roundtrip(self):
        """Full request with coerced values can roundtrip through JSON."""
        req = ListCreativeFormatsRequest(
            type="video",
            asset_types=["image", "html"],
            context={"key": "value"},
            name_search="test",
        )
        json_str = req.model_dump_json()
        restored = ListCreativeFormatsRequest.model_validate_json(json_str)
        assert restored.type == FormatCategory.video
        assert restored.asset_types == [AssetContentType.image, AssetContentType.html]
        assert restored.context.key == "value"
        assert restored.name_search == "test"


class TestResponseTypeCoercion:
    """Test that response types accept flexible input types.

    These tests verify the ergonomics improvements from GitHub issue #105,
    which extends type coercion from request types to response types.
    """

    def test_list_creative_formats_response_accepts_dict_context(self):
        """ListCreativeFormatsResponse.context accepts dict."""
        from adcp.types import Format, FormatCategory, ListCreativeFormatsResponse

        format_obj = Format(
            format_id={"agent_url": "https://example.com", "id": "banner-300x250"},
            name="Banner 300x250",
            type=FormatCategory.display,
        )

        response = ListCreativeFormatsResponse(
            formats=[format_obj],
            context={"request_id": "456"},
        )
        assert isinstance(response.context, ContextObject)
        assert response.context.request_id == "456"

    def test_list_creative_formats_response_accepts_format_subclass(self):
        """ListCreativeFormatsResponse.formats accepts Format subclass instances."""
        from pydantic import Field

        from adcp.types import Format, FormatCategory, ListCreativeFormatsResponse

        class ExtendedFormat(Format):
            """Extended with internal tracking fields."""

            internal_id: str | None = Field(None, exclude=True)

        format_obj = ExtendedFormat(
            format_id={"agent_url": "https://example.com", "id": "banner-300x250"},
            name="Banner 300x250",
            type=FormatCategory.display,
            internal_id="format-internal-123",
        )

        # No cast() needed!
        response = ListCreativeFormatsResponse(
            formats=[format_obj],  # type: ignore[list-item]  # Ignoring due to Python list covariance limitation
        )

        assert len(response.formats) == 1
        assert response.formats[0].name == "Banner 300x250"
        # Internal field is preserved at runtime
        assert response.formats[0].internal_id == "format-internal-123"  # type: ignore[attr-defined]

    def test_create_media_buy_response_accepts_package_subclass(self):
        """CreateMediaBuySuccessResponse.packages accepts Package subclass instances."""
        from pydantic import Field

        from adcp.types import CreateMediaBuySuccessResponse, Package

        class ExtendedPackage(Package):
            """Extended with internal tracking fields."""

            campaign_id: str | None = Field(None, exclude=True)

        package = ExtendedPackage(
            package_id="pkg1",
            campaign_id="campaign-456",
        )

        # No cast() needed!
        response = CreateMediaBuySuccessResponse(
            media_buy_id="mb1",
            buyer_ref="buyer-ref",
            packages=[package],  # type: ignore[list-item]  # Ignoring due to Python list covariance limitation
        )

        assert len(response.packages) == 1
        assert response.packages[0].package_id == "pkg1"
        # Internal field is preserved at runtime
        assert response.packages[0].campaign_id == "campaign-456"  # type: ignore[attr-defined]

    def test_get_media_buy_delivery_response_accepts_dict_context(self):
        """GetMediaBuyDeliveryResponse.context accepts dict."""
        from datetime import datetime, timezone

        from adcp.types import GetMediaBuyDeliveryResponse, MediaBuyDelivery

        delivery = MediaBuyDelivery(
            media_buy_id="mb1",
            status="active",
            by_package=[
                {
                    "package_id": "pkg1",
                    "currency": "USD",
                    "pricing_model": "cpm",
                    "rate": 10.0,
                    "impressions": 1000,
                    "spend": 10.0,
                }
            ],
            totals={"impressions": 1000, "spend": 10.0},
        )

        response = GetMediaBuyDeliveryResponse(
            currency="USD",
            reporting_period={
                "start": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "end": datetime(2024, 1, 31, tzinfo=timezone.utc),
            },
            media_buy_deliveries=[delivery],
            context={"request_id": "789"},
        )
        assert isinstance(response.context, ContextObject)
        assert response.context.request_id == "789"

    def test_response_serialization_roundtrip(self):
        """Response types with coerced values can roundtrip through JSON."""
        from adcp.types import Format, FormatCategory, ListCreativeFormatsResponse

        format_obj = Format(
            format_id={"agent_url": "https://example.com", "id": "banner-300x250"},
            name="Banner 300x250",
            type=FormatCategory.display,
        )

        response = ListCreativeFormatsResponse(
            formats=[format_obj],
            context={"key": "value"},
        )

        json_str = response.model_dump_json()
        restored = ListCreativeFormatsResponse.model_validate_json(json_str)

        assert len(restored.formats) == 1
        assert restored.formats[0].name == "Banner 300x250"
        assert restored.context.key == "value"

    def test_get_products_response_accepts_product_subclass(self):
        """GetProductsResponse.products accepts Product subclass instances."""
        from pydantic import Field

        from adcp.types import (
            CpmFixedRatePricingOption,
            DeliveryType,
            FormatId,
            GetProductsResponse,
            Product,
            PublisherPropertiesAll,
        )
        from adcp.types.generated_poc.core.product import DeliveryMeasurement

        class ExtendedProduct(Product):
            """Extended with internal tracking fields."""

            internal_sku: str | None = Field(None, exclude=True)

        product = ExtendedProduct(
            product_id="prod-123",
            name="Premium Display",
            description="A premium display product",
            delivery_type=DeliveryType.guaranteed,
            delivery_measurement=DeliveryMeasurement(provider="Test Provider"),
            format_ids=[FormatId(agent_url="https://example.com", id="banner-300x250")],
            pricing_options=[
                CpmFixedRatePricingOption(
                    currency="USD",
                    pricing_option_id="opt-1",
                    rate=5.0,
                    is_fixed=True,
                    pricing_model="cpm",
                )
            ],
            publisher_properties=[
                PublisherPropertiesAll(
                    publisher_domain="example.com",
                    selection_type="all",
                )
            ],
            internal_sku="SKU-12345",
        )

        # No cast() needed!
        response = GetProductsResponse(
            products=[product],  # type: ignore[list-item]  # Ignoring due to Python list covariance limitation
        )

        assert len(response.products) == 1
        assert response.products[0].product_id == "prod-123"
        # Internal field is preserved at runtime
        assert response.products[0].internal_sku == "SKU-12345"  # type: ignore[attr-defined]

    def test_response_errors_accepts_error_subclass(self):
        """Response types with errors field accept Error subclass instances."""
        from pydantic import Field

        from adcp.types import Error, GetProductsResponse

        class ExtendedError(Error):
            """Extended with internal tracking fields."""

            internal_trace_id: str | None = Field(None, exclude=True)

        error = ExtendedError(
            code="INVALID_REQUEST",
            message="Product ID is required",
            internal_trace_id="trace-abc-123",
        )

        response = GetProductsResponse(
            products=[],
            errors=[error],  # type: ignore[list-item]  # Ignoring due to Python list covariance limitation
        )

        assert len(response.errors) == 1
        assert response.errors[0].code == "INVALID_REQUEST"
        # Internal field is preserved at runtime
        assert response.errors[0].internal_trace_id == "trace-abc-123"  # type: ignore[attr-defined]
