"""Pure, opt-in normalization of pre-v3 creative asset dictionaries."""

from collections.abc import Mapping
from functools import cache
from typing import Any, get_args

from pydantic import RootModel


@cache
def _known_asset_types() -> frozenset[str]:
    """Every ``asset_type`` the pinned schema's canonical asset union routes.

    Derived from the generated ``AssetVariant`` arms rather than listed here,
    so a schema bump that adds an asset type (as 3.2 added ``display_tag``)
    extends key-name inference in the same regeneration. Imported on first use:
    the generated asset graph is not needed to import the legacy helpers.
    """
    from adcp.types.domains.core.assets.asset_union import AssetVariant

    union = get_args(AssetVariant)[0]
    types: set[str] = set()
    for arm in get_args(union):
        # A multi-shape asset (``vast``, ``display_tag``, ...) is a RootModel
        # over per-shape classes that share one ``asset_type`` literal.
        shapes = (
            get_args(arm.model_fields["root"].annotation) if issubclass(arm, RootModel) else (arm,)
        )
        for shape in shapes:
            types.update(get_args(shape.model_fields["asset_type"].annotation))
    return frozenset(types)


def infer_asset_type(asset_key: str, asset: Mapping[str, Any]) -> str | None:
    """Infer a missing discriminator from an exact key, then field presence.

    Keys naming any asset_type the pinned schema declares (``display_tag``,
    ``card``, ...) take precedence over fields. Otherwise URL plus both
    dimensions implies image, URL alone implies url, and content implies text.
    Unknown keys without those fields return None for validation to diagnose.
    This function does not inspect or replace an existing discriminator.
    """
    if asset_key in _known_asset_types():
        return asset_key
    if "url" in asset:
        return "image" if "width" in asset and "height" in asset else "url"
    if "content" in asset:
        return "text"
    return None


def coerce_legacy_asset(asset_key: str, asset: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an asset, infer missing asset_type, and demote dimensionless images.

    An image with a URL but missing either dimension becomes a url asset;
    any remaining width/height is removed. Existing discriminators otherwise
    survive unchanged, including unknown values. This normalizes rather than
    validates: malformed or ambiguous assets still fail normal model validation.
    Nested values are preserved; neither the input mapping nor its values are
    modified.
    """
    out = dict(asset)
    if "asset_type" not in out:
        inferred = infer_asset_type(asset_key, out)
        if inferred is not None:
            out["asset_type"] = inferred
    if out.get("asset_type") == "image" and not ("width" in out and "height" in out):
        if "url" in out:
            out.pop("width", None)
            out.pop("height", None)
            out["asset_type"] = "url"
    return out


def coerce_legacy_assets(assets: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an asset mapping and normalize each dictionary with legacy rules.

    Non-mapping values and unknown keys are retained for model validation.
    Every mapping value is copied, so repeated normalization is idempotent
    without modifying stored rows or caller-owned dictionaries.
    """
    return {
        key: coerce_legacy_asset(key, value) if isinstance(value, Mapping) else value
        for key, value in assets.items()
    }
