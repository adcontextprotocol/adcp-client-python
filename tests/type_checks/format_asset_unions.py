"""Adopter-facing type contract for the open format asset unions.

``FormatAssetUnion`` and ``GroupFormatAssetUnion`` are ``Annotated`` aliases, so
they must be usable as type expressions (``list[FormatAssetUnion]``) rather than
only as runtime values.
"""

from pydantic import TypeAdapter

from adcp.types import FormatAssetUnion, GroupFormatAssetUnion, UnknownFormatAsset

assets: list[FormatAssetUnion] = TypeAdapter(list[FormatAssetUnion]).validate_python(
    [
        {
            "item_type": "individual",
            "asset_id": "logo",
            "asset_type": "vendor_widget",
            "required": True,
        }
    ]
)
group_assets: list[GroupFormatAssetUnion] = TypeAdapter(
    list[GroupFormatAssetUnion]
).validate_python([])
unknown: FormatAssetUnion = UnknownFormatAsset(
    item_type="individual", asset_id="logo", asset_type="vendor_widget", required=True
)
