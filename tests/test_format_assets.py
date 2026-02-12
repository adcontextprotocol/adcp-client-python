"""Tests for format asset utilities.

These utilities provide access to format assets from the `assets` field.
"""

import pytest

from adcp import Format, FormatCategory, FormatId
from adcp.utils.format_assets import (
    get_asset_count,
    get_format_assets,
    get_individual_assets,
    get_optional_assets,
    get_repeatable_groups,
    get_required_assets,
    has_assets,
    normalize_assets_required,
    uses_deprecated_assets_field,
)


def make_format_id(format_name: str) -> FormatId:
    """Create a test format ID."""
    return FormatId(agent_url="https://test-agent.example.com", id=format_name)


def get_asset_id(asset) -> str:
    """Get asset_id or asset_group_id from asset."""
    return getattr(asset, "asset_id", None) or getattr(asset, "asset_group_id", None)


def get_asset_ids(assets) -> list[str]:
    """Get list of asset_ids from assets."""
    return [get_asset_id(a) for a in assets]


class TestGetFormatAssets:
    """Tests for get_format_assets() utility."""

    def test_returns_assets_from_format(self):
        """Should return assets from the format's assets field."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
            ],
        )
        assets = get_format_assets(fmt)
        assert len(assets) == 1
        assert get_asset_id(assets[0]) == "img"

    def test_returns_empty_list_when_no_assets(self):
        """Should return empty list when format has no assets."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
        )
        assets = get_format_assets(fmt)
        assert assets == []


class TestNormalizeAssetsRequired:
    """Tests for normalize_assets_required() utility (deprecated)."""

    def test_adds_required_true_to_dict_assets(self):
        """Should add required=True to dict assets and emit deprecation warning."""
        assets_required = [
            {"asset_id": "img", "asset_type": "image", "item_type": "individual"},
        ]
        with pytest.warns(DeprecationWarning, match="normalize_assets_required"):
            normalized = normalize_assets_required(assets_required)
        assert len(normalized) == 1
        assert normalized[0].required is True
        assert get_asset_id(normalized[0]) == "img"

    def test_preserves_other_fields(self):
        """Should preserve all other fields when normalizing."""
        assets_required = [
            {
                "asset_id": "img",
                "asset_type": "image",
                "item_type": "individual",
                "requirements": {"width": 300, "height": 250},
            },
        ]
        with pytest.warns(DeprecationWarning, match="normalize_assets_required"):
            normalized = normalize_assets_required(assets_required)
        # normalize_assets_required returns Pydantic models
        req = normalized[0].requirements
        assert req.width == 300
        assert req.height == 250

    def test_normalizes_repeatable_groups(self):
        """Should normalize repeatable groups by mapping asset_group_id to asset_id."""
        assets_required = [
            {
                "asset_group_id": "products",
                "item_type": "individual",
                "min_count": 2,
                "max_count": 10,
                "assets": [
                    {
                        "asset_id": "product_img",
                        "asset_type": "image",
                        "item_type": "individual",
                        "required": True,
                    },
                ],
            },
        ]
        with pytest.warns(DeprecationWarning, match="normalize_assets_required"):
            normalized = normalize_assets_required(assets_required)
        assert len(normalized) == 1
        assert normalized[0].required is True
        assert normalized[0].asset_id == "products"


class TestGetRequiredAssets:
    """Tests for get_required_assets() utility."""

    def test_filters_to_required_only(self):
        """Should return only assets with required=True."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "required_img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
                {
                    "asset_id": "optional_logo",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": False,
                },
                {
                    "asset_id": "required_url",
                    "asset_type": "url",
                    "item_type": "individual",
                    "required": True,
                },
            ],
        )
        required = get_required_assets(fmt)
        assert len(required) == 2
        ids = get_asset_ids(required)
        assert "required_img" in ids
        assert "required_url" in ids
        assert "optional_logo" not in ids

    def test_empty_when_no_required_assets(self):
        """Should return empty list when no required assets."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "optional_img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": False,
                },
            ],
        )
        required = get_required_assets(fmt)
        assert len(required) == 0


