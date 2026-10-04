"""Construct only the requested named base from generated schema annotations."""

from __future__ import annotations

import copy
import importlib
from functools import cache
from typing import Any

from pydantic import Field, GetCoreSchemaHandler, create_model
from pydantic_core import CoreSchema, core_schema

from adcp.types.versioned import (
    VersionedDirection,
    _object_shapes,
    _schema_admits_null,
    _VersionedExtensionModel,
)
from adcp.validation.schema_loader import get_portable_schema


class _Never:
    """The empty wire type: reject every supplied value, allow omission upstream."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        def reject(value: Any) -> Any:
            raise ValueError("field admits no value in the pinned schema")

        return core_schema.no_info_plain_validator_function(reject)


@cache
def resolve_base(
    module_name: str,
    version: str,
    name: str,
    tool: str,
    direction: VersionedDirection,
) -> type[_VersionedExtensionModel]:
    definitions = importlib.import_module(
        f"{module_name.rsplit('.', 1)[0]}._{module_name.rsplit('.', 1)[1]}"
    )
    templates: dict[str, tuple[Any, Any]] = definitions.FIELDS[name]
    schema = get_portable_schema(tool, direction, version=version)
    if schema is None:
        raise LookupError(f"no {version} schema for {tool}::{direction}")
    fields: dict[str, Any] = {
        field: (annotation, Field(default=copy.deepcopy(default)))
        for field, (annotation, default) in templates.items()
    }
    model: type[_VersionedExtensionModel] = create_model(
        name, __base__=_VersionedExtensionModel, __module__=module_name, **fields
    )
    model.schema_version = version
    model.schema_tool_name = tool
    model.schema_direction = direction
    model.schema_document = schema
    # Null defaults represent absence for optional fields whose wire schema
    # forbids null, exactly as in the existing dynamic helper.
    shapes = _object_shapes(schema, schema)
    required = set.intersection(*(required for _properties, required in shapes))
    properties = {
        field: field_schema
        for properties, _ in shapes
        for field, field_schema in properties.items()
    }
    model._omit_none_fields = frozenset(
        field
        for field in templates
        if field not in required and not _schema_admits_null(properties[field], schema)
    )
    return model
