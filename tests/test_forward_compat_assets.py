"""Regression tests for issue #742: open-union forward compatibility.

Verifies that Format.assets and RepeatableAssetGroup.assets tolerate novel
asset_type values (e.g., 'pixel_tracker') without raising ValidationError,
and that known asset types still parse correctly with full type narrowing.
"""

from __future__ import annotations

import json
import warnings

import pytest
from pydantic import RootModel, ValidationError

from adcp.types import (
    FormatAssetUnion,
    GroupFormatAssetUnion,
    ImageFormatAsset,
    RepeatableAssetGroup,
    UnknownFormatAsset,
    UnknownGroupAsset,
)
from adcp.types.generated_poc.core.format import Assets94, Format


def _make_format_id(name: str) -> dict:
    return {"agent_url": "https://test.example.com", "id": name}


class TestUnknownFormatAssetParsing:
    """Format.assets accepts novel asset_type values after _forward_compat patch."""

    def test_novel_asset_type_parses_as_unknown(self):
        """A novel asset_type that is not in the SDK parses as UnknownFormatAsset."""
        fmt = Format.model_validate(
            {
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
            }
        )
        assert fmt.assets is not None
        assert len(fmt.assets) == 1
        asset = fmt.assets[0]
        assert isinstance(asset, UnknownFormatAsset)
        assert asset.asset_type == "pixel_tracker"
        assert asset.asset_id == "impression_tracker"

    def test_known_asset_type_still_parses_correctly(self):
        """Known asset types continue to parse as their specific typed class."""
        fmt = Format.model_validate(
            {
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
            }
        )
        assert fmt.assets is not None
        asset = fmt.assets[0]
        assert isinstance(asset, ImageFormatAsset)
        assert asset.asset_type == "image"

    def test_mixed_known_and_unknown_assets_parse(self):
        """A format with both known and novel asset types parses fully."""
        fmt = Format.model_validate(
            {
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
            }
        )
        assert fmt.assets is not None
        assert len(fmt.assets) == 3
        assert isinstance(fmt.assets[0], ImageFormatAsset)
        assert isinstance(fmt.assets[1], UnknownFormatAsset)
        assert fmt.assets[1].asset_type == "pixel_tracker"

    def test_unknown_asset_preserves_extra_fields(self):
        """Extra fields on unknown assets are preserved via extra='allow'."""
        fmt = Format.model_validate(
            {
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
            }
        )
        asset = fmt.assets[0]  # type: ignore[index]
        assert isinstance(asset, UnknownFormatAsset)
        extra = asset.__pydantic_extra__ or {}
        assert extra.get("tracking_url") == "https://example.com/track"
        assert extra.get("fire_on") == "impression"

    def test_repeatable_group_still_routes_correctly(self):
        """RepeatableAssetGroup (item_type='repeatable_group') still parses."""
        fmt = Format.model_validate(
            {
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
            }
        )
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
        fmt = Format.model_validate(
            {
                "format_id": _make_format_id("test"),
                "name": "Test",
                "assets": assets_data,
            }
        )
        assert fmt.assets is not None
        assert len(fmt.assets) == 10
        for asset in fmt.assets:
            assert isinstance(asset, UnknownFormatAsset)


class TestUnknownGroupAssetParsing:
    """Assets94.assets (RepeatableAssetGroup) also tolerates novel asset_type."""

    def test_novel_group_asset_parses_as_unknown(self):
        """A novel asset_type inside a repeatable group parses as UnknownGroupAsset."""
        group = Assets94.model_validate(
            {
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
            }
        )
        assert len(group.assets) == 1
        assert isinstance(group.assets[0], UnknownGroupAsset)
        assert group.assets[0].asset_type == "pixel_tracker"

    def test_known_group_asset_still_parses(self):
        """Known asset_type inside a group still routes to the typed class."""
        from adcp.types import ImageFormatGroupAsset

        group = Assets94.model_validate(
            {
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
            }
        )
        assert isinstance(group.assets[0], ImageFormatGroupAsset)


class TestUnknownFormatAssetContract:
    """Structural invariants on the fallback type."""

    def test_requires_asset_id(self):
        """asset_id is required (inherited from BaseIndividualAsset)."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate(
                {
                    "asset_type": "pixel_tracker",
                    "item_type": "individual",
                    "required": False,
                    # asset_id missing
                }
            )

    def test_requires_required_field(self):
        """required is required (inherited from BaseIndividualAsset)."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate(
                {
                    "asset_id": "track",
                    "asset_type": "pixel_tracker",
                    "item_type": "individual",
                    # required missing
                }
            )

    def test_asset_type_is_required_not_empty_default(self):
        """asset_type has no default — a missing discriminator is a real error."""
        with pytest.raises(ValidationError):
            UnknownFormatAsset.model_validate(
                {
                    "asset_id": "track",
                    "item_type": "individual",
                    "required": False,
                    # asset_type missing
                }
            )

    def test_unknown_format_asset_is_unknown_form(self):
        """UnknownFormatAsset is not a known asset type by construction."""
        asset = UnknownFormatAsset.model_validate(
            {
                "asset_id": "x",
                "asset_type": "pixel_tracker",
                "item_type": "individual",
                "required": False,
            }
        )
        assert asset.asset_type not in (
            "image",
            "video",
            "audio",
            "text",
            "markdown",
            "html",
            "css",
            "javascript",
            "vast",
            "daast",
            "url",
            "webhook",
            "brief",
            "catalog",
        )


