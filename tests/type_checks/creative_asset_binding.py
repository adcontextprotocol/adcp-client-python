"""Static contract for the public canonical CreativeAsset binding (issue #1141)."""

from adcp.types import CanonicalFormatKind, CreativeAsset, CreativeManifest, DeliveryCreative
from adcp.types.canonical_creative import CanonicalBoundaryModel


def accepts_canonical_class(model: type[CanonicalBoundaryModel]) -> None:
    pass


accepts_canonical_class(CreativeAsset)

asset = CreativeAsset.model_validate(
    {
        "creative_id": "creative-1",
        "name": "Creative",
        "format_kind": "future_canonical_format",
        "assets": {},
    }
)
format_kind: CanonicalFormatKind | str = asset.format_kind

manifest = CreativeManifest(assets={})
manifest_kind: CanonicalFormatKind | str | None = manifest.format_kind

delivery = DeliveryCreative(
    creative_id="creative-1",
    format_kind="future_canonical_format",
    variants=[],
)
delivery_kind: CanonicalFormatKind | str | None = delivery.format_kind
