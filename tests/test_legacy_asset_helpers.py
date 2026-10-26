"""Public legacy normalization is opt-in and identical across adapter paths."""

from copy import deepcopy
from importlib import import_module
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from adcp.compat import legacy
from adcp.types.legacy import (
    LegacyCreativeAsset,
    coerce_legacy_asset,
    coerce_legacy_assets,
    infer_asset_type,
)


@pytest.mark.parametrize(
    ("key", "asset", "inferred", "normalized"),
    [
        ("text", {"url": "https://cdn.example/a"}, "text", {"asset_type": "text"}),
        (
            "hero_image",
            {"url": "https://cdn.example/a", "width": 3, "height": 2},
            "image",
            {"asset_type": "image", "width": 3, "height": 2},
        ),
        ("image", {"url": "https://cdn.example/a", "width": 3}, "image", {"asset_type": "url"}),
        ("image", {"url": "https://cdn.example/a", "height": 2}, "image", {"asset_type": "url"}),
        ("image", {"url": "https://cdn.example/a"}, "image", {"asset_type": "url"}),
        ("image", {"content": "invalid"}, "image", {"asset_type": "image"}),
        ("headline", {"content": "hello"}, "text", {"asset_type": "text"}),
        (
            "other",
            {"url": "https://cdn.example/a", "content": "hello"},
            "url",
            {"asset_type": "url"},
        ),
        ("hero_image", {"unknown": 1}, None, {}),
        (
            "image",
            {"asset_type": "video", "url": "https://cdn.example/a"},
            "image",
            {"asset_type": "video"},
        ),
        ("other", {"asset_type": "future", "unknown": 1}, None, {"asset_type": "future"}),
        ("text", {"asset_type": None, "content": "hello"}, "text", {"asset_type": None}),
    ],
)
def test_rules_match_both_adapters(key, asset, inferred, normalized):
    original = deepcopy(asset)
    expected = {**asset, **normalized}
    if normalized.get("asset_type") == "url" and asset.get("asset_type", key) == "image":
        expected.pop("width", None)
        expected.pop("height", None)
    assert infer_asset_type(key, MappingProxyType(asset)) == inferred
    result = coerce_legacy_asset(key, MappingProxyType(asset))
    assert result == expected
    assert result is not asset
    assert coerce_legacy_asset(key, result) == result
    for module in ("adcp.server.spec_compat", "adcp.compat.legacy.v2_5.sync_creatives"):
        adapter = import_module(module)
        assert adapter._infer_asset_type(key, asset) == inferred
        assert adapter._coerce_asset(key, asset) == result
    assert asset == original


def test_display_tag_key_is_inferred_as_display_tag():
    # #1624: a 3.2 display_tag asset was inferred as text from its content.
    tag = "<script src='https://ads.example/tag.js'></script>"
    assert coerce_legacy_assets({"display_tag": {"content": tag}}) == {
        "display_tag": {"content": tag, "asset_type": "display_tag"}
    }
    assert coerce_legacy_assets({"display_tag": {"url": "https://ads.example/tag"}}) == {
        "display_tag": {"url": "https://ads.example/tag", "asset_type": "display_tag"}
    }


def test_key_inference_covers_every_asset_type_the_schema_declares():
    from adcp.types.domains.core import format as format_module

    # The format slot union derives its own vocabulary from the same pin; the
    # creative-asset vocabulary key inference uses must not lag behind it.
    declared = format_module._KNOWN_BASE_INDIVIDUAL_ASSET_TYPES
    assert "display_tag" in declared
    for asset_type in declared:
        assert infer_asset_type(asset_type, {"content": "x"}) == asset_type
        assert infer_asset_type(asset_type, {"url": "https://cdn.example/a"}) == asset_type


def test_public_reexports_and_mapping_immutability():
    assert legacy.infer_asset_type is infer_asset_type
    assert legacy.coerce_legacy_asset is coerce_legacy_asset
    assert legacy.coerce_legacy_assets is coerce_legacy_assets
    assets = {"headline": {"content": "Hello", "extra": {"value": 1}}, "malformed": 42}
    original = deepcopy(assets)
    result = coerce_legacy_assets(MappingProxyType(assets))
    assert result["headline"]["asset_type"] == "text"
    assert result["headline"] is not assets["headline"]
    assert result["headline"]["extra"] == {"value": 1}
    assert result["malformed"] == 42
    assert assets == original


def test_strict_construction_stays_default():
    assets = {
        "banner": {"url": "https://cdn.example/b.png", "width": 300, "height": 250},
        "headline": {"content": "Hello"},
    }
    data = {
        "creative_id": "c1",
        "name": "c1",
        "format_id": {
            "agent_url": "https://creative.adcontextprotocol.org",
            "id": "display_300x250",
        },
    }
    with pytest.raises(ValidationError, match="Unable to extract tag"):
        LegacyCreativeAsset(**data, assets=assets)
    creative = LegacyCreativeAsset(**data, assets=coerce_legacy_assets(assets))
    wire = creative.model_dump(mode="json")
    assert wire["assets"]["banner"]["asset_type"] == "image"
    assert wire["assets"]["headline"]["asset_type"] == "text"
    assert "asset_type" not in assets["banner"]
