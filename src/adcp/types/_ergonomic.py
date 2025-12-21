"""Apply type coercion to generated types for better ergonomics.

This module patches the generated types to accept more flexible input types
while maintaining type safety. It uses Pydantic's model_rebuild() to add
BeforeValidator annotations to fields.

The coercion is applied at module load time, so imports from adcp.types
will automatically have the coercion applied.

Coercion rules applied:
1. Enum fields accept string values (e.g., "video" for FormatCategory.video)
2. List[Enum] fields accept list of strings (e.g., ["image", "video"])
3. ContextObject fields accept dict values
4. ExtensionObject fields accept dict values
5. FieldModel (enum) lists accept string lists

Note: List variance issues (list[Subclass] not assignable to list[BaseClass])
are a fundamental Python typing limitation. Users extending library types
should use Sequence[T] in their own code or cast() for type checker appeasement.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator

from adcp.types.coercion import (
    coerce_subclass_list,
    coerce_to_enum,
    coerce_to_enum_list,
    coerce_to_model,
)

# Import types that need coercion
from adcp.types.generated_poc.core.context import ContextObject
from adcp.types.generated_poc.core.creative_asset import CreativeAsset
from adcp.types.generated_poc.core.creative_assignment import CreativeAssignment
from adcp.types.generated_poc.core.ext import ExtensionObject
from adcp.types.generated_poc.enums.asset_content_type import AssetContentType
from adcp.types.generated_poc.enums.creative_sort_field import CreativeSortField
from adcp.types.generated_poc.enums.format_category import FormatCategory
from adcp.types.generated_poc.enums.sort_direction import SortDirection
from adcp.types.generated_poc.media_buy.create_media_buy_request import (
    CreateMediaBuyRequest,
)
from adcp.types.generated_poc.media_buy.get_products_request import GetProductsRequest
from adcp.types.generated_poc.media_buy.list_creative_formats_request import (
    ListCreativeFormatsRequest,
)
from adcp.types.generated_poc.media_buy.list_creatives_request import (
    FieldModel,
    ListCreativesRequest,
    Sort,
)
from adcp.types.generated_poc.media_buy.package_request import PackageRequest
from adcp.types.generated_poc.media_buy.update_media_buy_request import (
    Packages,
    Packages1,
)


def _apply_coercion() -> None:
    """Apply coercion validators to generated types.

    This function modifies the generated types in-place to accept
    more flexible input types.
    """
    # Apply coercion to ListCreativeFormatsRequest
    # - type: FormatCategory | str | None
    # - asset_types: list[AssetContentType | str] | None
    # - context: ContextObject | dict | None
    # - ext: ExtensionObject | dict | None
    _patch_field_annotation(
        ListCreativeFormatsRequest,
        "type",
        Annotated[FormatCategory | None, BeforeValidator(coerce_to_enum(FormatCategory))],
    )
    _patch_field_annotation(
        ListCreativeFormatsRequest,
        "asset_types",
        Annotated[
            list[AssetContentType] | None,
            BeforeValidator(coerce_to_enum_list(AssetContentType)),
        ],
    )
    _patch_field_annotation(
        ListCreativeFormatsRequest,
        "context",
        Annotated[ContextObject | None, BeforeValidator(coerce_to_model(ContextObject))],
    )
    _patch_field_annotation(
        ListCreativeFormatsRequest,
        "ext",
        Annotated[ExtensionObject | None, BeforeValidator(coerce_to_model(ExtensionObject))],
    )
    ListCreativeFormatsRequest.model_rebuild(force=True)

    # Apply coercion to ListCreativesRequest
    # - fields: list[FieldModel | str] | None
    # - context: ContextObject | dict | None
    # - ext: ExtensionObject | dict | None
    _patch_field_annotation(
        ListCreativesRequest,
        "fields",
        Annotated[list[FieldModel] | None, BeforeValidator(coerce_to_enum_list(FieldModel))],
    )
    _patch_field_annotation(
        ListCreativesRequest,
        "context",
        Annotated[ContextObject | None, BeforeValidator(coerce_to_model(ContextObject))],
    )
    _patch_field_annotation(
        ListCreativesRequest,
        "ext",
        Annotated[ExtensionObject | None, BeforeValidator(coerce_to_model(ExtensionObject))],
    )
    ListCreativesRequest.model_rebuild(force=True)

    # Apply coercion to Sort (nested in ListCreativesRequest)
    # - field: CreativeSortField | str | None
    # - direction: SortDirection | str | None
    _patch_field_annotation(
        Sort,
        "field",
        Annotated[
            CreativeSortField | None,
            BeforeValidator(coerce_to_enum(CreativeSortField)),
        ],
    )
    _patch_field_annotation(
        Sort,
        "direction",
        Annotated[SortDirection | None, BeforeValidator(coerce_to_enum(SortDirection))],
    )
    Sort.model_rebuild(force=True)

    # Apply coercion to GetProductsRequest
    # - context: ContextObject | dict | None
    # - ext: ExtensionObject | dict | None
    _patch_field_annotation(
        GetProductsRequest,
        "context",
        Annotated[ContextObject | None, BeforeValidator(coerce_to_model(ContextObject))],
    )
    _patch_field_annotation(
        GetProductsRequest,
        "ext",
        Annotated[ExtensionObject | None, BeforeValidator(coerce_to_model(ExtensionObject))],
    )
    GetProductsRequest.model_rebuild(force=True)

    # Apply coercion to PackageRequest
    # - creatives: list[CreativeAsset] | None (accepts subclass instances without cast)
    # - ext: ExtensionObject | dict | None
    _patch_field_annotation(
        PackageRequest,
        "creatives",
        Annotated[
            list[CreativeAsset] | None,
            BeforeValidator(coerce_subclass_list(CreativeAsset)),
        ],
    )
    _patch_field_annotation(
        PackageRequest,
        "ext",
        Annotated[ExtensionObject | None, BeforeValidator(coerce_to_model(ExtensionObject))],
    )
    PackageRequest.model_rebuild(force=True)

    # Apply coercion to CreateMediaBuyRequest
    # - packages: list[PackageRequest] (accepts subclass instances without cast)
    # - context: ContextObject | dict | None
    # - ext: ExtensionObject | dict | None
    _patch_field_annotation(
        CreateMediaBuyRequest,
        "packages",
        Annotated[
            list[PackageRequest],
            BeforeValidator(coerce_subclass_list(PackageRequest)),
        ],
    )
    _patch_field_annotation(
        CreateMediaBuyRequest,
        "context",
        Annotated[ContextObject | None, BeforeValidator(coerce_to_model(ContextObject))],
    )
    _patch_field_annotation(
        CreateMediaBuyRequest,
        "ext",
        Annotated[ExtensionObject | None, BeforeValidator(coerce_to_model(ExtensionObject))],
    )
    CreateMediaBuyRequest.model_rebuild(force=True)

    # Apply coercion to UpdateMediaBuyRequest nested Packages types
    # - creatives: list[CreativeAsset] | None (accepts subclass instances without cast)
    # - creative_assignments: list[CreativeAssignment] | None (accepts subclass instances)
    for packages_cls in [Packages, Packages1]:
        _patch_field_annotation(
            packages_cls,
            "creatives",
            Annotated[
                list[CreativeAsset] | None,
                BeforeValidator(coerce_subclass_list(CreativeAsset)),
            ],
        )
        _patch_field_annotation(
            packages_cls,
            "creative_assignments",
            Annotated[
                list[CreativeAssignment] | None,
                BeforeValidator(coerce_subclass_list(CreativeAssignment)),
            ],
        )
        packages_cls.model_rebuild(force=True)


def _patch_field_annotation(
    model: type,
    field_name: str,
    new_annotation: Any,
) -> None:
    """Patch a field annotation on a Pydantic model.

    This modifies the model's __annotations__ dict to add
    BeforeValidator coercion.
    """
    if hasattr(model, "__annotations__"):
        model.__annotations__[field_name] = new_annotation


# Apply coercion when module is imported
_apply_coercion()
