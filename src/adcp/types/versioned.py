"""Version-scoped Pydantic models backed by bundled AdCP JSON Schemas.

The primary :mod:`adcp.types` surface represents the SDK's current generated
release. Use this module (or ``adcp.types.v30`` / ``v31`` / ``v32``) when an
application must construct and validate the exact public shape negotiated with
an older peer in the same SDK process. Generated ``.pyi`` files expose exact
field, nested-value, and direct constructor requiredness information to type
checkers; conditional JSON Schema constraints remain runtime-only. At runtime
these remain exact-schema ``RootModel[dict[str, Any]]`` boundary validators, so
subclassing them to add Pydantic fields is not supported; compose adopter-only
state beside the versioned model instead.
"""

from __future__ import annotations

import copy
import re
from functools import cache
from typing import Any, ClassVar, Literal

from pydantic import GetJsonSchemaHandler, RootModel, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from adcp.validation.schema_loader import (
    get_portable_schema,
    get_validator,
    list_validator_keys,
)

VersionedDirection = Literal[
    "request",
    "sync",
    "submitted",
    "working",
    "input-required",
]

_ObjectShape = tuple[dict[str, Any], set[str]]


def _merge_object_shapes(left: list[_ObjectShape], right: list[_ObjectShape]) -> list[_ObjectShape]:
    return [
        ({**left_properties, **right_properties}, left_required | right_required)
        for left_properties, left_required in left
        for right_properties, right_required in right
    ]


def _object_shapes(
    schema: dict[str, Any],
    document: dict[str, Any],
    seen: frozenset[str] = frozenset(),
) -> list[_ObjectShape]:
    """Return every top-level object shape admitted by a bundled schema."""
    shapes: list[_ObjectShape] = [({}, set())]
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/") and reference not in seen:
        target: Any = document
        for raw_part in reference.removeprefix("#/").split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            target = target[part]
        if isinstance(target, dict):
            shapes = _merge_object_shapes(
                shapes,
                _object_shapes(target, document, seen | {reference}),
            )
    for part in schema.get("allOf", []):
        if isinstance(part, dict):
            shapes = _merge_object_shapes(shapes, _object_shapes(part, document, seen))

    properties = schema.get("properties", {})
    own_properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required", [])
    own_required = {item for item in required if isinstance(item, str)}
    shapes = [
        ({**shape_properties, **own_properties}, shape_required | own_required)
        for shape_properties, shape_required in shapes
    ]

    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        alternative_shapes = [
            shape
            for alternative in alternatives
            if isinstance(alternative, dict)
            for shape in _object_shapes(alternative, document, seen)
        ]
        if alternative_shapes:
            shapes = _merge_object_shapes(shapes, alternative_shapes)
    return shapes


