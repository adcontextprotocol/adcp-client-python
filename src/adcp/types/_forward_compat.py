"""Forward-compatibility patches for the AdCP type system.

Patches Format.assets and RepeatableAssetGroup.assets at import
time so responses containing novel asset_type values (e.g., 'pixel_tracker')
parse as UnknownFormatAsset / UnknownGroupAsset instead of raising a cascade
of ValidationErrors that zero out the entire list_creative_formats response.

This module is intentionally NOT auto-generated. It lives outside
generated_poc/ and is preserved across codegen runs (generate_types.py only
wipes src/adcp/types/generated_poc/).

Import order: comes after _ergonomic in types/__init__.py (alphabetical by
underscore-preserved sort). Importing this module directly triggers import of
adcp.types.aliases as a side effect (via ``from adcp.types.aliases import …``)
so there is no circular-import risk and no strict ordering requirement against
aliases in __init__.py.

Import layering: this module imports directly from generated_poc (like
aliases.py and _ergonomic.py) because it must patch the generated classes
in-place. It is therefore listed in ALLOWED_FILES in test_import_layering.py.
"""

from __future__ import annotations

from typing import Any, cast, get_args

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from adcp.types.aliases import FormatAssetUnion, GroupFormatAssetUnion, RepeatableAssetGroup
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    MediaBuy as BundledCapabilitiesMediaBuy,
)
from adcp.types.generated_poc.core.format import Format
from adcp.types.generated_poc.core.media_buy_features import MediaBuyFeatures


def _patch_model_field(model: type[BaseModel], field_name: str, new_annotation: Any) -> None:
    """Replace a Pydantic model field's annotation in-place.

    Sets both model_fields (the FieldInfo dict Pydantic uses for schema
    generation) and __annotations__ (for introspection), then forces a
    schema rebuild. Preserves default and description from the original field.
    """
    old_fi = model.model_fields.get(field_name)
    kwargs: dict[str, Any] = {"annotation": new_annotation}
    if old_fi is not None:
        if old_fi.default is not PydanticUndefined:
            kwargs["default"] = old_fi.default
        if old_fi.description is not None:
            kwargs["description"] = old_fi.description
    model.model_fields[field_name] = FieldInfo(**kwargs)
    model.__annotations__[field_name] = new_annotation


def _apply_forward_compat() -> None:
    """Open Format.assets and RepeatableAssetGroup.assets to accept novel asset_type values."""
    _patch_model_field(Format, "assets", list[FormatAssetUnion] | None)
    Format.model_rebuild(force=True)

    _patch_model_field(RepeatableAssetGroup, "assets", list[GroupFormatAssetUnion])
    cast(type[BaseModel], RepeatableAssetGroup).model_rebuild(force=True)

    # The 3.1 canonical-creatives capability was published after the bundled
    # generated model. Preserve it across code generation until the schema
    # bundle catches up; negotiation must not silently discard this evidence.
    canonical_creatives_annotation: Any = bool | None
    bundled_media_buy_features_arms = [
        arm
        for arm in get_args(BundledCapabilitiesMediaBuy.model_fields["features"].annotation)
        if arm is not type(None)
    ]
    if len(bundled_media_buy_features_arms) != 1:
        raise RuntimeError(
            "forward compatibility: MediaBuy.features lost its concrete model "
            f"(got {bundled_media_buy_features_arms!r})"
        )
    bundled_media_buy_features = bundled_media_buy_features_arms[0]
    for features_model in (MediaBuyFeatures, bundled_media_buy_features):
        features_model.model_fields["canonical_creatives"] = FieldInfo(
            annotation=canonical_creatives_annotation,
            default=None,
            description=(
                "Advertises canonical creative identity on AdCP 3.1. AdCP 3.2+ "
                "is canonical by contract."
            ),
        )
        features_model.__annotations__["canonical_creatives"] = canonical_creatives_annotation
        features_model.model_rebuild(force=True)


_apply_forward_compat()
