#!/usr/bin/env python3
"""Generate typed stubs for the dynamic version-scoped model modules.

Runtime validation remains backed by the bundled JSON Schemas in
``adcp.types.versioned``. These stubs give mypy and IDEs the corresponding
field, constructor, nested-object, and requiredness information without
introducing a second runtime validation implementation. Conditional JSON
Schema constraints remain enforced at runtime by the bundled validator.
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adcp.validation.schema_loader import get_portable_schema, list_validator_keys  # noqa: E402

VERSIONS = {"v30": "3.0", "v31": "3.1", "v32": "3.2-beta.4"}

TYPING_STRUCTURE_KEYS = frozenset(
    {"$ref", "allOf", "anyOf", "const", "enum", "oneOf", "properties", "type"}
)


def _has_typing_structure(schema: Any) -> bool:
    return isinstance(schema, dict) and bool(TYPING_STRUCTURE_KEYS.intersection(schema))


def _pascal(value: str) -> str:
    value = value.replace("~1", "/").replace("~0", "~")
    value = value.removesuffix(".json")
    words = re.findall(r"[A-Za-z0-9]+", value)
    return "".join(word[:1].upper() + word[1:] for word in words) or "Value"


def _model_name(tool: str, direction: str) -> str:
    suffix = {
        "request": "Request",
        "sync": "Response",
        "submitted": "SubmittedResponse",
        "working": "WorkingResponse",
        "input-required": "InputRequiredResponse",
    }[direction]
    return f"{_pascal(tool)}{suffix}"


def _literal(values: list[Any]) -> str:
    return f"Literal[{', '.join(repr(value) for value in values)}]"


def _intersect_property_schema(left: Any, right: Any) -> Any:
    """Preserve useful intersections when allOf branches reuse a field."""
    if left == right:
        return left
    if not isinstance(left, dict) or not isinstance(right, dict):
        return {"allOf": [left, right]}

    def finite_values(schema: dict[str, Any]) -> list[Any] | None:
        if "const" in schema:
            return [schema["const"]]
        values = schema.get("enum")
        return values if isinstance(values, list) else None

    left_values = finite_values(left)
    right_values = finite_values(right)
    if left_values is not None and right_values is not None:
        intersection = [value for value in left_values if value in right_values]
        return {"enum": intersection}

    left_type = left.get("type")
    right_type = right.get("type")
    if left_type is not None and left_type == right_type:
        return {**left, **right}
    left_types = set(left_type) if isinstance(left_type, list) else {left_type}
    right_types = set(right_type) if isinstance(right_type, list) else {right_type}
    common_types = (left_types & right_types) - {None}
    if common_types:
        schema_type: str | list[str]
        schema_type = next(iter(common_types)) if len(common_types) == 1 else sorted(common_types)
        return {**left, **right, "type": schema_type}
    return {"allOf": [left, right]}


def _merge_property_maps(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for field, schema in right.items():
        if field in merged:
            merged[field] = _intersect_property_schema(merged[field], schema)
        else:
            merged[field] = schema
    return merged


class StubBuilder:
    def __init__(
        self,
        model_name: str,
        schema: dict[str, Any],
        *,
        definition_registry: dict[tuple[str, str], str],
        object_registry: dict[tuple[str, str], str],
        objects: dict[str, tuple[dict[str, Any], StubBuilder]],
        used_names: set[str],
    ) -> None:
        self.model_name = model_name
        self.schema = schema
        self.definitions = schema.get("$defs", {})
        self.definition_names: dict[str, str] = {}
        self.definition_registry = definition_registry
        self.object_registry = object_registry
        self.objects = objects
        self._used_names = used_names
        self.local_reference_names: dict[str, str] = {}
        for raw_name, definition in self.definitions.items():
            fingerprint = json.dumps(definition, sort_keys=True, separators=(",", ":"))
            registry_key = (raw_name, fingerprint)
            name = definition_registry.get(registry_key)
            if name is None:
                name = self._unique_name(f"_{_pascal(raw_name)}")
                definition_registry[registry_key] = name
            self.definition_names[raw_name] = name

    def _unique_name(self, candidate: str) -> str:
        name = candidate
        suffix = 2
        while name in self._used_names:
            name = f"{candidate}{suffix}"
            suffix += 1
        self._used_names.add(name)
        return name

    def _resolve_ref(self, reference: str) -> tuple[str, dict[str, Any]] | None:
        prefix = "#/$defs/"
        if reference.startswith(prefix):
            raw_name = reference[len(prefix) :].replace("~1", "/").replace("~0", "~")
            # Definition keys retain JSON Pointer escaping in some bundles and are
            # decoded in others; support both representations deterministically.
            definition = self.definitions.get(raw_name)
            actual_name = raw_name
            if definition is None:
                encoded = raw_name.replace("~", "~0").replace("/", "~1")
                definition = self.definitions.get(encoded)
                actual_name = encoded
            if definition is not None:
                return self.definition_names[actual_name], definition

        if not reference.startswith("#/"):
            return None
        target: Any = self.schema
        for raw_segment in reference[2:].split("/"):
            segment = raw_segment.replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and (segment in target or raw_segment in target):
                target = target[segment] if segment in target else target[raw_segment]
            elif isinstance(target, list) and segment.isdigit():
                index = int(segment)
                if index >= len(target):
                    return None
                target = target[index]
            else:
                return None
        if not isinstance(target, dict):
            return None
        name = self.local_reference_names.get(reference)
        if name is None:
            fingerprint = json.dumps(target, sort_keys=True, separators=(",", ":"))
            registry_key = (reference, fingerprint)
            name = self.definition_registry.get(registry_key)
            if name is None:
                name = self._unique_name(f"_{_pascal(reference.rsplit('/', 1)[-1])}")
                self.definition_registry[registry_key] = name
            self.local_reference_names[reference] = name
        return name, target

    def _is_object(self, schema: Any) -> bool:
        if not isinstance(schema, dict):
            return False
        if schema.get("type") == "object" or "properties" in schema:
            return True
        if "$ref" in schema:
            resolved = self._resolve_ref(schema["$ref"])
            return resolved is not None and self._is_object(resolved[1])
        alternatives = schema.get("oneOf") or schema.get("anyOf")
        if isinstance(alternatives, list) and alternatives:
            return all(self._is_object(part) for part in alternatives)
        all_of = schema.get("allOf")
        if isinstance(all_of, list) and all_of:
            typed_parts = [part for part in all_of if _has_typing_structure(part)]
            return bool(typed_parts) and all(self._is_object(part) for part in typed_parts)
        return False

    def _finite_values(
        self,
        schema: dict[str, Any],
        seen: frozenset[str] = frozenset(),
    ) -> list[Any] | None:
        if "const" in schema:
            return [schema["const"]]
        enum = schema.get("enum")
        if isinstance(enum, list):
            return enum
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference not in seen:
            resolved = self._resolve_ref(reference)
            if resolved is not None:
                return self._finite_values(resolved[1], seen | {reference})
        all_of = schema.get("allOf")
        if isinstance(all_of, list) and all_of:
            constrained = [
                values
                for part in all_of
                if isinstance(part, dict)
                and (values := self._finite_values(part, seen)) is not None
            ]
            if constrained:
                return [
                    value
                    for value in constrained[0]
                    if all(value in values for values in constrained[1:])
                ]
        alternatives = schema.get("oneOf") or schema.get("anyOf")
        if isinstance(alternatives, list) and alternatives:
            rendered = [
                self._finite_values(part, seen) for part in alternatives if isinstance(part, dict)
            ]
            if rendered and all(values is not None for values in rendered):
                return list(dict.fromkeys(value for values in rendered for value in values or []))
        return None

    def type_expr(self, schema: Any, hint: str) -> str:
        if schema is True:
            return "Any"
        if schema is False:
            return "Never"
        if not isinstance(schema, dict):
            return "Any"
        finite_values = self._finite_values(schema)
        if finite_values is not None:
            return _literal(finite_values) if finite_values else "Never"
        if self._is_object(schema):
            shapes = self.object_shapes(schema)
            if len(shapes) > 1:
                rendered = []
                for index, (properties, required) in enumerate(shapes, 1):
                    variant_schema: dict[str, Any] = {
                        "type": "object",
                        "properties": properties,
                    }
                    if required:
                        variant_schema["required"] = sorted(required)
                    rendered.append(self.type_expr(variant_schema, f"{hint}Variant{index}"))
                return " | ".join(dict.fromkeys(rendered))
        reference = schema.get("$ref")
        if isinstance(reference, str):
            resolved = self._resolve_ref(reference)
            if resolved is not None:
                name, definition = resolved
                if self._is_object(definition):
                    properties, _required = self.object_shape(definition)
                    if not properties:
                        additional = definition.get("additionalProperties")
                        if isinstance(additional, dict):
                            return (
                                "builtins.dict[builtins.str, "
                                f"{self.type_expr(additional, f'{name}Value')}]"
                            )
                        return "builtins.dict[builtins.str, Any]"
                    self.objects.setdefault(name, (definition, self))
                    return name
                return self.type_expr(definition, name)
        variants = schema.get("oneOf")
        if not variants:
            any_of = schema.get("anyOf")
            if (
                isinstance(any_of, list)
                and any_of
                and all(_has_typing_structure(part) for part in any_of)
            ):
                variants = any_of
        if isinstance(variants, list) and variants:
            rendered = [
                self.type_expr(variant, f"{hint}Variant{index}")
                for index, variant in enumerate(variants, 1)
            ]
            return " | ".join(dict.fromkeys(rendered))
        all_of = schema.get("allOf")
        if isinstance(all_of, list) and all_of and not self._is_object(schema):
            rendered = list(
                dict.fromkeys(
                    self.type_expr(part, hint) for part in all_of if _has_typing_structure(part)
                )
            )
            if len(rendered) == 1:
                return rendered[0]
        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            rendered = [self.type_expr({**schema, "type": item}, hint) for item in schema_type]
            return " | ".join(dict.fromkeys(rendered))
        if schema_type == "array":
            return f"builtins.list[{self.type_expr(schema.get('items', {}), f'{hint}Item')}]"
        if self._is_object(schema):
            properties, _ = self.object_shape(schema)
            if not properties:
                additional = schema.get("additionalProperties")
                if isinstance(additional, dict):
                    return (
                        f"builtins.dict[builtins.str, {self.type_expr(additional, f'{hint}Value')}]"
                    )
                return "builtins.dict[builtins.str, Any]"
            fingerprint = json.dumps(schema, sort_keys=True, separators=(",", ":"))
            registry_key = (hint, fingerprint)
            name = self.object_registry.get(registry_key)
            if name is None:
                candidate = hint if hint.startswith("_") else f"_{hint}"
                name = self._unique_name(candidate)
                self.object_registry[registry_key] = name
            self.objects[name] = (schema, self)
            return name
        return {
            "string": "builtins.str",
            "integer": "builtins.int",
            "number": "builtins.float",
            "boolean": "builtins.bool",
            "null": "None",
        }.get(schema_type, "Any")

    def object_shapes(
        self, schema: dict[str, Any], seen: frozenset[str] = frozenset()
    ) -> list[tuple[dict[str, Any], set[str]]]:
        shapes: list[tuple[dict[str, Any], set[str]]] = [({}, set())]

        def merge(
            left: list[tuple[dict[str, Any], set[str]]],
            right: list[tuple[dict[str, Any], set[str]]],
        ) -> list[tuple[dict[str, Any], set[str]]]:
            return [
                (
                    _merge_property_maps(left_properties, right_properties),
                    left_required | right_required,
                )
                for left_properties, left_required in left
                for right_properties, right_required in right
            ]

        reference = schema.get("$ref")
        if isinstance(reference, str) and reference not in seen:
            resolved = self._resolve_ref(reference)
            if resolved is not None:
                shapes = merge(shapes, self.object_shapes(resolved[1], seen | {reference}))
        for part in schema.get("allOf", []):
            if isinstance(part, dict):
                shapes = merge(shapes, self.object_shapes(part, seen))
        own_properties = schema.get("properties", {})
        if not isinstance(own_properties, dict):
            own_properties = {}
        own_required = schema.get("required", [])
        required = (
            {item for item in own_required if isinstance(item, str)}
            if isinstance(own_required, list)
            else set()
        )
        shapes = [
            (_merge_property_maps(properties, own_properties), shape_required | required)
            for properties, shape_required in shapes
        ]

        # Expand exclusive structural alternatives. ``anyOf`` is often used
        # only to express "at least one of these keys"; multiplying those
        # requiredness combinations creates enormous stubs without improving
        # the nested value types, so exact enforcement remains with JSON Schema.
        alternatives = schema.get("oneOf")
        if isinstance(alternatives, list) and alternatives:
            alternative_shapes = [
                shape
                for alternative in alternatives
                if isinstance(alternative, dict)
                for shape in self.object_shapes(alternative, seen)
            ]
            if alternative_shapes:
                shapes = merge(shapes, alternative_shapes)
        return shapes

    def object_shape(self, schema: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        shapes = self.object_shapes(schema)
        if len(shapes) != 1:
            raise ValueError("union object must be rendered as separate TypedDict variants")
        return shapes[0]

    def render_model(self) -> list[str]:
        shapes = self.object_shapes(self.schema)
        top_level_defaults = {
            field
            for field, field_schema in self.schema.get("properties", {}).items()
            if isinstance(field_schema, dict) and "default" in field_schema
        }

        all_fields = dict.fromkeys(
            field for properties, _required in shapes for field in properties
        )
        field_types: dict[str, str] = {}
        constructor_types: dict[str, str] = {}
        shape_field_types = [
            {
                field: self.type_expr(
                    field_schema,
                    f"{self.model_name}{_pascal(field)}",
                )
                for field, field_schema in properties.items()
                if field.isidentifier() and not keyword.iskeyword(field)
            }
            for properties, _required in shapes
        ]
        for field in all_fields:
            if not field.isidentifier() or keyword.iskeyword(field):
                # JSON object keys that cannot be passed as Python keyword
                # arguments remain available through ``root`` construction.
                continue
            annotations = [types[field] for types in shape_field_types if field in types]
            constructor_type = " | ".join(dict.fromkeys(annotations))
            constructor_types[field] = constructor_type
            guaranteed = field in top_level_defaults or all(
                field in required for _properties, required in shapes
            )
            field_types[field] = (
                constructor_type
                if guaranteed or "None" in constructor_type.split(" | ")
                else f"{constructor_type} | None"
            )

        model_lines = [f"class {self.model_name}(VersionedSchemaModel):"]
        if not field_types:
            model_lines.append("    pass")
            return model_lines
        for field, annotation in field_types.items():
            model_lines.append(f"    {field}: {annotation}")
        model_lines.extend(
            [
                "",
                "    @overload",
                "    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...",
            ]
        )
        for shape_index, (properties, required) in enumerate(shapes):
            keyword_fields = [field for field in properties if field in constructor_types]
            ordered_fields = [
                *[field for field in keyword_fields if field in required],
                *[field for field in keyword_fields if field not in required],
            ]
            model_lines.extend(
                [
                    "",
                    "    @overload",
                    "    def __init__(",
                    "        self,",
                    "        *,",
                ]
            )
            for field in ordered_fields:
                default = "" if field in required else " = ..."
                model_lines.append(
                    f"        {field}: {shape_field_types[shape_index][field]}{default},"
                )
            model_lines.append("    ) -> None: ...")
        return model_lines


def _render_objects(objects: dict[str, tuple[dict[str, Any], StubBuilder]]) -> list[str]:
    lines: list[str] = []
    rendered: set[str] = set()
    while True:
        pending = [
            (name, schema, owner)
            for name, (schema, owner) in objects.items()
            if name not in rendered
        ]
        if not pending:
            break
        for name, schema, owner in pending:
            rendered.add(name)
            properties, required = owner.object_shape(schema)
            lines.append(f"class {name}(TypedDict, total=False):")
            if not properties:
                lines.append("    pass")
            for field, field_schema in properties.items():
                if not field.isidentifier() or keyword.iskeyword(field):
                    continue
                annotation = owner.type_expr(field_schema, f"{name}{_pascal(field)}")
                wrapper = "Required" if field in required else "NotRequired"
                lines.append(f"    {field}: {wrapper}[{annotation}]")
            lines.append("")
    return lines


def generate(module: str, version: str) -> str:
    header = [
        '"""Generated typing surface for the dynamic version-scoped models."""',
        "",
        "from __future__ import annotations",
        "",
        "import builtins",
        "from typing import Any, Literal, overload",
        "from typing_extensions import Never, NotRequired, Required, TypedDict",
        "",
        "from adcp.types.versioned import VersionedSchemaModel",
        "",
        "# Underscored TypedDicts are private stub details used to type nested values.",
        "# Runtime composition remains dict-based through the public boundary models.",
        "",
    ]
    model_blocks: list[str] = []
    exported: list[str] = []
    definition_registry: dict[tuple[str, str], str] = {}
    object_registry: dict[tuple[str, str], str] = {}
    objects: dict[str, tuple[dict[str, Any], StubBuilder]] = {}
    used_names = {_model_name(*key.split("::", 1)) for key in list_validator_keys(version=version)}
    for key in list_validator_keys(version=version):
        tool, direction = key.split("::", 1)
        name = _model_name(tool, direction)
        schema = get_portable_schema(tool, direction, version=version)
        if schema is None:
            continue
        builder = StubBuilder(
            name,
            schema,
            definition_registry=definition_registry,
            object_registry=object_registry,
            objects=objects,
            used_names=used_names,
        )
        model_blocks.extend(builder.render_model())
        model_blocks.append("")
        exported.append(name)
    blocks = [*_render_objects(objects), *model_blocks]
    blocks.append(f"__all__ = {exported!r}")
    blocks.append("")
    return "\n".join([*header, *blocks])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale: list[Path] = []
    for module, version in VERSIONS.items():
        path = ROOT / "src" / "adcp" / "types" / f"{module}.pyi"
        content = generate(module, version)
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(path)
        else:
            path.write_text(content)
            print(f"Generated {path.relative_to(ROOT)}")
    if stale:
        print("Versioned type stubs are stale:", file=sys.stderr)
        for path in stale:
            print(f"  {path.relative_to(ROOT)}", file=sys.stderr)
        print("Run: python scripts/generate_versioned_stubs.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
