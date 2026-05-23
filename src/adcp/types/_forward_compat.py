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

from typing import Any, cast

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from adcp.types.aliases import FormatAssetUnion, GroupFormatAssetUnion, RepeatableAssetGroup
from adcp.types.generated_poc.core.format import Format


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


_apply_forward_compat()