def _inline_local_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline bundled definitions for Pydantic's schema post-processor."""
    document = copy.deepcopy(schema)

    def resolve(pointer: str) -> Any:
        value: Any = document
        for raw_part in pointer.removeprefix("#/").split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            value = value[part]
        return value

    def expand(value: Any, stack: frozenset[str]) -> Any:
        if isinstance(value, list):
            return [expand(item, stack) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            if reference in stack:
                return {}
            target = expand(resolve(reference), stack | {reference})
            if isinstance(target, dict):
                siblings = {key: item for key, item in value.items() if key != "$ref"}
                return {**target, **expand(siblings, stack)}
        return {key: expand(item, stack) for key, item in value.items() if key != "$defs"}

    expanded = expand(document, frozenset())
    assert isinstance(expanded, dict)
    return expanded


class VersionedSchemaModel(RootModel[dict[str, Any]]):
    """Dict-shaped Pydantic model that enforces one bundled schema version.

    Keyword construction and attribute access intentionally mirror ordinary
    generated request models while retaining the exact JSON Schema as the
    validation authority. The companion module stubs provide static field
    information. This runtime wrapper remains intended for boundary validation
    and schema generation, not as a base class for adopter-defined models.
    """

    schema_version: ClassVar[str]
    schema_tool_name: ClassVar[str]
    schema_direction: ClassVar[VersionedDirection]
    schema_document: ClassVar[dict[str, Any]]
    optional_fields: ClassVar[frozenset[str]]

    def __init__(self, root: dict[str, Any] | None = None, **data: Any) -> None:
        if root is not None and data:
            raise TypeError("pass either root or keyword fields, not both")
        super().__init__(root=root if root is not None else data)

    @model_validator(mode="before")
    @classmethod
    def _apply_top_level_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        properties = cls.schema_document.get("properties", {})
        if isinstance(properties, dict):
            for name, field_schema in properties.items():
                if (
                    name not in result
                    and isinstance(field_schema, dict)
                    and "default" in field_schema
                ):
                    result[name] = copy.deepcopy(field_schema["default"])
        return result

    @model_validator(mode="after")
    def _validate_bundled_schema(self) -> VersionedSchemaModel:
        validator = get_validator(
            self.schema_tool_name,
            self.schema_direction,
            version=self.schema_version,
        )
        if validator is None:
            raise ValueError(
                f"no {self.schema_version} schema for "
                f"{self.schema_tool_name}::{self.schema_direction}"
            )
        issues = sorted(validator.iter_errors(self.root), key=lambda error: list(error.path))
        if issues:
            issue = issues[0]
            path = ".".join(str(part) for part in issue.absolute_path) or "<root>"
            raise ValueError(f"{path}: {issue.message}")
        return self

    def __getattr__(self, name: str) -> Any:
        root = object.__getattribute__(self, "root")
        if name in root:
            return root[name]
        if name in type(self).optional_fields:
            return None
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    def __getitem__(self, name: str) -> Any:
        return self.root[name]

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return copy.deepcopy(cls.schema_document)

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Expose the negotiated contract to TypeAdapter/FastAPI consumers."""
        del core_schema, handler
        return _inline_local_refs(cls.schema_document)


def _pascal_case(tool_name: str) -> str:
    return "".join(part.capitalize() for part in tool_name.split("_"))


def _snake_case(model_stem: str) -> str:
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", model_stem)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1).lower()


@cache
def schema_model_for_version(
    version: str,
    tool_name: str,
    direction: VersionedDirection = "request",
    *,
    model_name: str | None = None,
) -> type[VersionedSchemaModel]:
    """Return a cached Pydantic model for one version/tool/direction."""
    schema = get_portable_schema(tool_name, direction, version=version)
    if schema is None:
        raise LookupError(f"no {version} schema for {tool_name}::{direction}")
    suffix = {
        "request": "Request",
        "sync": "Response",
        "submitted": "SubmittedResponse",
        "working": "WorkingResponse",
        "input-required": "InputRequiredResponse",
    }[direction]
    name = model_name or f"{_pascal_case(tool_name)}{suffix}"
    shapes = _object_shapes(schema, schema)
    known_fields = {field for properties, _required in shapes for field in properties}
    guaranteed_fields = set.intersection(*(required for _properties, required in shapes))
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        guaranteed_fields.update(
            field
            for field, field_schema in properties.items()
            if isinstance(field_schema, dict) and "default" in field_schema
        )
    return type(
        name,
        (VersionedSchemaModel,),
        {
            "__module__": __name__,
            "schema_version": version,
            "schema_tool_name": tool_name,
            "schema_direction": direction,
            "schema_document": schema,
            "optional_fields": frozenset(known_fields - guaranteed_fields),
        },
    )


def model_for_version(version: str, model_name: str) -> type[VersionedSchemaModel]:
    """Resolve ``ListCreativesRequest``-style names for a protocol release."""
    direction: VersionedDirection
    if model_name.endswith("SubmittedResponse"):
        direction = "submitted"
        stem = model_name[: -len("SubmittedResponse")]
    elif model_name.endswith("WorkingResponse"):
        direction = "working"
        stem = model_name[: -len("WorkingResponse")]
    elif model_name.endswith("InputRequiredResponse"):
        direction = "input-required"
        stem = model_name[: -len("InputRequiredResponse")]
    elif model_name.endswith("Request"):
        direction = "request"
        stem = model_name[: -len("Request")]
    elif model_name.endswith("Response"):
        direction = "sync"
        stem = model_name[: -len("Response")]
    else:
        raise AttributeError(
            f"version-scoped model names must end in Request or Response: {model_name}"
        )
    return schema_model_for_version(
        version,
        _snake_case(stem),
        direction,
        model_name=model_name,
    )


def versioned_surface(
    version: str,
    module_name: str,
) -> tuple[Any, Any, list[str]]:
    """Build PEP 562 hooks for a version shorthand module."""
    names: list[str] = []
    for key in list_validator_keys(version=version):
        tool_name, direction = key.split("::", 1)
        if direction == "request":
            names.append(f"{_pascal_case(tool_name)}Request")
        elif direction == "sync":
            names.append(f"{_pascal_case(tool_name)}Response")
        elif direction == "submitted":
            names.append(f"{_pascal_case(tool_name)}SubmittedResponse")
        elif direction == "working":
            names.append(f"{_pascal_case(tool_name)}WorkingResponse")
        elif direction == "input-required":
            names.append(f"{_pascal_case(tool_name)}InputRequiredResponse")
    exported = sorted(set(names))

    def resolve(name: str) -> Any:
        if name not in exported:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        model = model_for_version(version, name)
        model.__module__ = module_name
        return model

    def directory() -> list[str]:
        return list(exported)

    return resolve, directory, exported


__all__ = [
    "VersionedDirection",
    "VersionedSchemaModel",
    "model_for_version",
    "schema_model_for_version",
    "versioned_surface",
]
