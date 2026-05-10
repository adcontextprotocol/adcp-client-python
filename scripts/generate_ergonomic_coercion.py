#!/usr/bin/env python3
"""Generate _ergonomic.py by introspecting generated Pydantic models.

This script analyzes the generated Pydantic types to auto-generate the type
coercion rules in _ergonomic.py. It uses runtime introspection of the actual
models to detect fields that need coercion.

Coercion patterns detected:
1. Enum fields -> coerce_to_enum
2. list[Enum] fields -> coerce_to_enum_list
3. ContextObject fields -> coerce_to_model(ContextObject)
4. ExtensionObject fields -> coerce_to_model(ExtensionObject)
5. list[BaseModel] fields -> coerce_subclass_list (for subclass variance)
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Any, get_args, get_origin

# Add src to path so we can import the types
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

OUTPUT_FILE = REPO_ROOT / "src" / "adcp" / "types" / "_ergonomic.py"

# Types to analyze for coercion
# These are the main request types users construct
REQUEST_TYPES_TO_ANALYZE = [
    "ListCreativeFormatsRequest",
    "ListCreativesRequest",
    "PackageRequest",
    "CreateMediaBuyRequest",
]

# Response types to analyze for coercion
RESPONSE_TYPES_TO_ANALYZE = [
    "GetProductsResponse",
    "ListCreativesResponse",
    "ListCreativeFormatsResponse",
    "CreateMediaBuyResponse1",
    "GetMediaBuyDeliveryResponse",
]

# Nested types that also need coercion
NESTED_TYPES_TO_ANALYZE = [
    ("Sort", "creative.list_creatives_request"),
    ("GetProductsRequest", "media_buy.get_products_request"),
    ("PackageUpdate", "media_buy.package_update"),
]

# Types that should get subclass_list coercion (for list variance)
SUBCLASS_LIST_TYPES = {
    # Request list types
    "CreativeAsset",
    "CreativeAssignment",
    "PackageRequest",
    # Response list types
    "Product",
    "Creative",
    "Format",
    "Package",
    "MediaBuyDelivery",
    "Error",
    "CreativeAgent",
}


def get_base_type(annotation: Any) -> Any:
    """Extract the base type from Optional/Union annotations.

    For X | None, returns X.
    For non-union types, returns the type as-is.
    """
    origin = get_origin(annotation)
    if origin is type(None):
        return None

    # Handle Union types (including Optional which is Union[X, None])
    # Check if it's a union by looking at origin
    import types

    if origin is types.UnionType:
        args = get_args(annotation)
        # Filter out None type
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
        return annotation

    # Not a union - return as-is
    return annotation


def is_list_of(annotation: Any, item_check) -> tuple[bool, Any]:
    """Check if annotation is list[X] or Sequence[X] where X passes item_check.

    Handles both T[X] and T[X] | None (where T is list or collections.abc.Sequence).
    """
    from collections.abc import Sequence as AbcSequence

    _list_origins = (list, AbcSequence)

    # First check if the annotation itself is a list/Sequence
    origin = get_origin(annotation)
    if origin in _list_origins:
        args = get_args(annotation)
        if args and item_check(args[0]):
            return True, args[0]

    # Then check if it's Optional[list[X]] or Optional[Sequence[X]]
    base = get_base_type(annotation)
    if base is not None and base is not annotation:
        origin = get_origin(base)
        if origin in _list_origins:
            args = get_args(base)
            if args and item_check(args[0]):
                return True, args[0]

    return False, None


def analyze_model(model_class) -> list[dict]:
    """Analyze a Pydantic model and return fields needing coercion."""
    from pydantic import BaseModel

    coercions = []

    # Import the specific types we check for
    from adcp.types.generated_poc.core.context import ContextObject
    from adcp.types.generated_poc.core.ext import ExtensionObject

    for field_name, field_info in model_class.model_fields.items():
        annotation = field_info.annotation
        base_type = get_base_type(annotation)

        if base_type is None:
            continue

        # Check for enum field
        if isinstance(base_type, type) and issubclass(base_type, Enum):
            coercions.append(
                {
                    "field": field_name,
                    "type": "enum",
                    "target_class": base_type,
                }
            )
            continue

        # Check for list[Enum]
        is_enum_list, enum_type = is_list_of(
            annotation, lambda t: isinstance(t, type) and issubclass(t, Enum)
        )
        if is_enum_list:
            coercions.append(
                {
                    "field": field_name,
                    "type": "enum_list",
                    "target_class": enum_type,
                }
            )
            continue

        # Check for ContextObject
        if base_type is ContextObject:
            coercions.append(
                {
                    "field": field_name,
                    "type": "context",
                }
            )
            continue

        # Check for ExtensionObject
        if base_type is ExtensionObject:
            coercions.append(
                {
                    "field": field_name,
                    "type": "ext",
                }
            )
            continue

        # Check for list[BaseModel] - for subclass variance
        is_model_list, model_type = is_list_of(
            annotation, lambda t: isinstance(t, type) and issubclass(t, BaseModel)
        )
        if is_model_list and model_type.__name__ in SUBCLASS_LIST_TYPES:
            coercions.append(
                {
                    "field": field_name,
                    "type": "subclass_list",
                    "target_class": model_type,
                }
            )
            continue

    return coercions


def get_import_path(cls) -> str:
    """Get the import path for a class relative to generated_poc."""
    module = cls.__module__
    # Convert adcp.types.generated_poc.x.y to x.y
    if "generated_poc" in module:
        return module.split("generated_poc.")[1]
    return module


def get_symbol_name(cls) -> str:
    """Get the symbol name to use in generated code.

    Some generated modules now expose colliding enum names like Field1 in
    multiple namespaces. Give those stable aliases in the generated module.
    """
    path = get_import_path(cls)
    if path == "media_buy.get_products_request" and cls.__name__ == "Field1":
        return "GetProductsField"
    if path == "creative.list_creatives_request" and cls.__name__ == "Field1":
        return "ListCreativesField"
    return cls.__name__


def generate_code() -> str:
    """Generate the _ergonomic.py module content."""
    # Import all the types we need to analyze
    from adcp.types.generated_poc.media_buy import (
        create_media_buy_response as _cmbr_module,
    )
    from adcp.types.generated_poc.media_buy.create_media_buy_request import CreateMediaBuyRequest
    from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
        GetMediaBuyDeliveryResponse,
    )

    # Resolve the CreateMediaBuyResponse success variant. Different
    # datamodel-codegen versions emit the variants under shifting names:
    # sometimes `CreateMediaBuyResponse1`/`...2`, sometimes `CreateMediaBuyResponse`
    # (success, unnumbered) + `CreateMediaBuyResponseN`. Find the success
    # variant by scanning the module for a pydantic model whose fields include
    # the success-only `media_buy_id` key.
    from pydantic import BaseModel as _PydBaseModel

    def _find_success_variant() -> type[_PydBaseModel]:
        for name in dir(_cmbr_module):
            obj = getattr(_cmbr_module, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, _PydBaseModel)
                and "media_buy_id" in getattr(obj, "model_fields", {})
            ):
                return obj
        raise ImportError(
            "Could not find CreateMediaBuyResponse success variant "
            "(class with a `media_buy_id` field) in "
            "adcp.types.generated_poc.media_buy.create_media_buy_response"
        )

    CreateMediaBuyResponse1 = _find_success_variant()
    from adcp.types.generated_poc.media_buy.get_products_request import (
        Field1 as GetProductsField,
        GetProductsRequest,
    )

    # Response types
    from adcp.types.generated_poc.media_buy.get_products_response import GetProductsResponse
    from adcp.types.generated_poc.media_buy.list_creative_formats_request import (
        ListCreativeFormatsRequest,
    )
    from adcp.types.generated_poc.media_buy.list_creative_formats_response import (
        ListCreativeFormatsResponse,
    )
    from adcp.types.generated_poc.creative.list_creatives_request import (
        Field1 as ListCreativesField,
        ListCreativesRequest,
        Sort,
    )
    from adcp.types.generated_poc.creative.list_creatives_response import ListCreativesResponse
    from adcp.types.generated_poc.media_buy.package_request import PackageRequest
    from adcp.types.generated_poc.media_buy.package_update import PackageUpdate

    # Map names to classes
    request_classes = {
        "ListCreativeFormatsRequest": ListCreativeFormatsRequest,
        "ListCreativesRequest": ListCreativesRequest,
        "PackageRequest": PackageRequest,
        "CreateMediaBuyRequest": CreateMediaBuyRequest,
    }

    response_classes = {
        "GetProductsResponse": GetProductsResponse,
        "ListCreativesResponse": ListCreativesResponse,
        "ListCreativeFormatsResponse": ListCreativeFormatsResponse,
        "CreateMediaBuyResponse1": CreateMediaBuyResponse1,
        "GetMediaBuyDeliveryResponse": GetMediaBuyDeliveryResponse,
    }

    nested_classes = {
        "Sort": Sort,
        "GetProductsRequest": GetProductsRequest,
        "PackageUpdate": PackageUpdate,
    }

    # Analyze all types
    all_coercions = {}
    all_imports = set()

    for name, cls in {**request_classes, **response_classes, **nested_classes}.items():
        coercions = analyze_model(cls)
        if coercions:
            all_coercions[name] = (cls, coercions)
            # Collect imports
            for c in coercions:
                if "target_class" in c:
                    all_imports.add(c["target_class"])

    # Group imports by module
    enum_imports = []
    core_imports = []
    request_imports = []

    for cls in all_imports:
        path = get_import_path(cls)
        if path.startswith("enums."):
            enum_imports.append((cls.__name__, path))
        elif path.startswith("core."):
            core_imports.append((cls.__name__, path))
        elif path.startswith("media_buy.") or path.startswith("creative."):
            request_imports.append((cls.__name__, path))

    # Always include these core types
    core_imports.append(("ContextObject", "core.context"))
    core_imports.append(("ExtensionObject", "core.ext"))
    core_imports.append(("CreativeAsset", "core.creative_asset"))
    core_imports.append(("CreativeAssignment", "core.creative_assignment"))
    core_imports.append(("Product", "core.product"))
    core_imports.append(("Format", "core.format"))
    core_imports.append(("Package", "core.package"))
    core_imports.append(("Error", "core.error"))

    # Deduplicate
    enum_imports = sorted(set(enum_imports))
    core_imports = sorted(set(core_imports))

    # Build the module
    lines = [
        "# AUTO-GENERATED by scripts/generate_ergonomic_coercion.py",
        "# Do not edit manually - changes will be overwritten on next type generation.",
        "# To regenerate: python scripts/generate_types.py",
        "",
        "# ruff: noqa: E501, I001",
        '"""Apply type coercion to generated types for better ergonomics.',
        "",
        "This module patches the generated types to accept more flexible input types",
        "while maintaining type safety. It uses Pydantic's model_rebuild() to add",
        "BeforeValidator annotations to fields.",
        "",
        "Why import-time patching?",
        "    We apply coercion at module load time rather than lazily because:",
        "    1. Pydantic validation runs during __init__, before any lazy access",
        "    2. model_rebuild() is the standard Pydantic pattern for post-hoc changes",
        "    3. The cost is minimal (~10-20ms for all types, once at import)",
        "    4. After import, there is zero runtime overhead",
        "    5. This approach maintains full type checker compatibility",
        "",
        "Coercion rules applied:",
        '1. Enum fields accept string values (e.g., "video" for enum fields)',
        '2. List[Enum] fields accept list of strings (e.g., ["image", "video"])',
        "3. ContextObject fields accept dict values",
        "4. ExtensionObject fields accept dict values",
        "5. FieldModel (enum) lists accept string lists",
        "",
        "Note: List variance issues (list[Subclass] not assignable to list[BaseClass])",
        "are a fundamental Python typing limitation. Response-only container fields",
        "(affected_packages, media_buys, packages, media_buy_deliveries) already use",
        "Sequence[T] in their generated base class. For other fields not yet migrated,",
        "adopters should use Sequence[T] in their own code or cast() for appeasement.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from collections.abc import Sequence",
        "from typing import Annotated, Any",
        "",
        "from pydantic import BeforeValidator",
        "",
        "from adcp.types.coercion import (",
        "    coerce_subclass_list,",
        "    coerce_to_enum,",
        "    coerce_to_enum_list,",
        "    coerce_to_model,",
        ")",
        "",
    ]

    # Add core imports
    for name, path in core_imports:
        lines.append(f"from adcp.types.generated_poc.{path} import {name}")

    # Add enum imports
    for name, path in enum_imports:
        lines.append(f"from adcp.types.generated_poc.{path} import {name}")

    # Add request type imports
    lines.append("from adcp.types.generated_poc.media_buy.create_media_buy_request import (")
    lines.append("    CreateMediaBuyRequest,")
    lines.append(")")
    lines.append("from adcp.types.generated_poc.media_buy.get_products_request import (")
    lines.append("    Field1 as GetProductsField,")
    lines.append("    GetProductsRequest,")
    lines.append(")")
    lines.append("from adcp.types.generated_poc.media_buy.list_creative_formats_request import (")
    lines.append("    ListCreativeFormatsRequest,")
    lines.append(")")
    lines.append("from adcp.types.generated_poc.creative.list_creatives_request import (")
    lines.append("    Field1 as ListCreativesField,")
    lines.append("    ListCreativesRequest,")
    lines.append("    Sort,")
    lines.append(")")
    lines.append("from adcp.types.generated_poc.media_buy.package_request import PackageRequest")
    lines.append("from adcp.types.generated_poc.media_buy.package_update import PackageUpdate")

    # Add response type imports. CreateMediaBuyResponse1's numeric suffix can
    # shift between codegen versions; import under its actual class name.
    cmbr_name = CreateMediaBuyResponse1.__name__
    lines.append("from adcp.types.generated_poc.media_buy.create_media_buy_response import (")
    if cmbr_name == "CreateMediaBuyResponse1":
        lines.append("    CreateMediaBuyResponse1,")
    else:
        lines.append(f"    {cmbr_name} as CreateMediaBuyResponse1,")
    lines.append(")")
    lines.append("from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (")
    lines.append("    GetMediaBuyDeliveryResponse,")
    lines.append("    MediaBuyDelivery,")
    lines.append("    NotificationType,")
    lines.append(")")
    lines.append(
        "from adcp.types.generated_poc.media_buy.get_products_response import GetProductsResponse"
    )
    lines.append("from adcp.types.generated_poc.media_buy.list_creative_formats_response import (")
    lines.append("    CreativeAgent,")
    lines.append("    ListCreativeFormatsResponse,")
    lines.append(")")
    lines.append("from adcp.types.generated_poc.creative.list_creatives_response import (")
    lines.append("    Creative,")
    lines.append("    ListCreativesResponse,")
    lines.append(")")

    # Add any discovered enum/model imports from request/response modules
    # These are types (like BuyingMode, ValidAction) defined inside request/response
    # modules rather than in the enums/ directory.
    # Filter out types already imported in the hardcoded block above.
    already_imported = {
        "CreateMediaBuyRequest", "GetProductsRequest", "Field1",
        "ListCreativeFormatsRequest", "ListCreativesRequest", "Sort",
        "PackageRequest", "PackageUpdate", "CreateMediaBuyResponse1",
        "GetMediaBuyDeliveryResponse", "MediaBuyDelivery", "NotificationType",
        "GetProductsResponse", "ListCreativeFormatsResponse", "CreativeAgent",
        "Creative", "ListCreativesResponse",
    }
    request_imports_sorted = sorted(set(request_imports))
    for name, path in request_imports_sorted:
        if name not in already_imported:
            lines.append(f"from adcp.types.generated_poc.{path} import {name}")

    lines.append("")
    lines.append("")
    lines.append("def _apply_coercion() -> None:")
    lines.append('    """Apply coercion validators to generated types.')
    lines.append("")
    lines.append("    This function modifies the generated types in-place to accept")
    lines.append("    more flexible input types.")
    lines.append('    """')

    # Generate coercion code for each type
    # Process in a specific order for readability
    type_order = [
        # Request types
        "ListCreativeFormatsRequest",
        "ListCreativesRequest",
        "Sort",
        "GetProductsRequest",
        "PackageRequest",
        "CreateMediaBuyRequest",
        "PackageUpdate",
        # Response types
        "GetProductsResponse",
        "ListCreativesResponse",
        "ListCreativeFormatsResponse",
        "CreateMediaBuyResponse1",
        "GetMediaBuyDeliveryResponse",
    ]

    for type_name in type_order:
        if type_name not in all_coercions:
            continue

        cls, coercions = all_coercions[type_name]

        # Add comment describing what we're coercing
        field_comments = []
        for c in coercions:
            if c["type"] == "enum":
                target = get_symbol_name(c["target_class"])
                field_comments.append(f'{c["field"]}: {target} | str | None')
            elif c["type"] == "enum_list":
                target = get_symbol_name(c["target_class"])
                field_comments.append(
                    f'{c["field"]}: list[{target} | str] | None'
                )
            elif c["type"] == "context":
                field_comments.append(f'{c["field"]}: ContextObject | dict | None')
            elif c["type"] == "ext":
                field_comments.append(f'{c["field"]}: ExtensionObject | dict | None')
            elif c["type"] == "subclass_list":
                field_comments.append(
                    f'{c["field"]}: list[{c["target_class"].__name__}] (accepts subclass instances)'
                )

        lines.append(f"    # Apply coercion to {type_name}")
        for comment in field_comments:
            lines.append(f"    # - {comment}")

        # Generate the actual coercion code
        for c in coercions:
            field = c["field"]
            if c["type"] == "enum":
                target = get_symbol_name(c["target_class"])
                lines.append("    _patch_field_annotation(")
                lines.append(f"        {type_name},")
                lines.append(f'        "{field}",')
                lines.append(
                    f"        Annotated[{target} | None, BeforeValidator(coerce_to_enum({target}))],"
                )
                lines.append("    )")
            elif c["type"] == "enum_list":
                target = get_symbol_name(c["target_class"])
                lines.append("    _patch_field_annotation(")
                lines.append(f"        {type_name},")
                lines.append(f'        "{field}",')
                lines.append("        Annotated[")
                lines.append(f"            list[{target}] | None,")
                lines.append(f"            BeforeValidator(coerce_to_enum_list({target})),")
                lines.append("        ],")
                lines.append("    )")
            elif c["type"] == "context":
                lines.append("    _patch_field_annotation(")
                lines.append(f"        {type_name},")
                lines.append(f'        "{field}",')
                lines.append(
                    "        Annotated[ContextObject | None, BeforeValidator(coerce_to_model(ContextObject))],"
                )
                lines.append("    )")
            elif c["type"] == "ext":
                lines.append("    _patch_field_annotation(")
                lines.append(f"        {type_name},")
                lines.append(f'        "{field}",')
                lines.append(
                    "        Annotated[ExtensionObject | None, BeforeValidator(coerce_to_model(ExtensionObject))],"
                )
                lines.append("    )")
            elif c["type"] == "subclass_list":
                from collections.abc import Sequence as AbcSequence

                target = c["target_class"].__name__
                field_info = cls.model_fields[field]
                is_optional = "None" in str(field_info.annotation)
                # Preserve Sequence[T] when the field already uses it (covariant
                # inheritance, set by post_generate_fixes.rewrite_response_list_to_sequence).
                ann = field_info.annotation
                base_ann = get_base_type(ann)
                is_seq = get_origin(base_ann if base_ann is not None else ann) is AbcSequence
                container = "Sequence" if is_seq else "list"
                type_str = f"{container}[{target}] | None" if is_optional else f"{container}[{target}]"
                lines.append("    _patch_field_annotation(")
                lines.append(f"        {type_name},")
                lines.append(f'        "{field}",')
                lines.append("        Annotated[")
                lines.append(f"            {type_str},")
                lines.append(f"            BeforeValidator(coerce_subclass_list({target})),")
                lines.append("        ],")
                lines.append("    )")

        lines.append(f"    {type_name}.model_rebuild(force=True)")
        lines.append("")

    # PackageUpdate is now a single class (flattened from validation-only oneOf)

    # Add helper function
    lines.append("")
    lines.append("def _patch_field_annotation(")
    lines.append("    model: type,")
    lines.append("    field_name: str,")
    lines.append("    new_annotation: Any,")
    lines.append(") -> None:")
    lines.append('    """Patch a field annotation on a Pydantic model.')
    lines.append("")
    lines.append("    This modifies the model's __annotations__ dict to add")
    lines.append("    BeforeValidator coercion.")
    lines.append('    """')
    lines.append("    model.__annotations__[field_name] = new_annotation")
    lines.append("")
    lines.append("")
    lines.append("# Apply coercion when module is imported")
    lines.append("_apply_coercion()")
    lines.append("")

    return "\n".join(lines)


def main():
    """Generate _ergonomic.py from model introspection."""
    print("Generating ergonomic coercion module...")

    content = generate_code()

    # Write to output file
    OUTPUT_FILE.write_text(content)
    print(f"  ✓ Generated {OUTPUT_FILE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
