"""Regression tests for issue #742: open-union forward compatibility.

Verifies that Format.assets and RepeatableAssetGroup.assets tolerate novel
asset_type values (e.g., 'pixel_tracker') without raising ValidationError,
and that known asset types still parse correctly with full type narrowing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.types import (
    FormatAssetUnion,
    GroupFormatAssetUnion,
    ImageFormatAsset,
    RepeatableAssetGroup,
    UnknownFormatAsset,
    UnknownGroupAsset,
)
from adcp.types.generated_poc.core.format import Assets94, Format
from adcp import FormatId


def _make_format_id(name: str) -> dict:
    return {"agent_url": "https://test.example.com", "id": name}


class TestUnknownFormatAssetParsing:
    """Format.assets accepts novel asset_type values after _forward_compat patch."""

    def test_novel_asset_type_parses_as_unknown(self):
        """A novel asset_type that is not in the SDK parses as UnknownFormatAsset."""
        fmt = Format.model_validate({
            "format_id": _make_format_id("test"),
            "name": "Test",
            "assets": [
                {
                    "asset_id": "impression_tracker",
                    "asset_type": "pixel_tracker",
                    "item_type": "individual",
                    "required": False,
                },
            ],
        })
        assert fmt.assets is not None
        assert len(fmt.assets) == 1
        asset = fmt.assets[0]
        assert isinstance(asset, UnknownFormatAsset)
        assert asset.asset_type == "pixel_tracker"
        assert asset.asset_id == "impression_tracker"

    def test_known_asset_type_still_parses_correctly(self):
        """Known asset types continue to parse as their specific typed class."""
        fmt = Format.model_validate({
            "format_id": _make_format_id("test"),
            "name": "Test",
            "assets": [
                {
                    "asset_id": "hero",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
            ],
        })
        assert fmt.assets is not None
        asset = fmt.assets[0]
        assert isinstance(asset, ImageFormatAsset)
        assert asset.asset_type == "image"

    def test_mixed_known_and_unknown_assets_parse(self):
        """A format with both known and novel asset types parses fully."""
        fmt = Format.model_validate({
            "format_id": _make_format_id("mixed"),
            "name": "Mixed",
            "assets": [
                {
                    "asset_id": "img",
                    "asset_type": "image",
                    "item_type": "individual",
                    "required": True,
                },
                {
                    "asset_id": "tracker",
                    "asset_type": "pixel_tracker",
                    "item_type": "individual",
                    "required": False,
                },
                {
                    "asset_id": "headline",
                    "asset_type": "text",
                    "item_type": "individual",
                    "required": True,
                },
            ],
        })
        assert fmt.assets is not None
        assert len(fmt.assets) == 3
        assert isinstance(fmt.assets[0], ImageFormatAsset)
        assert isinstance(fmt.assets[1], UnknownFormatAsset)
        assert fmt.assets[1].asset_type == "pixel_tracker"

    def test_unknown_asset_preserves_extra_fields(self):
        """Extra fields on unknown assets are preserved via extra='allow'."""
        fmt = Format.model_validate({
            "format_id": _make_format_id("test"),
            "name": "Test",
            "assets": [
                {
                    "asset_id": "track",
                    "asset_type": "pixel_tracker",
                    "item_type": "individual",
                    "required": False,
                    "tracking_url": "https://example.com/track",
                    "fire_on": "impression",
                },
            ],
        })
        asset = fmt.assets[0]  # type: ignore[index]
        assert isinstance(asset, UnknownFormatAsset)
        extra = asset.__pydantic_extra__ or {}
        assert extra.get("tracking_url") == "https://example.com/track"
        assert extra.get("fire_on") == "impression"

    def test_repeatable_group_still_routes_correctly(self):
        """RepeatableAssetGroup (item_type='repeatable_group') still parses."""
        fmt = Format.model_validate({
            "format_id": _make_format_id("carousel"),
            "name": "Carousel",
            "assets": [
                {
                    "asset_group_id": "slide",
                    "item_type": "repeatable_group",
                    "required": True,
                    "min_count": 2,
                    "max_count": 10,
                    "assets": [],
                },
            ],
        })
        assert fmt.assets is not None
        group = fmt.assets[0]
        assert isinstance(group, RepeatableAssetGroup)

    def test_multiple_novel_asset_types_no_validation_error(self):
        """Multiple assets with novel asset_types all parse without error."""
        assets_data = [
            {
                "asset_id": f"slot_{i}",
                "asset_type": f"novel_type_{i}",
                "item_type": "individual",
                "required": False,
            }
            for i in range(10)
        ]
        fmt = Format.model_validate({
            "format_id": _make_format_id("test"),
            "name": "Test",
            "assets": assets_data,
        })
        assert fmt.assets is not None
        assert len(fmt.assets) == 10
        for asset in fmt.assets:
            assert isinstance(asset, UnknownFormatAsset)


class TestUnknownGroupAssetParsing:
    """Assets94.assets (RepeatableAssetGroup) also tolerates novel asset_type."""

    def test_novel_group_asset_parses_as_unknown(self):
        """A novel asset_type inside a repeatable group parses as UnknownGroupAsset."""
        group = Assets94.model_validate({
            "asset_group_id": "slide",
            "item_type": "repeatable_group",
            "required": True,
            "min_count": 1,
            "max_count": 5,
            "assets": [
                {
                    "asset_id": "track",
                    "asset_type": "pixel_tracker",
                    "required": False,
                },
            ],
        })
        assert len(group.assets) == 1
        assert isinstance(group.assets[0], UnknownGroupAsset)
        assert group.assets[0].asset_type == "pixel_tracker"

    def test_known_group_asset_still_parses(self):
        """Known asset_type inside a group still routes to the typed class."""
        from adcp.types import ImageFormatGroupAsset

        group = Assets94.model_validate({
            "asset_group_id": "product",
            "item_type": "repeatable_group",
            "required": True,
            "min_count": 1,
            "max_count": 10,
            "assets": [
                {
                    "asset_id": "img",
                    "asset_type": "image",
                    "required": True,
                },
            ],
        })
        assert isinstance(group.assets[0], ImageFormatGroupAsset)


class TestUnknownFormatAssetContract:
    """Structural invariants on the fallback type."""

    def test_requires_asset_id(self):
        """asset_id is required (inherited from BaseIndividualAsset)."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate({
                "asset_type": "pixel_tracker",
                "item_type": "individual",
                "required": False,
                # asset_id missing
            })

    def test_requires_required_field(self):
        """required is required (inherited from BaseIndividualAsset)."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate({
                "asset_id": "track",
                "asset_type": "pixel_tracker",
                "item_type": "individual",
                # required missing
            })

    def test_asset_type_is_required_not_empty_default(self):
        """asset_type has no default — a missing discriminator is a real error."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate({
                "asset_id": "track",
                "item_type": "individual",
                "required": False,
                # asset_type missing
            })

    def test_unknown_format_asset_is_unknown_form(self):
        """UnknownFormatAsset is not a known asset type by construction."""
        asset = UnknownFormatAsset.model_validate({
            "asset_id": "x",
            "asset_type": "pixel_tracker",
            "item_type": "individual",
            "required": False,
        })
        assert asset.asset_type not in (
            "image", "video", "audio", "text", "markdown", "html",
            "css", "javascript", "vast", "daast", "url", "webhook",
            "brief", "catalog",
        )


class TestPublicAPIExports:
    """New types are accessible from the public surface."""

    def test_imports_from_adcp_types(self):
        from adcp.types import (
            FormatAssetUnion,
            GroupFormatAssetUnion,
            UnknownFormatAsset,
            UnknownGroupAsset,
        )
        assert UnknownFormatAsset is not None
        assert UnknownGroupAsset is not None
        assert FormatAssetUnion is not None
        assert GroupFormatAssetUnion is not None