class TestPublicAPIExports:
    """New types are accessible from the public surface."""

    def test_imports_from_adcp_types(self):
        from adcp.types import (
            UnknownFormatAsset,
            UnknownGroupAsset,
        )

        assert UnknownFormatAsset is not None
        assert UnknownGroupAsset is not None
        assert FormatAssetUnion is not None
        assert GroupFormatAssetUnion is not None


_NOVEL_INDIVIDUAL_ASSET = {
    "asset_id": "hologram_slot",
    "asset_type": "hologram",
    "item_type": "individual",
    "required": False,
    "hologram_depth_mm": 12,
}
_NOVEL_GROUP_ASSET = {
    "asset_id": "hologram_slot",
    "asset_type": "hologram",
    "required": False,
    "hologram_depth_mm": 12,
}


def _repeatable_group(*assets: dict) -> dict:
    return {
        "asset_group_id": "slide",
        "item_type": "repeatable_group",
        "required": True,
        "min_count": 1,
        "max_count": 5,
        "assets": list(assets),
    }


def _format(*assets: dict) -> dict:
    return {
        "format_id": _make_format_id("novel"),
        "name": "Novel",
        "assets": list(assets),
    }


def _is_generated_group_wrapper(item: object) -> bool:
    """Whether ``item`` is the generated RootModel nested group items arrive in."""
    return isinstance(item, RootModel) and type(item).__module__ == Format.__module__


def _assert_wrapped_novel_group_asset(group: object) -> None:
    """Inside Format, group items keep their generated wrapper, fallback or not."""
    assert isinstance(group, RepeatableAssetGroup)
    item = group.assets[0]
    assert _is_generated_group_wrapper(item)
    slot = item.root
    assert isinstance(slot, UnknownGroupAsset)
    assert slot.asset_type == "hologram"
    assert slot.__pydantic_extra__ == {"hologram_depth_mm": 12}
    # The wrapper proxies attribute access, as it does for declared types.
    assert item.asset_id == "hologram_slot"


def _list_responses() -> list[type]:
    from adcp.types import LegacyListCreativeFormatsResponse
    from adcp.types.generated_poc.creative.list_creative_formats_response import (
        ListCreativeFormatsResponseCreativeAgent,
    )

    return [LegacyListCreativeFormatsResponse, ListCreativeFormatsResponseCreativeAgent]


