"""Public canonical model identity and the open ``format_kind`` type (#1141/#1241).

``format_kind`` is a ``str`` at every reference and no model refuses a value,
because the schema requires a consumer to retain an unknown one and a pinned
SDK cannot tell a kind a seller invented from a kind defined after its pin.
Checking the vocabulary is the caller's, through
``adcp.types.is_canonical_format_kind``. See
:mod:`adcp.types.canonical_creative`'s module docstring and adcp#7929.

One consequence is visible below: a field that had to be asserted ``Any``
because no annotation could express a widening union now has a real static
type, since there is no widening left.
"""

from collections.abc import Sequence

from typing_extensions import assert_type

from adcp.types import (
    Creative,
    CreativeAsset,
    CreativeManifest,
    DeliveryCreative,
    GetCreativeDeliveryResponse,
)
from adcp.types.canonical_creative import CanonicalBoundaryModel, _DeliveryCreativeVariant


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
assert_type(asset.format_kind, str)


def listed_kind(creative: Creative) -> str:
    assert_type(creative.format_kind, str)
    return creative.format_kind


manifest = CreativeManifest(assets={})
assert_type(manifest.format_kind, str | None)

delivery = DeliveryCreative(
    creative_id="creative-1",
    format_kind="future_canonical_format",
    variants=[],
)
assert_type(delivery.format_kind, str | None)


def check_served_manifest(response: GetCreativeDeliveryResponse) -> None:
    for creative in response.creatives:
        # ``variants`` narrows the generated ``list[CreativeVariant]`` to the
        # tolerant delivery row, which pydantic accepts and mypy's invariant
        # list rule does not — the library declares the precise list and
        # suppresses the override on its side (#1416), so the field reads as
        # ``list[_DeliveryCreativeVariant]`` here under mypy and pyright alike.
        # The row is module-private because it is a readback shape no adopter
        # constructs, which is why the type is spelled here rather than
        # imported from ``adcp.types``.
        assert_type(creative.variants, list[_DeliveryCreativeVariant])
        variants: Sequence[_DeliveryCreativeVariant] = creative.variants
        for variant in variants:
            if variant.manifest is not None:
                # This used to read ``Any``: the tolerant manifest widened the
                # generated ``CanonicalFormatKind | None`` to admit an unknown
                # kind, no annotation expresses a widening override, so the
                # field had to be ``SchemaVariant``. With one open type there is
                # no widening, the readback manifest redeclares nothing, and the
                # inherited annotation is visible and assertable.
                assert_type(variant.manifest.format_kind, str | None)