class TestGetOptionalAssets:
    """Tests for get_optional_assets() utility."""

    def test_filters_to_optional_only(self):
        """Should return only assets with required=False."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "required_img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
                {
                    "asset_id": "optional_logo",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": False,
                },
            ],
        )
        optional = get_optional_assets(fmt)
        assert len(optional) == 1
        assert get_asset_id(optional[0]) == "optional_logo"

    def test_empty_when_all_required(self):
        """Should return empty when all assets are required."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
            ],
        )
        optional = get_optional_assets(fmt)
        assert optional == []


class TestGetIndividualAssets:
    """Tests for get_individual_assets() utility."""

    def test_filters_to_individual_only(self):
        """Should return only individual assets (not groups)."""
        fmt = Format(
            format_id=make_format_id("carousel"),
            name="Carousel",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "headline",
                    "asset_type": "text",
                    "item_type": "individual",
                    "required": True,
                },
                {
                    "asset_group_id": "product",
                    "item_type": "repeatable_group",
                    "required": True,
                    "min_count": 2,
                    "max_count": 10,
                    "assets": [],
                },
            ],
        )
        individual = get_individual_assets(fmt)
        assert len(individual) == 1
        assert get_asset_id(individual[0]) == "headline"


class TestGetRepeatableGroups:
    """Tests for get_repeatable_groups() utility."""

    def test_filters_to_groups_only(self):
        """Should return only repeatable groups."""
        fmt = Format(
            format_id=make_format_id("carousel"),
            name="Carousel",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "headline",
                    "asset_type": "text",
                    "item_type": "individual",
                    "required": True,
                },
                {
                    "asset_group_id": "product",
                    "item_type": "repeatable_group",
                    "required": True,
                    "min_count": 2,
                    "max_count": 10,
                    "assets": [],
                },
            ],
        )
        groups = get_repeatable_groups(fmt)
        assert len(groups) == 1
        assert get_asset_id(groups[0]) == "product"


class TestUsesDeprecatedAssetsField:
    """Tests for uses_deprecated_assets_field() utility (deprecated).

    This function always returns False and emits a deprecation warning.
    """

    def test_false_when_using_assets_with_deprecation_warning(self):
        """Should return False and emit deprecation warning."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
            ],
        )
        with pytest.warns(DeprecationWarning, match="uses_deprecated_assets_field"):
            result = uses_deprecated_assets_field(fmt)
        assert result is False

    def test_false_when_no_assets_with_deprecation_warning(self):
        """Should return False and emit deprecation warning even with no assets."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
        )
        with pytest.warns(DeprecationWarning, match="uses_deprecated_assets_field"):
            result = uses_deprecated_assets_field(fmt)
        assert result is False


class TestGetAssetCount:
    """Tests for get_asset_count() utility."""

    def test_counts_all_assets(self):
        """Should count all assets."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "img1",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
                {
                    "asset_id": "img2",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": False,
                },
            ],
        )
        assert get_asset_count(fmt) == 2

    def test_zero_when_no_assets(self):
        """Should return 0 when no assets."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
        )
        assert get_asset_count(fmt) == 0


class TestHasAssets:
    """Tests for has_assets() utility."""

    def test_true_when_has_assets(self):
        """Should return True when format has assets."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
            assets=[
                {
                    "asset_id": "img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
            ],
        )
        assert has_assets(fmt) is True

    def test_false_when_no_assets(self):
        """Should return False when format has no assets."""
        fmt = Format(
            format_id=make_format_id("test"),
            name="Test",
            type=FormatCategory.display,
        )
        assert has_assets(fmt) is False


class TestPublicImports:
    """Tests for public API imports."""

    def test_can_import_from_adcp(self):
        """Should be able to import utilities from main adcp package."""
        from adcp import (
            get_asset_count,
            get_format_assets,
            get_individual_assets,
            get_optional_assets,
            get_repeatable_groups,
            get_required_assets,
            has_assets,
            normalize_assets_required,
            uses_deprecated_assets_field,
        )

        # All should be callable
        assert callable(get_format_assets)
        assert callable(get_required_assets)
        assert callable(get_optional_assets)
        assert callable(get_individual_assets)
        assert callable(get_repeatable_groups)
        assert callable(uses_deprecated_assets_field)
        assert callable(normalize_assets_required)
        assert callable(get_asset_count)
        assert callable(has_assets)
