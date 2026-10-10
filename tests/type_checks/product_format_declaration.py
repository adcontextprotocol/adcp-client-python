"""The authoring declaration is a class; consumer formats stay open."""

from typing import Any

from pydantic import BaseModel, TypeAdapter
from typing_extensions import assert_type

from adcp.canonical_formats import (
    V2ToV1Projection,
    find_declaration_by_kind,
    group_declarations_by_product,
    project_declaration_to_v1,
    project_v1_format_to_declaration,
)
from adcp.types import Format, ProductFormatDeclaration


class ProductCatalog(BaseModel):
    format_options: list[ProductFormatDeclaration]


def read_kind(declaration: ProductFormatDeclaration) -> str:
    kind: str = declaration.format_kind
    return kind


def read_parameter_bag(declaration: ProductFormatDeclaration) -> dict[str, Any]:
    return declaration.params


adapter: TypeAdapter[ProductFormatDeclaration] = TypeAdapter(ProductFormatDeclaration)
catalog = ProductCatalog.model_validate(
    {"format_options": [{"format_kind": "image", "params": {"width": 300, "height": 250}}]}
)
assert_type(read_kind(catalog.format_options[0]), str)

# The public name is a class, so an adopter can construct it, validate through
# it, and narrow with it. Each of these is a type error against a union alias.
authored = ProductFormatDeclaration(format_kind="image", params={"width": 300, "height": 250})
validated = ProductFormatDeclaration.model_validate(
    {"format_kind": "image", "params": {"width": 300, "height": 250}}
)
assert_type(authored, ProductFormatDeclaration)
assert_type(validated, ProductFormatDeclaration)
assert_type(adapter.validate_python({}), ProductFormatDeclaration)
assert_type(authored.params_as(Format), Format)


def narrow(value: object) -> str | None:
    if isinstance(value, ProductFormatDeclaration):
        return value.format_kind
    return None


# A declaration IS a Format, so every projection entry point accepts one
# without the adopter restating the type.
assert_type(project_declaration_to_v1(authored), V2ToV1Projection)
assert_type(find_declaration_by_kind("image", [authored]), Format | None)
assert_type(group_declarations_by_product([authored], {}), dict[str, list[Format]])

consumer = Format(format_kind="future_kind", params={})
assert_type(consumer.format_kind, str)
projection = project_v1_format_to_declaration({})
assert_type(projection.declaration, Format | None)
project_declaration_to_v1(consumer)
