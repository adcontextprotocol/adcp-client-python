"""Public canonical model identity and closed format-kind types (#1141/#1241)."""

from typing_extensions import assert_type

from adcp.types import (
    CanonicalFormatKind,
    Creative,
    CreativeAsset,
    CreativeManifest,
    DeliveryCreative,
    GetCreativeDeliveryResponse,
)
from adcp.types.canonical_creative import CanonicalBoundaryModel


def accepts_canonical_class(model: type[CanonicalBoundaryModel]) -> None:
    pass


accepts_canonical_class(CreativeAsset)

asset = CreativeAsset.model_validate(
    {
        "creative_id": "creative-1",
        "name": "Creative",
        "format_kind": "image",
        "assets": {},
    }
)
assert_type(asset.format_kind, CanonicalFormatKind)


def listed_kind(creative: Creative) -> CanonicalFormatKind:
    assert_type(creative.format_kind, CanonicalFormatKind)
    return creative.format_kind


manifest = CreativeManifest(assets={})
assert_type(manifest.format_kind, CanonicalFormatKind | None)

delivery = DeliveryCreative(
    creative_id="creative-1",
    format_kind="future_canonical_format",
    variants=[],
)
assert_type(delivery.format_kind, CanonicalFormatKind | str | None)


def check_served_manifest(response: GetCreativeDeliveryResponse) -> None:
    for creative in response.creatives:
        for variant in creative.variants:
            if variant.manifest is not None:
                assert_type(variant.manifest.format_kind, CanonicalFormatKind | str | None)
