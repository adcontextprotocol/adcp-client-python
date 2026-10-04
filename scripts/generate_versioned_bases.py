#!/usr/bin/env python3
"""Generate named extension bases and runtime annotations from pinned schemas.

No current-model annotations are used. Nested values are TypedDicts at runtime
and in stubs, and the existing exact JSON Schema validator remains the wire
boundary. Importing a version module constructs no Pydantic model.
"""

from __future__ import annotations

import argparse
import keyword
import sys
from io import StringIO
from pathlib import Path
from tokenize import NAME, generate_tokens, untokenize
from typing import Any

from generate_versioned_stubs import VERSIONS, StubBuilder, _model_name, _pascal, _render_objects

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adcp.validation.schema_loader import get_portable_schema, list_validator_keys  # noqa: E402


def _qualify_annotation(annotation: str) -> str:
    """Qualify private type names while retaining literal strings and spacing."""
    return untokenize(
        (
            token._replace(string=f"_definitions.{token.string}")
            if token.type == NAME and token.string.startswith("_")
            else token
        )
        for token in generate_tokens(StringIO(annotation).readline)
    )


def generate(module: str, version: str) -> dict[str, str]:
    definition_registry: dict[tuple[str, str], str] = {}
    object_registry: dict[tuple[str, str], str] = {}
    objects: dict[str, tuple[dict[str, Any], StubBuilder]] = {}
    keys = list_validator_keys(version=version)
    used_names = {_model_name(*key.split("::", 1)) + "Base" for key in keys}
    stub_blocks: list[str] = []
    template_blocks = ["FIELDS: dict[str, dict[str, tuple[Any, Any]]] = {"]
    index: dict[str, tuple[str, str]] = {}
    for key in keys:
        tool, direction = key.split("::", 1)
        name = _model_name(tool, direction) + "Base"
        schema = get_portable_schema(tool, direction, version=version)
        if schema is None:
            raise RuntimeError(f"missing pinned schema: {version}/{key}")
        builder = StubBuilder(
            name,
            schema,
            definition_registry=definition_registry,
            object_registry=object_registry,
            objects=objects,
            used_names=used_names,
        )
        shapes = builder.object_shapes(schema)
        all_fields = dict.fromkeys(field for properties, _ in shapes for field in properties)
        stub_blocks.append(f"class {name}(_VersionedExtensionModel):")
        template_blocks.append(f"    {name!r}: {{")
        for field in all_fields:
            field_schemas = [properties[field] for properties, _ in shapes if field in properties]
            annotations = list(
                dict.fromkeys(
                    builder.type_expr(s, f"{name}{_pascal(field)}") for s in field_schemas
                )
            )
            annotation = " | ".join(annotations)
            required = all(field in fields for _, fields in shapes)
            default_schema = next(
                (s for s in field_schemas if isinstance(s, dict) and "default" in s), None
            )
            if default_schema is not None:
                default = repr(default_schema["default"])
            elif required:
                default = "..."
            else:
                default = "None"
                if "None" not in annotation.split(" | "):
                    annotation += " | None"
            # Refer to the exact same nested declarations from runtime and stub.
            stub_annotation = _qualify_annotation(annotation)
            stub_default = f" = Field(default={default})" if default != "..." else ""
            if field.isidentifier() and not keyword.iskeyword(field):
                stub_blocks.append(f"    {field}: {stub_annotation}{stub_default}")
            template_blocks.append(f"        {field!r}: ({annotation}, {default}),")
        if not all_fields:
            stub_blocks.append("    pass")
        stub_blocks.append("")
        template_blocks.append("    },")
        index[name] = (tool, direction)
    template_blocks.extend(["}", ""])
    definitions = "\n".join(
        [
            '"""Generated nested types and field templates; no model construction."""',
            "from __future__ import annotations",
            "",
            "import builtins",
            "from typing import TYPE_CHECKING, Any, Literal",
            "from typing_extensions import NotRequired, Required, TypedDict",
            "if TYPE_CHECKING:",
            "    from typing_extensions import Never",
            "else:",
            "    from ._runtime import _Never as Never",
            "from pydantic import ConfigDict, with_config",
            "",
            *[
                (
                    ('@with_config(ConfigDict(extra="allow"))\n' + line)
                    if line.startswith("class ")
                    else line
                )
                for line in _render_objects(objects)
            ],
            *template_blocks,
        ]
    )
    stub = "\n".join(
        [
            '"""Generated named extension bases for the pinned bundled schema."""',
            "from __future__ import annotations",
            "",
            "import builtins",
            "from typing import Any, Literal",
            "from pydantic import Field",
            "from adcp.types.versioned import _VersionedExtensionModel",
            f"from . import _{module} as _definitions",
            "",
            *stub_blocks,
            f"__all__ = {list(index)!r}",
            "",
        ]
    )
    runtime = "\n".join(
        [
            '"""Generated lazy named extension bases for the pinned bundled schema."""',
            "from typing import Any",
            "from adcp.types.versioned_bases._runtime import resolve_base",
            "",
            f"_VERSION = {version!r}",
            f"_INDEX = {index!r}",
            "__all__ = list(_INDEX)",
            "",
            "def __getattr__(name: str) -> Any:",
            "    if name not in _INDEX:",
            '        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")',
            "    tool, direction = _INDEX[name]",
            "    model = resolve_base(__name__, _VERSION, name, tool, direction)",
            "    globals()[name] = model",
            "    return model",
            "",
            "def __dir__() -> list[str]:",
            "    return sorted(__all__)",
            "",
        ]
    )
    return {f"_{module}.py": definitions, f"{module}.pyi": stub, f"{module}.py": runtime}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = ROOT / "src" / "adcp" / "types" / "versioned_bases"
    stale = []
    for module, version in VERSIONS.items():
        for filename, content in generate(module, version).items():
            path = output / filename
            if args.check:
                if not path.exists() or path.read_text() != content:
                    stale.append(path)
            else:
                path.write_text(content)
                print(f"Generated {path.relative_to(ROOT)}")
    if stale:
        print("Named versioned bases are stale:", file=sys.stderr)
        for path in stale:
            print(f"  {path.relative_to(ROOT)}", file=sys.stderr)
        print("Run: python scripts/generate_versioned_bases.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
