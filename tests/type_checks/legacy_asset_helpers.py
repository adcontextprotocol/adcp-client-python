"""Typed, opt-in normalization without changing strict constructors."""

from collections.abc import Mapping
from typing import Any

from typing_extensions import assert_type

from adcp.compat.legacy import coerce_legacy_asset
from adcp.types.legacy import coerce_legacy_assets, infer_asset_type


def normalize(assets: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    assert_type(infer_asset_type("headline", assets["headline"]), str | None)
    assert_type(coerce_legacy_asset("headline", assets["headline"]), dict[str, Any])
    return coerce_legacy_assets(assets)