class TestUndeclaredAssetsParseEverywhere:
    """An asset_type no generated class declares parses on every 8.x path.

    It used to fail in two places. Format embedded the repeatable group as it
    was before the group was patched, so an undeclared asset inside a group
    failed there although the group alone accepted it. The list_creative_formats
    responses were built before either patch and embed the closed generated
    Format, so any undeclared asset failed there, 26 errors at top level and 43
    inside a group. Each closed union now falls back for undeclared asset types
    only; tests/test_forward_compat_assets_baseline.py proves that every
    document 8.x already accepted parses exactly as before.
    """

    def test_inside_a_group_inside_format(self) -> None:
        fmt = Format.model_validate(_format(_repeatable_group(_NOVEL_GROUP_ASSET)))
        assert fmt.assets is not None
        _assert_wrapped_novel_group_asset(fmt.assets[0])

    @pytest.mark.parametrize("response", _list_responses(), ids=lambda r: r.__name__)
    def test_at_top_level_inside_a_list_response(self, response: type) -> None:
        parsed = response.model_validate({"formats": [_format(_NOVEL_INDIVIDUAL_ASSET)]})
        assets = parsed.formats[0].assets
        assert assets is not None
        assert isinstance(assets[0], UnknownFormatAsset)
        assert assets[0].__pydantic_extra__ == {"hologram_depth_mm": 12}

    @pytest.mark.parametrize("response", _list_responses(), ids=lambda r: r.__name__)
    def test_inside_a_group_inside_a_list_response(self, response: type) -> None:
        parsed = response.model_validate_json(
            json.dumps({"formats": [_format(_repeatable_group(_NOVEL_GROUP_ASSET))]})
        )
        assets = parsed.formats[0].assets
        assert assets is not None
        _assert_wrapped_novel_group_asset(assets[0])

    @pytest.mark.parametrize("response", _list_responses(), ids=lambda r: r.__name__)
    def test_round_trips_through_model_dump(self, response: type) -> None:
        document = {
            "formats": [_format(_NOVEL_INDIVIDUAL_ASSET, _repeatable_group(_NOVEL_GROUP_ASSET))]
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            parsed = response.model_validate(document)
            dumped = parsed.model_dump(mode="json", exclude_none=True)
        assets = dumped["formats"][0]["assets"]
        assert assets[0] == {**_NOVEL_INDIVIDUAL_ASSET}
        assert assets[1]["assets"][0] == {**_NOVEL_GROUP_ASSET}
        assert response.model_validate(dumped) == parsed

    async def test_client_list_creative_formats_legacy(self) -> None:
        from unittest.mock import patch

        from adcp import ADCPClient
        from adcp.types import AgentConfig, LegacyListCreativeFormatsRequest, Protocol
        from adcp.types.core import TaskResult, TaskStatus

        client = ADCPClient(
            AgentConfig(id="formats", agent_uri="https://test.example.com", protocol=Protocol.A2A)
        )
        raw = TaskResult(
            status=TaskStatus.COMPLETED,
            data={
                "formats": [_format(_NOVEL_INDIVIDUAL_ASSET, _repeatable_group(_NOVEL_GROUP_ASSET))]
            },
            success=True,
        )
        with (
            patch.object(client.adapter, "list_creative_formats", return_value=raw),
            pytest.warns(DeprecationWarning),
        ):
            result = await client.list_creative_formats_legacy(LegacyListCreativeFormatsRequest())

        assert result.success, result.error
        assert result.data is not None
        assets = result.data.formats[0].assets
        assert assets is not None
        assert isinstance(assets[0], UnknownFormatAsset)
        _assert_wrapped_novel_group_asset(assets[1])


class TestDeclaredAssetsKeepTheir8xShape:
    """The fallbacks never see a declared asset type, on any path.

    Spot checks of what the baseline test proves exhaustively, kept here so a
    failure names the path. The three paths differ, and always have: each must
    stay as it was.
    """

    _PIXEL_TRACKER = {
        "asset_id": "imp",
        "asset_type": "pixel_tracker",
        "item_type": "individual",
        "required": False,
    }

    def test_format_routes_individual_assets_by_its_vocabulary(self) -> None:
        # pixel_tracker is generated but outside the 14-type vocabulary.
        fmt = Format.model_validate(_format(self._PIXEL_TRACKER))
        assert fmt.assets is not None
        assert isinstance(fmt.assets[0], UnknownFormatAsset)

    @pytest.mark.parametrize("response", _list_responses(), ids=lambda r: r.__name__)
    def test_list_responses_keep_the_generated_classes(self, response: type) -> None:
        from adcp.types import LegacyListCreativeFormatsResponse

        parsed = response.model_validate({"formats": [_format(self._PIXEL_TRACKER)]})
        assets = parsed.formats[0].assets
        assert assets is not None
        if response is LegacyListCreativeFormatsResponse:
            # Built before any patch: the closed generated union, every type typed.
            assert not isinstance(assets[0], UnknownFormatAsset)
            assert assets[0].asset_type == "pixel_tracker"
        else:
            # Built lazily, after Format was patched: Format's own routing.
            assert isinstance(assets[0], UnknownFormatAsset)

    @pytest.mark.parametrize("nested", [False, True], ids=["format", "list_response"])
    def test_group_items_inside_format_stay_wrapped(self, nested: bool) -> None:
        from adcp.types import ImageFormatGroupAsset, LegacyListCreativeFormatsResponse

        document = _format(
            _repeatable_group({"asset_id": "card", "asset_type": "image", "required": True})
        )
        if nested:
            parsed = LegacyListCreativeFormatsResponse.model_validate({"formats": [document]})
            assets = parsed.formats[0].assets
        else:
            assets = Format.model_validate(document).assets
        assert assets is not None
        item = assets[0].assets[0]
        assert _is_generated_group_wrapper(item)
        assert type(item.root) is ImageFormatGroupAsset

    def test_group_alone_stays_unwrapped(self) -> None:
        from adcp.types import ImageFormatGroupAsset

        group = Assets94.model_validate(
            _repeatable_group({"asset_id": "card", "asset_type": "image", "required": True})
        )
        assert type(group.assets[0]) is ImageFormatGroupAsset

    def test_declared_but_invalid_asset_still_fails_on_the_closed_union(self) -> None:
        from adcp.types import LegacyListCreativeFormatsResponse

        bad = {**self._PIXEL_TRACKER, "required": "nope"}
        with pytest.raises(ValidationError) as excinfo:
            LegacyListCreativeFormatsResponse.model_validate({"formats": [_format(bad)]})
        assert not any("UnknownFormatAsset" in str(err["loc"]) for err in excinfo.value.errors())
