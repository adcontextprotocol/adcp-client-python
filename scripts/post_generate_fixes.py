#!/usr/bin/env python3
# ruff: noqa: E501
"""
Post-generation fixes for generated Pydantic models.

This script applies necessary modifications to generated files that cannot be
handled by datamodel-code-generator directly:

1. Rewrites generated string enums to StrEnum
2. Adds model_validators to types requiring mutual exclusivity checks
3. Fixes self-referential RootModel type annotations
4. Fixes BrandManifest forward references
5. Adds deprecated=True to fields marked deprecated in JSON schema
6. Unwraps specified RootModel unions to plain Union type aliases (#155)
7. Widens canceled: Literal[True] = True on request types to | None = None (#641)
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent


# Load ``resolve_bundle_key`` from its source file rather than via the
# ``adcp`` package — this script runs after datamodel-codegen produces a
# fresh ``generated_poc/`` tree, before the post-fixes that make it
# importable. ``adcp/__init__.py`` would crash on the unfixed models.
def _load_resolve_bundle_key():
    src = REPO_ROOT / "src" / "adcp" / "validation" / "version.py"
    spec = importlib.util.spec_from_file_location("_adcp_bundle_key", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_bundle_key


resolve_bundle_key = _load_resolve_bundle_key()

_VERSION_FILE = REPO_ROOT / "src" / "adcp" / "ADCP_VERSION"
_BUNDLE_KEY = resolve_bundle_key(_VERSION_FILE.read_text().strip())

OUTPUT_DIR = REPO_ROOT / "src" / "adcp" / "types" / "generated_poc"
SCHEMA_DIR = REPO_ROOT / "schemas" / "cache" / _BUNDLE_KEY

_PROTOCOL_ENVELOPE_IMPORT = "from ..core.protocol_envelope import ProtocolEnvelope\n"
_VERSION_ENVELOPE_IMPORT = "from ..core.version_envelope import AdcpVersionEnvelope\n"
_RESPONSE_ARM_DISPATCH_IMPORT = (
    "from adcp.types.response_dispatch import ResponseArmDispatchMixin\n"
)
_STR_ENUM_MEMBER_ASSIGNMENT_IGNORE = "  # type: ignore[assignment]"
_STR_ATTRIBUTE_NAMES = set(dir(str))


def _resolve_schema_ref(schema_rel: Path, ref: str) -> Path:
    """Resolve a schema reference without dropping root-relative domains."""
    file_ref = ref.split("#", 1)[0]
    canonical_url = re.match(
        r"^https://adcontextprotocol\.org/schemas/[^/]+/(.+)$",
        file_ref,
    )
    if canonical_url:
        return Path(canonical_url.group(1))
    if file_ref.startswith("/schemas/"):
        return Path(file_ref.removeprefix("/schemas/"))
    return (SCHEMA_DIR / schema_rel.parent / file_ref).resolve().relative_to(SCHEMA_DIR.resolve())


def _sync_protocol_envelope_import(source: str) -> str:
    """Keep the ProtocolEnvelope import aligned with restored response arms."""
    uses_protocol_envelope = "ProtocolEnvelope" in source.replace(_PROTOCOL_ENVELOPE_IMPORT, "")
    if not uses_protocol_envelope:
        return source.replace(_PROTOCOL_ENVELOPE_IMPORT, "")
    if _PROTOCOL_ENVELOPE_IMPORT in source:
        return source
    if _VERSION_ENVELOPE_IMPORT in source:
        return source.replace(
            _VERSION_ENVELOPE_IMPORT,
            _PROTOCOL_ENVELOPE_IMPORT + _VERSION_ENVELOPE_IMPORT,
            1,
        )
    future_import = "from __future__ import annotations\n\n"
    if future_import in source:
        return source.replace(future_import, future_import + _PROTOCOL_ENVELOPE_IMPORT, 1)
    return _PROTOCOL_ENVELOPE_IMPORT + source


def add_model_validator_to_product():
    """Add model_validators to Product class.

    NOTE: This function is now deprecated after PR #213 added explicit discriminator
    to publisher_properties schema. Pydantic now generates proper discriminated union
    variants (PublisherProperties, PublisherProperties4, PublisherProperties5) with
    Literal discriminator fields, which Pydantic validates automatically.

    Keeping function as no-op for backwards compatibility with older schemas.
    """
    print("  product.py validation: no fixes needed (Pydantic handles discriminated unions)")


def _ignore_strenum_member_method_collisions(source: str) -> tuple[str, int]:
    """Suppress mypy for StrEnum members that intentionally shadow str methods."""
    lines = source.splitlines()
    updated: list[str] = []
    class_indent: int | None = None
    ignores_added = 0

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        class_match = re.match(r"class\s+\w+\(StrEnum\):", stripped)
        if class_match is not None:
            class_indent = indent
            updated.append(line)
            continue

        if class_indent is not None and stripped and indent <= class_indent:
            class_indent = None

        if class_indent is not None and indent == class_indent + 4:
            assignment_match = re.match(r"([A-Za-z_]\w*)\s*=", stripped)
            if (
                assignment_match is not None
                and assignment_match.group(1) in _STR_ATTRIBUTE_NAMES
                and _STR_ENUM_MEMBER_ASSIGNMENT_IGNORE not in line
            ):
                line = f"{line}{_STR_ENUM_MEMBER_ASSIGNMENT_IGNORE}"
                ignores_added += 1

        updated.append(line)

    return "\n".join(updated) + ("\n" if source.endswith("\n") else ""), ignores_added


def rewrite_generated_enums_to_strenum() -> None:
    """Make all generated schema enums inherit from StrEnum.

    datamodel-code-generator emits plain ``Enum`` classes for string-valued
    JSON Schema enums. The generated enum members should behave like their wire
    values for equality, hashing, formatting, and ``str()`` without widening or
    narrowing any model field annotations.
    """
    files_changed = 0
    classes_changed = 0
    ignores_added = 0

    for path in OUTPUT_DIR.rglob("*.py"):
        source = path.read_text()
        if (
            "(Enum):" not in source
            and "from enum import Enum" not in source
            and "(StrEnum):" not in source
        ):
            continue

        updated = source.replace(
            "from enum import Enum, IntEnum\n",
            "from enum import IntEnum\nfrom adcp.types._str_enum import StrEnum\n",
        )
        updated = updated.replace(
            "from enum import IntEnum, Enum\n",
            "from enum import IntEnum\nfrom adcp.types._str_enum import StrEnum\n",
        )
        updated = updated.replace(
            "from enum import Enum\n",
            "from adcp.types._str_enum import StrEnum\n",
        )
        updated, changed = re.subn(r"\((?:str,\s*)?Enum\):", "(StrEnum):", updated)
        updated, file_ignores_added = _ignore_strenum_member_method_collisions(updated)

        if updated != source:
            path.write_text(updated)
            files_changed += 1
            classes_changed += changed
            ignores_added += file_ignores_added

    print(
        f"  generated enums rewritten to StrEnum "
        f"({classes_changed} classes across {files_changed} files, "
        f"{ignores_added} member type ignore(s))"
    )


def fix_preview_render_self_reference():
    """Fix self-referential RootModel in preview_render.py."""
    preview_file = OUTPUT_DIR / "creative" / "preview_render.py"

    if not preview_file.exists():
        print("  preview_render.py not found (skipping)")
        return

    with open(preview_file) as f:
        content = f.read()

    # Check if already fixed
    if "preview_render.PreviewRender1" not in content:
        print("  preview_render.py already fixed or doesn't need fixing")
        return

    # Replace module-qualified names with direct class names
    content = content.replace("preview_render.PreviewRender1", "PreviewRender1")
    content = content.replace("preview_render.PreviewRender2", "PreviewRender2")
    content = content.replace("preview_render.PreviewRender3", "PreviewRender3")

    with open(preview_file, "w") as f:
        f.write(content)

    print("  preview_render.py self-references fixed")


def fix_brand_manifest_references():
    """Fix BrandManifest forward references in promoted_offerings.py.

    datamodel-code-generator imports brand_manifest with an alias (_1 suffix)
    but then references it without the alias in the type annotation.
    This fix updates the type annotation to use the correct alias.
    """
    promoted_offerings_file = OUTPUT_DIR / "core" / "promoted_offerings.py"

    if not promoted_offerings_file.exists():
        print("  promoted_offerings.py not found (skipping)")
        return

    with open(promoted_offerings_file) as f:
        content = f.read()

    # Check if already fixed
    if "brand_manifest_1.BrandManifest" in content:
        print("  promoted_offerings.py already fixed")
        return

    # Fix the import alias mismatch
    # Line imports: from . import brand_manifest as brand_manifest_1
    # But uses: brand_manifest.BrandManifest
    # Need to change to: brand_manifest_1.BrandManifest
    content = content.replace("brand_manifest.BrandManifest", "brand_manifest_1.BrandManifest")

    with open(promoted_offerings_file, "w") as f:
        f.write(content)

    print("  promoted_offerings.py brand_manifest references fixed")


def fix_enum_defaults():
    """Fix enum default values in generated files.

    datamodel-code-generator sometimes creates string defaults for enum fields
    instead of enum member defaults, causing mypy errors.

    Note: brand_manifest_ref.py was a stale file and has been removed.
    The enum defaults in brand_manifest.py are already correct.
    """
    brand_manifest_file = OUTPUT_DIR / "core" / "brand_manifest.py"

    if not brand_manifest_file.exists():
        print("  brand_manifest.py not found (skipping)")
    else:
        with open(brand_manifest_file) as f:
            content = f.read()

        # Check if already fixed (using enum member, not string)
        if "FeedFormat.google_merchant_center" in content:
            print("  brand_manifest.py enum defaults already correct")
        else:
            # Fix ProductCatalog.feed_format default if needed
            content = content.replace(
                'feed_format: FeedFormat | None = Field("google_merchant_center"',
                "feed_format: FeedFormat | None = Field(FeedFormat.google_merchant_center",
            )

            # Fix BrandManifest.feed_format default if needed
            content = content.replace(
                'product_feed_format: FeedFormat | None = Field("google_merchant_center"',
                "product_feed_format: FeedFormat | None = Field(FeedFormat.google_merchant_center",
            )

            with open(brand_manifest_file, "w") as f:
                f.write(content)

            print("  brand_manifest.py enum defaults fixed")

    bundled_media_buys_file = OUTPUT_DIR / "bundled" / "media_buy" / "get_media_buys_response.py"
    if bundled_media_buys_file.exists():
        content = bundled_media_buys_file.read_text()
        new_content = content.replace(
            "] = 'ok'\n    impairments: Annotated[\n",
            "] = 'ok'  # type: ignore[assignment]\n    impairments: Annotated[\n",
            1,
        )
        if new_content != content:
            bundled_media_buys_file.write_text(new_content)
            print("  bundled/media_buy/get_media_buys_response.py health enum default fixed")


def fix_preview_creative_request_discriminator():
    """Add discriminator to PreviewCreativeRequest union.

    The schema uses request_type as a discriminator with const values 'single'
    and 'batch', but datamodel-code-generator doesn't add the discriminator to
    the Field annotation. This adds it explicitly for Pydantic to properly
    validate the union.
    """
    preview_request_file = OUTPUT_DIR / "creative" / "preview_creative_request.py"

    if not preview_request_file.exists():
        print("  preview_creative_request.py not found (skipping)")
        return

    with open(preview_request_file) as f:
        content = f.read()

    # Check if already fixed
    if "discriminator='request_type'" in content:
        print("  preview_creative_request.py discriminator already added")
        return

    # Add discriminator to the Field
    content = content.replace(
        "Field(\n            description='Request to generate previews",
        "Field(\n            discriminator='request_type',\n            description='Request to generate previews",
    )

    with open(preview_request_file, "w") as f:
        f.write(content)

    print("  preview_creative_request.py discriminator added")


def add_deprecated_field_metadata():
    """Add deprecated=True to fields marked deprecated in JSON schema.

    datamodel-code-generator doesn't translate JSON Schema's "deprecated": true
    to Pydantic's Field(deprecated=True). This function reads the schemas and
    injects the metadata into the generated Python files.
    """
    deprecated_fields_fixed = 0

    # Walk through all schema files
    for schema_file in SCHEMA_DIR.rglob("*.json"):
        try:
            with open(schema_file) as f:
                schema = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Find deprecated fields in properties
        properties = schema.get("properties", {})
        deprecated_fields = [
            field_name
            for field_name, field_def in properties.items()
            if isinstance(field_def, dict) and field_def.get("deprecated") is True
        ]

        if not deprecated_fields:
            continue

        # Map schema file to generated Python file
        relative_path = schema_file.relative_to(SCHEMA_DIR)
        # Convert path: core/format.json -> core/format.py
        py_path = OUTPUT_DIR / relative_path.with_suffix(".py")
        # Handle kebab-case to snake_case conversion
        py_path = py_path.parent / py_path.name.replace("-", "_")

        if not py_path.exists():
            continue

        with open(py_path) as f:
            content = f.read()

        modified = False
        for field_name in deprecated_fields:
            field_block = _find_indented_field_block(content, field_name)
            if field_block is None:
                continue
            field_start, field_end = field_block
            field_section = content[field_start:field_end]

            if "deprecated=True" in field_section.split("] = ")[0]:
                continue  # Already fixed

            field_call_offset = field_section.find("Field(")
            if field_call_offset == -1:
                continue
            insert_pos = field_start + field_call_offset + len("Field(")
            # Check what comes after - if it's description=, add before it.
            after_match = content[insert_pos : insert_pos + 50]
            if after_match.strip().startswith("description="):
                new_content = (
                    content[:insert_pos] + "deprecated=True,\n            " + content[insert_pos:]
                )
            else:
                new_content = content[:insert_pos] + "deprecated=True, " + content[insert_pos:]

            if new_content != content:
                content = new_content
                modified = True
                deprecated_fields_fixed += 1

        if modified:
            with open(py_path, "w") as f:
                f.write(content)

    if deprecated_fields_fixed > 0:
        print(f"  Added deprecated=True to {deprecated_fields_fixed} field(s)")
    else:
        print("  No deprecated fields needed fixing")


def apply_open_payload_config():
    """Apply ``x-adcp-open-payload`` to generated named models.

    ``datamodel-code-generator`` ignores custom schema keywords. Current
    open-payload annotations are mostly anonymous object fields and already
    generate ``dict[str, Any]`` because the schema object also carries
    ``additionalProperties: true``. When the annotation appears on a named
    schema object, make the corresponding generated model explicitly
    extension-tolerant so the custom keyword remains contract-bearing.
    """
    updated_classes = 0
    already_open = 0
    anonymous_annotations = 0

    for schema_file in SCHEMA_DIR.rglob("*.json"):
        try:
            schema = json.loads(schema_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        class_names, anonymous_count = _open_payload_class_names(schema)
        anonymous_annotations += anonymous_count
        if not class_names:
            continue

        relative_path = schema_file.relative_to(SCHEMA_DIR)
        py_path = OUTPUT_DIR / relative_path.with_suffix(".py")
        py_path = py_path.parent / py_path.name.replace("-", "_")
        if not py_path.exists():
            continue

        content = py_path.read_text()
        original = content
        for class_name in class_names:
            if class_name is None:
                class_name = _first_generated_class_name(content)
            if class_name is None:
                continue
            content, status = _set_class_extra_allow(content, class_name)
            if status == "updated":
                updated_classes += 1
            elif status == "already":
                already_open += 1

        if content != original:
            content = _ensure_configdict_import(content)
            py_path.write_text(content)

    if updated_classes:
        print(f"  Applied x-adcp-open-payload extra='allow' to {updated_classes} class(es)")
    else:
        print("  No named x-adcp-open-payload classes needed model_config changes")
    if already_open:
        print(f"  {already_open} x-adcp-open-payload class(es) already allowed extras")
    if anonymous_annotations:
        print(
            "  "
            f"{anonymous_annotations} anonymous x-adcp-open-payload annotation(s) "
            "remain dict[str, Any] fields"
        )


def _open_payload_class_names(schema: dict) -> tuple[list[str | None], int]:
    """Return generated class names for named open-payload schema objects.

    ``None`` is a sentinel for the root schema's first generated class when
    the schema has no title. Anonymous property annotations are counted but do
    not map to model classes; datamodel-code-generator emits those as mapping
    fields.
    """
    class_names: list[str | None] = []
    anonymous_count = 0

    def walk(obj: object, path: tuple[str, ...]) -> None:
        nonlocal anonymous_count
        if isinstance(obj, dict):
            if obj.get("x-adcp-open-payload") is True:
                title = obj.get("title")
                if path == ():
                    class_names.append(_schema_title_to_class_name(title) if title else None)
                elif isinstance(title, str) and title.strip():
                    class_names.append(_schema_title_to_class_name(title))
                else:
                    anonymous_count += 1

            for key, value in obj.items():
                walk(value, (*path, key))
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                walk(value, (*path, str(index)))

    walk(schema, ())
    return class_names, anonymous_count


def _schema_title_to_class_name(title: object) -> str:
    words = re.findall(r"[A-Za-z0-9]+", str(title))
    return "".join(word[:1].upper() + word[1:] for word in words)


def _first_generated_class_name(content: str) -> str | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {
            (
                base.id
                if isinstance(base, ast.Name)
                else base.attr if isinstance(base, ast.Attribute) else ""
            )
            for base in node.bases
        }
        if base_names & {"Enum", "IntEnum", "StrEnum"}:
            continue
        return node.name
    return None


def _set_class_extra_allow(content: str, class_name: str) -> tuple[str, str]:
    class_pattern = re.compile(
        rf"(^class {re.escape(class_name)}\b[^\n]*:\n)(.*?)(?=^class |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = class_pattern.search(content)
    if match is None:
        return content, "missing"

    header = match.group(1)
    body = match.group(2)
    config_pattern = re.compile(r"(    model_config = ConfigDict\(\n)(.*?)(    \)\n)", re.DOTALL)
    config_match = config_pattern.search(body)
    # Compact single-line form: `    model_config = ConfigDict(<args>)`. The
    # open paren is not immediately followed by a newline, so the multi-line
    # pattern above never matches it.
    compact_pattern = re.compile(r"(    model_config = ConfigDict\()([^\n]*?)(\)\n)")
    compact_match = compact_pattern.search(body)
    if config_match is not None:
        config_body = config_match.group(2)
        if re.search(r"extra=(['\"])allow\1", config_body):
            return content, "already"
        if re.search(r"extra=(['\"])(?:forbid|ignore)\1", config_body):
            new_config_body = re.sub(
                r"extra=(['\"])(?:forbid|ignore)\1",
                "extra='allow'",
                config_body,
                count=1,
            )
        else:
            new_config_body = "        extra='allow',\n" + config_body
        new_body = (
            body[: config_match.start()]
            + config_match.group(1)
            + new_config_body
            + config_match.group(3)
            + body[config_match.end() :]
        )
    elif compact_match is not None:
        config_args = compact_match.group(2)
        if re.search(r"extra=(['\"])allow\1", config_args):
            return content, "already"
        if re.search(r"extra=(['\"])(?:forbid|ignore)\1", config_args):
            new_config_args = re.sub(
                r"extra=(['\"])(?:forbid|ignore)\1",
                "extra='allow'",
                config_args,
                count=1,
            )
        elif config_args.strip():
            new_config_args = "extra='allow', " + config_args
        else:
            new_config_args = "extra='allow'"
        new_body = (
            body[: compact_match.start()]
            + compact_match.group(1)
            + new_config_args
            + compact_match.group(3)
            + body[compact_match.end() :]
        )
    else:
        new_body = "    model_config = ConfigDict(\n        extra='allow',\n    )\n" + body

    return (
        content[: match.start()] + header + new_body + content[match.end() :],
        "updated",
    )


def _ensure_configdict_import(content: str) -> str:
    if "ConfigDict" not in content:
        return content
    if re.search(r"^from pydantic import .*ConfigDict", content, re.MULTILINE):
        return content
    if "from pydantic import" in content:
        return re.sub(
            r"^from pydantic import ([^\n]+)$",
            lambda m: (
                "from pydantic import "
                + ", ".join(
                    sorted({*[part.strip() for part in m.group(1).split(",")], "ConfigDict"})
                )
            ),
            content,
            count=1,
            flags=re.MULTILINE,
        )

    future_imports = list(re.finditer(r"^from __future__ import [^\n]+$", content, re.MULTILINE))
    if future_imports:
        offset = future_imports[-1].end()
        return content[:offset] + "\n\nfrom pydantic import ConfigDict" + content[offset:]
    return "from pydantic import ConfigDict\n\n" + content


_TYPED_EXTRA_ASSIGNMENT = re.compile(
    r"^(?P<class_name>[A-Za-z_]\w*)\.__annotations__\['__pydantic_extra__'\] = "
    r"(?P<annotation>.+?)\n(?P=class_name)\.model_rebuild\(force=True\)\n?",
    re.MULTILINE | re.DOTALL,
)


def _inline_typed_extra_annotations(content: str) -> tuple[str, int]:
    """Move generated typed-extra annotations into their Pydantic classes.

    datamodel-code-generator 0.64 emits a post-class mutation of
    ``__annotations__`` followed by ``model_rebuild(force=True)`` for typed
    ``additionalProperties``. Pydantic does not rediscover fields added to
    ``__annotations__`` after class creation, so the generated model allows
    arbitrary extra values instead of validating them against the schema.
    Declaring ``__pydantic_extra__`` in the class body activates Pydantic's
    documented typed-extra validation path.
    """
    fixed = 0
    while match := _TYPED_EXTRA_ASSIGNMENT.search(content):
        class_name = match.group("class_name")
        class_headers = list(
            re.finditer(
                rf"^class {re.escape(class_name)}\b[^\n]*:\n",
                content[: match.start()],
                re.MULTILINE,
            )
        )
        if not class_headers:
            raise ValueError(
                f"Generated typed-extra assignment has no class declaration: {class_name}"
            )

        annotation_lines = match.group("annotation").splitlines()
        declaration = f"    __pydantic_extra__: {annotation_lines[0]}\n"
        declaration += "".join(f"    {line}\n" for line in annotation_lines[1:])

        insertion_offset = class_headers[-1].end()
        content = content[: match.start()] + content[match.end() :]
        content = content[:insertion_offset] + declaration + content[insertion_offset:]
        fixed += 1

    return content, fixed


def fix_typed_additional_properties() -> None:
    """Make schema-valued ``additionalProperties`` validate at runtime."""
    fixed = 0
    modified_files = 0
    for py_path in OUTPUT_DIR.rglob("*.py"):
        content = py_path.read_text()
        updated, file_fixed = _inline_typed_extra_annotations(content)
        if not file_fixed:
            continue
        py_path.write_text(updated)
        fixed += file_fixed
        modified_files += 1

    print(
        f"  Inlined {fixed} typed additionalProperties annotation(s) "
        f"across {modified_files} file(s)"
    )


def _remove_unused_pydantic_field_import(source: str) -> tuple[str, bool]:
    """Remove a generated ``Field`` import when the module never references it."""
    tree = ast.parse(source)
    if any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "Field"
        for node in ast.walk(tree)
    ):
        return source, False

    lines = source.splitlines(keepends=True)
    changed = False
    for node in reversed(list(ast.walk(tree))):
        if not isinstance(node, ast.ImportFrom) or node.module != "pydantic":
            continue
        if not any(alias.name == "Field" and alias.asname is None for alias in node.names):
            continue

        remaining = [
            alias for alias in node.names if alias.name != "Field" or alias.asname is not None
        ]
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        if remaining:
            names = ", ".join(
                alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"
                for alias in remaining
            )
            newline = "\n" if lines[end - 1].endswith("\n") else ""
            lines[start:end] = [f"from pydantic import {names}{newline}"]
        else:
            if start > 0 and not lines[start - 1].strip():
                start -= 1
            del lines[start:end]
        changed = True

    return "".join(lines), changed


def remove_unused_pydantic_field_imports() -> None:
    """Remove spurious ``Field`` imports emitted for generated enum modules."""
    modified_files = 0
    for py_path in OUTPUT_DIR.rglob("*.py"):
        source = py_path.read_text()
        updated, changed = _remove_unused_pydantic_field_import(source)
        if not changed:
            continue
        py_path.write_text(updated)
        modified_files += 1

    print(f"  Removed unused pydantic.Field imports from {modified_files} file(s)")


def _find_indented_field_block(content: str, field_name: str) -> tuple[int, int] | None:
    """Return absolute offsets for a generated four-space field block."""
    cursor = 0
    field_prefix = f"    {field_name}:"
    while cursor < len(content):
        line_end = content.find("\n", cursor)
        if line_end == -1:
            line_end = len(content)
            next_cursor = len(content)
        else:
            line_end += 1
            next_cursor = line_end

        if content.startswith(field_prefix, cursor):
            block_end = next_cursor
            scan = next_cursor
            while scan < len(content):
                next_end = content.find("\n", scan)
                if next_end == -1:
                    next_end = len(content)
                    next_scan = len(content)
                else:
                    next_end += 1
                    next_scan = next_end
                line = content[scan:next_end]
                if re.match(r"^    [a-zA-Z_]", line) or line.startswith("class "):
                    break
                block_end = next_scan
                scan = next_scan
            return cursor, block_end

        cursor = next_cursor

    return None


def fix_constr_type_annotations():
    """Normalize constrained string annotations in generated files.

    datamodel-code-generator has emitted both ``constr(pattern=...)`` and bare
    ``StringConstraints(...)`` as dict key types across releases. Pydantic's
    schema generation expects the constraint metadata to be attached to ``str``
    via ``Annotated[str, StringConstraints(...)]``.
    """
    fixed_count = 0

    for py_file in OUTPUT_DIR.rglob("*.py"):
        with open(py_file) as f:
            content = f.read()

        if "constr(" not in content and "dict[StringConstraints(" not in content:
            continue

        original = content

        # Replace constr(...) with Annotated[str, StringConstraints(...)].
        # Keep this generic: schemas use pattern, min_length, and potentially
        # other StringConstraints keyword arguments for mapping keys.
        content = re.sub(
            r"constr\(([^()]*)\)",
            r"Annotated[str, StringConstraints(\1)]",
            content,
        )

        # Replace 'constr' in imports with 'StringConstraints'
        content = re.sub(r"\bconstr\b", "StringConstraints", content)

        # Replace dict[StringConstraints(...), T] with
        # dict[Annotated[str, StringConstraints(...)], T].
        content = re.sub(
            r"dict\[StringConstraints\((.*?)\),",
            r"dict[Annotated[str, StringConstraints(\1)],",
            content,
        )

        if content != original:
            with open(py_file, "w") as f:
                f.write(content)
            fixed_count += 1

    if fixed_count > 0:
        print(f"  Normalized constrained string annotations in {fixed_count} file(s)")
    else:
        print("  No constrained string annotations needed fixing")


# Types to unwrap from RootModel to Union type alias.
# Only genuine discriminated unions (different field shapes per variant) belong here.
# "Validation-only" oneOf types (same fields, different required combos) are now
# handled at the schema level by flatten_validation_oneof() in generate_types.py,
# which produces a single BaseModel class — no RootModel or unwrapping needed.
# Removed from this set (now single classes): GetCreativeDeliveryRequest,
# GetSignalsRequest, ProvidePerformanceFeedbackRequest, SiSendMessageRequest,
# UpdateMediaBuyRequest.
# See: https://github.com/adcontextprotocol/adcp-client-python/issues/155
_UNWRAP_TO_UNION: set[str] = {
    "AcceptProposalResponse",
    "BuyProductsResponse",
    "CheckGovernanceRequest",
    "ControlMediaBuyResponse",
    "DeclineProposalsResponse",
    "ListProductsResponse",
    "MediaBuyCommitmentResponse",
    "RefineProposalsResponse",
    "RequestProposalsResponse",
    "AcquireRightsResponse",
    "ComplyTestControllerRequest",
    "ComplyTestControllerResponse",
    "ActivateSignalResponse",
    "BuildCreativeRequest",
    "BuildCreativeResponse",
    "CalibrateContentResponse",
    "CreateContentStandardsResponse",
    "CreateMediaBuyResponse",
    "CreativeApprovalResponse",
    "GetAccountFinancialsResponse",
    "GetBrandIdentityResponse",
    "GetContentStandardsResponse",
    "GetCreativeFeaturesResponse",
    "GetMediaBuyArtifactsResponse",
    "GetPlanAuditLogsRequest",
    "GetProductsRequest",
    "GetRightsResponse",
    "ListContentStandardsResponse",
    "LogEventResponse",
    "PreviewCreativeRequest",
    "PreviewCreativeResponse",
    "ProvidePerformanceFeedbackResponse",
    "SyncAccountsResponse",
    "SyncAudiencesResponse",
    "SyncGovernanceResponse",
    "SyncCatalogsResponse",
    "SyncCreativesResponse",
    "SyncEventSourcesResponse",
    "UpdateContentStandardsResponse",
    "UpdateMediaBuyResponse",
    "UpdateRightsResponse",
    "ValidateContentDeliveryResponse",
}


def unwrap_rootmodel_unions():
    """Unwrap specified RootModel unions to plain Union type aliases.

    Consumers that subclass library types cannot extend RootModel subclasses
    because Pydantic 2 forbids model_config overrides on RootModel.

    Uses AST to find class definitions instead of regex, which avoids issues
    with nested brackets in base class annotations.

    Replaces:
        class TypeName(RootModel[Variant1 | Variant2]):
            root: Annotated[Variant1 | Variant2, Field(...)]
            def __getattr__(self, name): ...

    With:
        TypeName = Variant1 | Variant2

    Note: The types in _UNWRAP_TO_UNION are all Request/Response types whose
    root: fields had no meaningful Field(description=..., examples=[...])
    metadata. Value-type RootModels that carry rich metadata are intentionally
    excluded and keep the RootModel wrapper + __getattr__ proxy.
    """
    unwrapped_count = 0

    for py_file in OUTPUT_DIR.rglob("*.py"):
        with open(py_file) as f:
            content = f.read()

        if "RootModel[" not in content:
            continue

        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        original = content
        lines = content.split("\n")

        # Collect classes to unwrap (process in reverse order to preserve line numbers)
        replacements: list[tuple[int, int, str, str]] = (
            []
        )  # (start_line, end_line, name, union_types)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name not in _UNWRAP_TO_UNION:
                continue
            if not node.end_lineno:
                continue

            # Find the RootModel[...] base class using AST source segments
            for base in node.bases:
                base_src = ast.get_source_segment(content, base)
                if not base_src or "RootModel[" not in base_src:
                    continue

                # Extract union type from RootModel[...] using bracket depth
                # to handle nested generics like RootModel[list[X] | Y]
                bracket_start = base_src.index("RootModel[") + len("RootModel[")
                depth = 1
                pos = bracket_start
                while pos < len(base_src) and depth > 0:
                    if base_src[pos] == "[":
                        depth += 1
                    elif base_src[pos] == "]":
                        depth -= 1
                    pos += 1
                bracket_end = pos - 1  # position of the matching ]
                union_types = base_src[bracket_start:bracket_end].strip()

                replacements.append((node.lineno, node.end_lineno, node.name, union_types))
                break

        if not replacements:
            continue

        # Apply replacements in reverse line order to preserve indices
        for start_line, end_line, type_name, union_types in sorted(replacements, reverse=True):
            # Wrap multi-line unions in parentheses for valid syntax
            if "\n" in union_types:
                # Re-indent continuation lines to 4 spaces
                union_lines = [ln.strip() for ln in union_types.split("\n")]
                indented = union_lines[0] + "\n" + "\n".join(f"    {ln}" for ln in union_lines[1:])
                replacement = f"{type_name} = (\n    {indented}\n)"
            else:
                replacement = f"{type_name} = {union_types}"
            lines[start_line - 1 : end_line] = [replacement]
            unwrapped_count += 1

        content = "\n".join(lines)

        if content != original:
            # Remove RootModel from imports if no longer used as a base class
            if not re.search(r"\(RootModel\[", content):
                content = re.sub(r",\s*RootModel", "", content)
                content = re.sub(r"RootModel,\s*", "", content)

            # Remove unused Any import if no longer referenced in code body
            import_line_end = content.find("\n", content.find("from typing import"))
            after_imports = content[import_line_end:] if import_line_end > 0 else ""
            if "Any" not in after_imports:
                content = re.sub(r"Any,\s*", "", content)
                content = re.sub(r",\s*Any", "", content)

            with open(py_file, "w") as f:
                f.write(content)

    if unwrapped_count > 0:
        print(f"  Unwrapped {unwrapped_count} RootModel union(s) to type aliases")
    else:
        print("  No RootModel unions needed unwrapping")


def add_rootmodel_getattr_proxy():
    """Add __getattr__ delegation to RootModel union types.

    RootModel wrappers around discriminated unions are opaque — accessing
    attributes of the inner type requires .root.attribute_name. This adds
    __getattr__ so attribute access delegates transparently to the wrapped type.

    See: https://github.com/adcontextprotocol/adcp-client-python/issues/145
    """
    fixed_count = 0

    for py_file in OUTPUT_DIR.rglob("*.py"):
        source = py_file.read_text()

        if "RootModel[" not in source:
            continue

        # Already patched
        if "def __getattr__" in source:
            continue

        # Ensure Any is imported before parsing AST (avoids line number shift)
        if "from typing import Any" not in source and "Any," not in source:
            if "from typing import " in source:
                source = source.replace("from typing import ", "from typing import Any, ", 1)
            else:
                source = "from typing import Any\n" + source

        insertions: list[int] = []
        for match in re.finditer(r"^class ([A-Za-z_]\w*)\b", source, re.MULTILINE):
            header_end = source.find(":\n", match.end())
            if header_end == -1:
                continue
            header = source[match.start() : header_end]
            if "RootModel[" not in header or "|" not in header:
                continue
            next_class = re.compile(r"^class ", re.MULTILINE).search(source, header_end + 2)
            insertions.append(next_class.start() if next_class is not None else len(source))

        if not insertions:
            continue

        # Insert __getattr__ methods (reverse order to preserve line numbers)
        method = (
            "\n"
            "    def __getattr__(self, name: str) -> Any:\n"
            '        """Proxy attribute access to the wrapped type."""\n'
            "        if name.startswith('_'):\n"
            "            raise AttributeError(name)\n"
            "        return getattr(self.root, name)\n\n"
        )

        for offset in sorted(insertions, reverse=True):
            source = source[:offset].rstrip() + method + source[offset:]

        py_file.write_text(source)
        fixed_count += len(insertions)

    if fixed_count > 0:
        print(f"  Added __getattr__ proxy to {fixed_count} RootModel union type(s)")
    else:
        print("  No RootModel union types needed __getattr__ proxy")


# Response-only list fields changed to Sequence[T] so adopters can narrow the
# element type without type: ignore[assignment] under strict mypy.  Only
# response-side fields (received, never mutated) are safe to change; request-
# side list fields (packages/creatives on request types) stay as list[T]
# because adopters call .append() on them.  See issue #624.
RESPONSE_SEQUENCE_FIELDS: list[tuple[str, str]] = [
    ("media_buy/update_media_buy_response.py", "affected_packages"),
    ("media_buy/get_media_buys_response.py", "media_buys"),
    ("media_buy/get_media_buys_response.py", "packages"),
    ("media_buy/get_media_buy_delivery_response.py", "media_buy_deliveries"),
]


def rewrite_response_list_to_sequence() -> None:
    """Change list[T] → Sequence[T] on response-only container fields.

    list[T] is invariant so ``affected_packages: list[MyPkg]`` on a subclass
    triggers mypy[assignment] against the parent's ``list[Pkg]``.  Sequence[T]
    is covariant, removing the error for adopters who extend element types.
    """
    print("Rewriting response list fields to Sequence for covariant inheritance...")

    for rel_path, field_name in RESPONSE_SEQUENCE_FIELDS:
        target = OUTPUT_DIR / rel_path
        if not target.exists():
            print(f"  {rel_path}: not found (skipping)")
            continue

        content = target.read_text()

        # Idempotency: skip if field already uses Sequence
        if re.search(rf"{re.escape(field_name)}: Annotated\[\s+Sequence\[", content):
            print(f"  {rel_path}: {field_name} already uses Sequence (skipping)")
            continue

        new_content = re.sub(
            rf"({re.escape(field_name)}: Annotated\[\s+)list\[",
            r"\1Sequence[",
            content,
        )

        if new_content == content:
            print(f"  {rel_path}: {field_name} — list[ pattern not found (skipping)")
            continue

        # Add Sequence import from collections.abc in stdlib block.
        # Anchor on the first stdlib import line (enum or typing) so Sequence
        # lands in correct alphabetical position (c < e < t).
        if "from collections.abc import Sequence" not in new_content:
            new_content = re.sub(
                r"^(from (?:enum|typing) import .+)$",
                r"from collections.abc import Sequence\n\1",
                new_content,
                count=1,
                flags=re.MULTILINE,
            )

        target.write_text(new_content)
        print(f"  {rel_path}: {field_name} → Sequence[...]")


def fix_list_field_shadowing():
    """Fix models where a field named 'list' shadows the builtin list type.

    GetPropertyListResponse has a field named 'list' which shadows the builtin
    list type in annotations like list[Identifier]. We add a _list = list alias
    before the class and replace bare list[] usage in annotations.
    """
    target = OUTPUT_DIR / "property" / "get_property_list_response.py"
    if not target.exists():
        return

    content = target.read_text()
    if "_list = list" in content:
        return  # Already fixed

    # Add alias before the class definition
    content = content.replace(
        "\n\nclass GetPropertyListResponse(",
        "\n\n_list = list  # alias to avoid shadowing by field name\n\n\nclass GetPropertyListResponse(",
    )

    # Replace bare list[] in annotations (but not the 'list' field itself)
    # Only replace list[ when used as a type annotation, not as a field name
    import re

    # Replace list[identifier...] and dict[str, list[identifier...]] patterns
    content = re.sub(
        r"(?<![._a-zA-Z])list\[identifier\.",
        "_list[identifier.",
        content,
    )

    target.write_text(content)
    print("  Fixed list field shadowing in get_property_list_response.py")


def fix_reuse_model_discriminator_bug():
    """Strip bogus ``<field>: Literal['reuse']`` subclasses.

    datamodel-code-generator bug: when ``--reuse-model`` deduplicates inlined
    copies of the same discriminated union, codegen emits subclasses like
    ``class SignalIdN(Parent): source: Literal['reuse']``. Two such subclasses
    collide on the literal ``'reuse'`` and pydantic rejects the union with
    ``Value 'reuse' for discriminator mapped to multiple choices``.

    Workaround: delete each bogus subclass and rewrite references to its
    parent. Remove once koxudaxi/datamodel-code-generator#3092 is fixed.
    """
    print("Fixing Literal['reuse'] discriminator bug from --reuse-model...")

    pattern = re.compile(
        r"\n\s*class (\w+)\((\w+)\):\n\s*\w+: Literal\['reuse'\](?: = 'reuse')?\n",
    )

    total_fixes = 0
    for py_file in OUTPUT_DIR.rglob("*.py"):
        content = py_file.read_text()
        mappings = pattern.findall(content)
        if not mappings:
            continue

        content = pattern.sub("\n", content)
        for child, parent in mappings:
            content = re.sub(rf"\b{re.escape(child)}\b", parent, content)

        py_file.write_text(content)
        rel = py_file.relative_to(OUTPUT_DIR)
        print(f"  {rel}: stripped {len(mappings)} bogus subclasses")
        total_fixes += len(mappings)

    if total_fixes == 0:
        print("  No Literal['reuse'] subclasses found")


def fix_allof_merge_field_override_conflicts() -> None:
    """Collapse ``allOf``-merge classes whose bases declare conflicting fields.

    JSON-Schema ``allOf`` arms that each constrain the same property make
    datamodel-codegen emit a multi-base class. ``postal-area.json``'s native
    arm combines ``postal-country-system.json`` (a discriminated union pinning
    ``country`` to a per-country ``Literal['US']`` and ``system`` to a
    country-local enum) with its own looser ``country: str`` /
    ``system: <full enum>`` properties::

        class PostalArea411(AdCPBaseModel):              # the loose own-props arm
            country: Annotated[str, ...]
            system: Annotated[PostalCodeSystem, ...]
            values: Annotated[list[str], ...]

        class PostalArea41(AdCPBaseModel):               # one country arm
            country: Annotated[Literal['US'], ...] = 'US'
            system: Annotated[System, ...]

        class PostalArea412(PostalArea41, PostalArea411):  # allOf merge
            country: Annotated[str, ...]                 # re-stated loose
            system: Annotated[PostalCodeSystem, ...]
            values: Annotated[list[str], ...]

    The ``allOf`` intersection of the two arms is the *narrower* country arm:
    ``country`` is ``Literal['US']``, not ``str``. The codegen output is wrong
    twice over — the two bases declare ``country`` / ``system`` with
    incompatible types (``[misc]``), and the body re-states the loose type on
    top of the narrow base (``[assignment]``).

    The conformant collapse: inherit only from the narrow country arm (selected
    by its ``Literal``-constrained shared fields), then synthesize each shared
    field from the narrow type plus every arm's ``Annotated`` metadata and
    requiredness. Keep, as local fields, fields the dropped loose base
    contributed that the narrow base lacks (here ``values``). This matters for
    helper arms such as ``items: Annotated[Any, Field(min_length=1)]``: choosing
    the concrete ``list[Item]`` type must not discard the helper arm's minimum
    length or required status.

    Pattern-based and schema-agnostic: triggers only when a class's bases
    declare some shared field with conflicting annotations. No name- or
    spec-value-specific logic, so it follows postal/geo schema churn.
    """
    import ast

    total_files = 0
    total_classes = 0

    for py_file in sorted(OUTPUT_DIR.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        source = py_file.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        classes_in_module: dict[str, ast.ClassDef] = {
            n.name: n for n in tree.body if isinstance(n, ast.ClassDef)
        }
        enum_names = {
            name
            for name, cls in classes_in_module.items()
            if any(
                isinstance(base, ast.Name) and base.id in {"Enum", "StrEnum"} for base in cls.bases
            )
        }

        def _type_expression(annotation: str) -> ast.expr:
            expression = ast.parse(annotation, mode="eval").body
            if (
                isinstance(expression, ast.Subscript)
                and isinstance(expression.value, ast.Name)
                and expression.value.id == "Annotated"
                and isinstance(expression.slice, ast.Tuple)
            ):
                return expression.slice.elts[0]
            return expression

        def _has_finite_constraint(annotation: str) -> bool:
            expression = _type_expression(annotation)
            return any(
                (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "Literal"
                )
                or (isinstance(node, ast.Name) and node.id in enum_names)
                for node in ast.walk(expression)
            )

        def _literal_values(annotation: str) -> frozenset[object] | None:
            if not annotation:
                return None
            expression = _type_expression(annotation)
            if not (
                isinstance(expression, ast.Subscript)
                and isinstance(expression.value, ast.Name)
                and expression.value.id == "Literal"
            ):
                return None
            values = (
                expression.slice.elts
                if isinstance(expression.slice, ast.Tuple)
                else [expression.slice]
            )
            try:
                return frozenset(ast.literal_eval(value) for value in values)
            except (ValueError, TypeError):
                return None

        def _contains_any(annotation: str) -> bool:
            if not annotation:
                return True
            return any(
                isinstance(node, ast.Name) and node.id == "Any"
                for node in ast.walk(_type_expression(annotation))
            )

        def _own_annotations(cls: ast.ClassDef) -> dict[str, str]:
            out: dict[str, str] = {}
            for stmt in cls.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    out[stmt.target.id] = ast.unparse(stmt.annotation)
            return out

        def _own_fields(cls: ast.ClassDef) -> dict[str, ast.AnnAssign]:
            return {
                stmt.target.id: stmt
                for stmt in cls.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }

        def _resolved_annotations(name: str, _seen: frozenset[str] = frozenset()) -> dict[str, str]:
            """Field annotations a class exposes, including those inherited
            from in-module base classes (closest definition wins, mirroring
            Python MRO). ``pass``-bodied wrapper subclasses therefore surface
            their parent's fields."""
            cls = classes_in_module.get(name)
            if cls is None or name in _seen:
                return {}
            seen = _seen | {name}
            merged: dict[str, str] = {}
            for base in reversed([b.id for b in cls.bases if isinstance(b, ast.Name)]):
                merged.update(_resolved_annotations(base, seen))
            merged.update(_own_annotations(cls))
            return merged

        annotations_by_class = {name: _resolved_annotations(name) for name in classes_in_module}

        def _resolved_fields(
            name: str, _seen: frozenset[str] = frozenset()
        ) -> dict[str, ast.AnnAssign]:
            """Resolved field declarations, retaining metadata and defaults."""
            cls = classes_in_module.get(name)
            if cls is None or name in _seen:
                return {}
            seen = _seen | {name}
            merged: dict[str, ast.AnnAssign] = {}
            for base in reversed([b.id for b in cls.bases if isinstance(b, ast.Name)]):
                merged.update(_resolved_fields(base, seen))
            merged.update(_own_fields(cls))
            return merged

        fields_by_class = {name: _resolved_fields(name) for name in classes_in_module}

        def _annotation_parts(annotation: ast.expr) -> tuple[ast.expr, list[ast.expr]]:
            if (
                isinstance(annotation, ast.Subscript)
                and isinstance(annotation.value, ast.Name)
                and annotation.value.id == "Annotated"
            ):
                elements = (
                    list(annotation.slice.elts)
                    if isinstance(annotation.slice, ast.Tuple)
                    else [annotation.slice]
                )
                if elements:
                    return elements[0], elements[1:]
            return annotation, []

        def _without_none_type(expression: ast.expr) -> ast.expr:
            """Remove generated omission-nullability from a required field type."""

            def _is_none(member: ast.expr) -> bool:
                return isinstance(member, ast.Constant) and member.value is None

            def _union_members(member: ast.expr) -> list[ast.expr]:
                if isinstance(member, ast.BinOp) and isinstance(member.op, ast.BitOr):
                    return [*_union_members(member.left), *_union_members(member.right)]
                return [member]

            if (
                isinstance(expression, ast.Subscript)
                and isinstance(expression.value, ast.Name)
                and expression.value.id == "Optional"
            ):
                return expression.slice

            if (
                isinstance(expression, ast.Subscript)
                and isinstance(expression.value, ast.Name)
                and expression.value.id == "Union"
            ):
                members = (
                    list(expression.slice.elts)
                    if isinstance(expression.slice, ast.Tuple)
                    else [expression.slice]
                )
            else:
                members = _union_members(expression)

            non_none = [member for member in members if not _is_none(member)]
            if not non_none or len(non_none) == len(members):
                return expression
            narrowed = non_none[0]
            for member in non_none[1:]:
                narrowed = ast.BinOp(left=narrowed, op=ast.BitOr(), right=member)
            return narrowed

        def _intersection_field(field: str, keep_base: str, all_bases: list[str]) -> str:
            """Render one field carrying the intersection of all base declarations.

            The concrete/narrow base supplies the type. ``Annotated`` metadata
            from every arm is retained in declaration order (with exact
            duplicates removed), and the field is required when any arm makes
            it required. JSON Schema ``allOf`` combines these properties; none
            of them may be inferred from Python's base ordering.
            """
            declarations = [
                fields_by_class[base][field]
                for base in [keep_base, *all_bases]
                if field in fields_by_class[base]
            ]
            # ``keep_base`` also occurs in ``all_bases``.
            unique_declarations: list[ast.AnnAssign] = []
            seen_declarations: set[int] = set()
            for declaration in declarations:
                marker = id(declaration)
                if marker not in seen_declarations:
                    unique_declarations.append(declaration)
                    seen_declarations.add(marker)

            kept = fields_by_class[keep_base][field]
            core_type, _ = _annotation_parts(kept.annotation)
            required_any_helper = any(
                declaration is not kept
                and declaration.value is None
                and _contains_any(ast.unparse(_annotation_parts(declaration.annotation)[0]))
                for declaration in unique_declarations
            )
            if required_any_helper:
                # datamodel-codegen uses ``T | None = None`` for a non-null
                # optional JSON property. A required allOf helper arm removes
                # omission, so retaining that synthetic None would wrongly
                # allow explicit JSON null for the underlying T property.
                core_type = _without_none_type(core_type)
            metadata: list[ast.expr] = []
            seen_metadata: set[str] = set()
            for declaration in unique_declarations:
                _, declaration_metadata = _annotation_parts(declaration.annotation)
                for item in declaration_metadata:
                    marker = ast.dump(item, include_attributes=False)
                    if marker not in seen_metadata:
                        metadata.append(item)
                        seen_metadata.add(marker)

            type_text = ast.unparse(core_type)
            if metadata:
                type_text = (
                    "Annotated["
                    + ", ".join([type_text, *(ast.unparse(item) for item in metadata)])
                    + "]"
                )

            # An absent AnnAssign value is a required Pydantic field. allOf
            # requiredness is a union: one required arm makes the intersection
            # required even when the concrete base allowed omission.
            if any(declaration.value is None for declaration in unique_declarations):
                default = ""
            else:
                assert kept.value is not None
                default = f" = {ast.unparse(kept.value)}"
            return f"    {field}: {type_text}{default}\n"

        # 1-indexed lines to delete: conflicting base-list entries and body
        # field overrides shadowing the kept base. Collected file-wide.
        drop_lines: set[int] = set()
        # (lineno, new_base_list_text) edits to the ``class X(...):`` header.
        header_edits: dict[int, str] = {}
        # Replacement fields and fields absent from the generated merge body.
        field_edits: dict[int, str] = {}
        insert_before: dict[int, list[str]] = {}
        file_classes = 0

        for cls in classes_in_module.values():
            base_names = [b.id for b in cls.bases if isinstance(b, ast.Name)]
            in_module_bases = [b for b in base_names if b in annotations_by_class]
            if len(in_module_bases) < 2:
                continue
            # A real merge conflict: two bases declare the same field with
            # different annotations. Otherwise leave the class alone.
            conflicting_fields: set[str] = set()
            for i, a in enumerate(in_module_bases):
                for b in in_module_bases[i + 1 :]:
                    shared = annotations_by_class[a].keys() & annotations_by_class[b].keys()
                    conflicting_fields.update(
                        f
                        for f in shared
                        if annotations_by_class[a][f] != annotations_by_class[b][f]
                    )
            if not conflicting_fields:
                continue

            # datamodel-codegen expands a discriminated union intersected by
            # allOf into one merge class per union branch. When multiple
            # bases pin a discriminator (commonly ``type`` or ``mode``), its
            # first base is the generated union branch and the other base is
            # the common allOf constraint. Preserve that branch identity so
            # the emitted discriminated union retains one class per
            # discriminator value; this is a distinct generator artifact,
            # not an ordering decision between a loose and narrow schema arm.
            literal_conflicts = [
                field
                for field in conflicting_fields
                if sum(
                    _literal_values(annotations_by_class[base].get(field, "")) is not None
                    for base in in_module_bases
                )
                >= 2
            ]
            union_branch_base = in_module_bases[0] if literal_conflicts else None

            # JSON Schema allOf is order-independent. Codegen's base ordering
            # is not a semantic signal, so select the arm that actually
            # carries the most finite constraints on the conflicting fields:
            # Literal fields or references to a local generated Enum. A
            # concrete conflicting annotation also outranks an ``Any``
            # placeholder (the shape used by adagents minItems helper arms).
            # If there is no unique narrow arm, stop regeneration instead of
            # silently widening validation based on whichever base happened
            # to sort first.
            narrow_scores = {
                base: (
                    sum(
                        _has_finite_constraint(annotations_by_class[base].get(field, ""))
                        for field in conflicting_fields
                        if annotations_by_class[base].get(field)
                    ),
                    sum(
                        not _contains_any(annotations_by_class[base].get(field, ""))
                        for field in conflicting_fields
                    ),
                )
                for base in in_module_bases
            }
            highest_score = max(narrow_scores.values())
            narrow_bases = (
                [union_branch_base]
                if union_branch_base is not None
                else [base for base, score in narrow_scores.items() if score == highest_score]
            )
            if highest_score == (0, 0) or len(narrow_bases) != 1:
                rel = py_file.relative_to(OUTPUT_DIR)
                raise RuntimeError(
                    "Cannot safely order allOf merge bases for "
                    f"{rel}:{cls.name}; conflicting fields="
                    f"{sorted(conflicting_fields)!r}, narrow scores={narrow_scores!r}"
                )

            keep_base = narrow_bases[0]
            kept_fields = annotations_by_class[keep_base]
            # datamodel-codegen represents property-only constraint arms as
            # ``Any`` fields. Those declarations contribute metadata and/or
            # requiredness, while the sibling arm contributes the usable
            # Python type. Ordinary concrete-vs-concrete conflicts are type
            # narrowing only and need no local re-declaration.
            intersection_fields = {
                field: _intersection_field(field, keep_base, in_module_bases)
                for field in conflicting_fields
                if any(
                    _contains_any(annotations_by_class[base].get(field, ""))
                    for base in in_module_bases
                )
            }

            # Rewrite the header to inherit only from the narrow first base.
            # Non-Name bases (e.g. RootModel[...]) are preserved verbatim.
            new_bases = [
                b.id if isinstance(b, ast.Name) and b.id in in_module_bases else ast.unparse(b)
                for b in cls.bases
            ]
            # Collapse the in-module bases down to just keep_base, in place of
            # the first occurrence; drop the rest.
            collapsed: list[str] = []
            inserted_keep = False
            for original, name in zip(cls.bases, new_bases):
                if isinstance(original, ast.Name) and original.id in in_module_bases:
                    if not inserted_keep:
                        collapsed.append(keep_base)
                        inserted_keep = True
                    continue
                collapsed.append(name)
            header_edits[cls.lineno] = f"class {cls.name}({', '.join(collapsed)}):"

            # Drop body field overrides that the kept base already declares.
            # Conflicting fields are replaced with the synthesized allOf
            # intersection so constraints from dropped bases remain active.
            emitted_intersections: set[str] = set()
            for stmt in cls.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                    continue
                if stmt.target.id in kept_fields:
                    assert stmt.end_lineno is not None
                    if stmt.target.id in intersection_fields:
                        field_edits[stmt.lineno] = intersection_fields[stmt.target.id]
                        emitted_intersections.add(stmt.target.id)
                    else:
                        drop_lines.add(stmt.lineno)
                    for line_no in range(stmt.lineno, stmt.end_lineno + 1):
                        if line_no != stmt.lineno:
                            drop_lines.add(line_no)

            missing_intersections = intersection_fields.keys() - emitted_intersections
            if missing_intersections:
                # Generated merge classes normally restate the shared fields,
                # but inserting handles pass/model_config-only wrappers too.
                first_field_line = next(
                    (
                        stmt.lineno
                        for stmt in cls.body
                        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                    ),
                    (
                        cls.body[-1].end_lineno + 1
                        if cls.body and cls.body[-1].end_lineno
                        else cls.end_lineno
                    ),
                )
                assert first_field_line is not None
                insert_before.setdefault(first_field_line, []).extend(
                    intersection_fields[field] for field in sorted(missing_intersections)
                )

            # A merge wrapper can consist entirely of redundant field
            # re-declarations. If collapsing the bases removes every body
            # statement, retain a syntactically valid empty class.
            body_has_surviving_statement = any(
                any(
                    line_no not in drop_lines
                    for line_no in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1)
                )
                for stmt in cls.body
            )
            if not body_has_surviving_statement and not missing_intersections:
                insert_before.setdefault(cls.body[0].lineno, []).append("    pass\n")
            file_classes += 1

        if not header_edits and not drop_lines:
            continue

        out_lines: list[str] = []
        for idx, line in enumerate(source.splitlines(keepends=True), start=1):
            out_lines.extend(insert_before.get(idx, []))
            if idx in drop_lines:
                continue
            if idx in header_edits:
                trailing_nl = "\n" if line.endswith("\n") else ""
                out_lines.append(header_edits[idx] + trailing_nl)
            elif idx in field_edits:
                out_lines.append(field_edits[idx])
            else:
                out_lines.append(line)
        py_file.write_text("".join(out_lines))
        total_files += 1
        total_classes += file_classes

    if total_files:
        print(f"  Collapsed {total_classes} allOf-merge class(es) across {total_files} file(s)")
    else:
        print("  No allOf-merge field override conflicts found")


def expose_account_reference_union_fields() -> None:
    """Replace generated AccountReference wrappers with their concrete arms.

    ``AccountReference`` is public as a composable object-union alias, but
    datamodel-codegen still annotates every schema reference with its outer
    ``RootModel`` class. Rewrite those generated annotations at the source so
    request, nested-input, response, and canonical-clone paths all expose the
    same concrete arm types without import-time Pydantic patching.
    """
    account_ref_source = OUTPUT_DIR / "core" / "account_ref.py"
    if not account_ref_source.exists():
        print("  account reference model not found (skipping union-field fix)")
        return

    tree = ast.parse(account_ref_source.read_text())
    wrapper = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AccountReference"
        ),
        None,
    )
    if wrapper is None:
        raise RuntimeError("generated account_ref.py has no AccountReference wrapper")

    root_base = next(
        (
            base
            for base in wrapper.bases
            if isinstance(base, ast.Subscript)
            and isinstance(base.value, ast.Name)
            and base.value.id == "RootModel"
        ),
        None,
    )
    if root_base is None:
        raise RuntimeError("generated AccountReference has no RootModel union base")

    def union_arm_names(node: ast.expr) -> list[str]:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return [*union_arm_names(node.left), *union_arm_names(node.right)]
        if isinstance(node, ast.Name):
            return [node.id]
        raise RuntimeError(
            f"generated AccountReference has an unsupported union expression: {ast.unparse(node)}"
        )

    arm_names = union_arm_names(root_base.slice)
    if len(arm_names) < 2 or len(set(arm_names)) != len(arm_names):
        raise RuntimeError(f"generated AccountReference has invalid union arms: {arm_names!r}")

    pattern = re.compile(r"\b(account_ref(?:_\d+)?)\.AccountReference\b(?!\d)")
    total_files = 0
    total_fields = 0

    for py_file in sorted(OUTPUT_DIR.rglob("*.py")):
        source = py_file.read_text()
        fixed, replacements = pattern.subn(
            lambda match: " | ".join(f"{match.group(1)}.{arm_name}" for arm_name in arm_names),
            source,
        )
        if not replacements:
            continue
        py_file.write_text(fixed)
        total_files += 1
        total_fields += replacements

    if total_fields:
        print(
            f"  Exposed AccountReference union arms in {total_fields} field(s) "
            f"across {total_files} file(s)"
        )
    else:
        print("  AccountReference field annotations already expose concrete arms")


def fix_postal_union_arm_order() -> None:
    """Prefer the legacy postal arm when a payload omits ``country``.

    The generated native arm contains country-specific models whose ``country``
    fields have defaults. When that arm appears first, a legacy payload such as
    ``{"system": "us_zip", ...}`` is accepted as native and serializes with an
    injected ``country`` that is incompatible with the retained fused system.
    The legacy arm forbids extra fields, so putting it first is safe: native
    payloads with ``country`` fall through to the native arm.
    """
    target = OUTPUT_DIR / "core" / "postal_area.py"
    if not target.exists():
        print("  postal area model not found (skipping arm-order fix)")
        return

    source = target.read_text()
    old = "PostalArea1 | PostalArea2"
    new = "PostalArea2 | PostalArea1"
    replacements = source.count(old)
    if replacements:
        target.write_text(source.replace(old, new))
        print(f"  core/postal_area.py: reordered {replacements} postal union annotation(s)")
    elif new in source:
        print("  postal area union already prefers the legacy arm")
    else:
        raise RuntimeError("generated postal_area.py has an unexpected outer union shape")


def fix_postal_country_system_pairing() -> None:
    """Restore postal country/system pairing dropped by model generation.

    JSON Schema expresses the pairing as ``anyOf`` arms plus an open-country
    fallback. datamodel-codegen flattens each arm's referenced postal-system
    enum before intersecting it with the arm-local constraint, so generated
    Pydantic models accept combinations such as ``US`` + ``plz``. Inject one
    before-validator on the stable outer ``PostalArea`` model, deriving every
    pairing from the bundled schema rather than duplicating spec values here.
    """
    target = OUTPUT_DIR / "core" / "postal_area.py"
    schema_path = SCHEMA_DIR / "core" / "postal-country-system.json"
    marker = "def _validate_country_system_pairing("
    if not target.exists() or not schema_path.exists():
        print("  postal area model/schema not found (skipping)")
        return

    source = target.read_text()
    if marker in source:
        print("  postal area country/system pairing already enforced")
        return

    schema = json.loads(schema_path.read_text())
    allowed_by_country: dict[str, tuple[str, ...]] = {}
    fallback_systems: tuple[str, ...] | None = None
    fallback_excludes: set[str] | None = None

    for arm in schema.get("anyOf", []):
        properties = arm.get("properties", {})
        country_schema = properties.get("country", {})
        system_schema = properties.get("system", {})
        countries = (
            [country_schema["const"]] if "const" in country_schema else country_schema.get("enum")
        )
        systems = (
            [system_schema["const"]] if "const" in system_schema else system_schema.get("enum")
        )
        if not systems:
            raise RuntimeError(f"postal pairing arm has no system constraint: {arm!r}")
        normalized_systems = tuple(str(value) for value in systems)
        if countries:
            for country in countries:
                allowed_by_country[str(country)] = normalized_systems
            continue

        excluded = country_schema.get("not", {}).get("enum")
        if not excluded:
            raise RuntimeError(f"postal pairing fallback has no country exclusion: {arm!r}")
        fallback_excludes = {str(value) for value in excluded}
        fallback_systems = normalized_systems

    if not allowed_by_country or fallback_systems is None or fallback_excludes is None:
        raise RuntimeError("postal-country-system schema has no complete pairing/fallback arms")
    if fallback_excludes != set(allowed_by_country):
        raise RuntimeError(
            "postal-country-system fallback exclusions do not match registered countries: "
            f"registered={sorted(allowed_by_country)!r}, "
            f"excluded={sorted(fallback_excludes)!r}"
        )

    tree = ast.parse(source)
    postal_cls = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "PostalArea"
        ),
        None,
    )
    if postal_cls is None:
        raise RuntimeError("generated postal_area.py has no outer PostalArea class")
    first_method = next(
        (
            node
            for node in postal_cls.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
        None,
    )
    if first_method is None:
        assert postal_cls.end_lineno is not None
        insertion_line = postal_cls.end_lineno + 1
    else:
        insertion_line = first_method.lineno

    if "model_validator" not in source:
        source, count = re.subn(
            r"from pydantic import ([^\n]+)",
            lambda match: f"from pydantic import {match.group(1)}, model_validator",
            source,
            count=1,
        )
        if count != 1:
            raise RuntimeError("generated postal_area.py has no pydantic import to extend")

    validator = f"""    @model_validator(mode='before')
    @classmethod
    def _validate_country_system_pairing(cls, value: Any) -> Any:
        raw = value.get('root', value) if isinstance(value, dict) else value
        country = raw.get('country') if isinstance(raw, dict) else getattr(raw, 'country', None)
        if not isinstance(country, str):
            return value
        system = raw.get('system') if isinstance(raw, dict) else getattr(raw, 'system', None)
        system = getattr(system, 'value', system)
        if not isinstance(system, str):
            return value
        allowed_by_country = {allowed_by_country!r}
        allowed = allowed_by_country.get(country, {fallback_systems!r})
        if system not in allowed:
            raise ValueError(
                f"postal system {{system!r}} is not valid for country {{country!r}}; "
                f"expected one of {{list(allowed)!r}}"
            )
        return value

"""
    lines = source.splitlines(keepends=True)
    lines.insert(insertion_line - 1, validator)
    target.write_text("".join(lines))
    print("  postal area country/system pairing validator injected")


def fix_adagents_duplicate_aliases() -> None:
    """Collapse duplicate adagents wrapper subclasses to type aliases.

    The rc.9 adagents schema produces repeated wrapper classes with equivalent
    shapes. Subclasses then narrow inherited list fields to those duplicate
    wrappers, which is rejected by strict mypy because ``list`` is invariant.
    Aliasing the duplicates to the canonical generated classes preserves the
    runtime model while keeping downstream field overrides type-compatible.
    """
    target = OUTPUT_DIR / "adagents.py"
    if not target.exists():
        print("  adagents.py: not found (skipping)")
        return

    source = target.read_text()
    changed = 0
    for suffix in range(1, 7):
        name = f"RevokedPublisherDomain{suffix}"
        source, count = re.subn(
            rf"\n\nclass {name}\(RevokedPublisherDomain\):\n    pass\n",
            f"\n\n{name} = RevokedPublisherDomain\n",
            source,
            count=1,
        )
        changed += count

    for suffix in (7, 14, 21, 28, 35, 42):
        name = f"AuthorizedAgents{suffix}"
        marker = f"\n\nclass {name}("
        start = source.find(marker)
        if start == -1:
            continue
        next_class = source.find("\n\nclass ", start + len(marker))
        if next_class == -1:
            continue
        source = source[:start] + f"\n\n{name} = AuthorizedAgents\n" + source[next_class:]
        changed += 1

    if changed:
        target.write_text(source)
        print(f"  adagents.py: collapsed {changed} duplicate wrapper class(es)")
    else:
        print("  adagents.py: no duplicate wrapper classes found")


def restore_format_category_deprecation_shim():
    """Restore the removed-type ``format_category`` module after codegen.

    ``scripts/generate_types.py`` wipes ``generated_poc/`` before
    regenerating. The deprecation shim file for the removed
    ``format_category`` submodule lives inside that tree so downstream
    imports of ``adcp.types.generated_poc.enums.format_category`` hit an
    ``ImportError`` with a migration pointer instead of
    ``ModuleNotFoundError``. This function re-writes the shim after each
    regen. Keep the message in sync with ``_REMOVED_IN_V4`` in
    ``src/adcp/__init__.py``.
    """
    target = OUTPUT_DIR / "enums" / "format_category.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (
        '"""Deprecation shim for the removed ``format_category`` submodule.\n'
        "\n"
        "``FormatCategory`` was replaced by free-form ``FormatId`` strings in\n"
        "AdCP 3.0. See MIGRATION_v3_to_v4.md for the full migration path.\n"
        "\n"
        "Importing this module raises :class:`ImportError` with a pointer to the\n"
        "migration guide — so downstream import sites like::\n"
        "\n"
        "    from adcp.types.generated_poc.enums.format_category import FormatCategory\n"
        "\n"
        "get the same pointer as the top-level ``from adcp import FormatCategory``,\n"
        "instead of a bare ``ModuleNotFoundError``.\n"
        "\n"
        "This file is restored after every codegen run by\n"
        "``scripts/post_generate_fixes.py`` (which wipes ``generated_poc/``).\n"
        '"""\n'
        "\n"
        "raise ImportError(\n"
        '    "adcp.types.generated_poc.enums.format_category was removed in AdCP 3.0. "\n'
        "    \"Use free-form format-id strings (e.g. 'goog:video_responsive_ad') via \"\n"
        '    "adcp.types.FormatId. See MIGRATION_v3_to_v4.md#creative-format-asset-slots-formataasset-aliases "\n'
        '    "for details."\n'
        ")\n"
    )
    target.write_text(content)
    rel = target.relative_to(REPO_ROOT)
    print(f"  ✓ Restored format_category deprecation shim at {rel}")


def inject_literal_discriminator_defaults() -> None:
    """Inject defaults for ``Literal[<single-value>]`` required fields.

    AdCP's schema marks discriminator fields like ``asset_type``,
    ``delivery_type``, ``pricing_model`` as required even though the
    field's type is ``Literal[<one-value>]`` — the spec's intent is
    "this field MUST be this one tag", not "the user MUST type the
    tag out by hand". Pydantic takes the spec literally and generates
    the field as required, breaking ergonomic construction::

        # Spec-literal generated shape:
        text = TextAsset(asset_type="text", content="hello")

        # With this fix:
        text = TextAsset(content="hello")  # asset_type defaults to "text"

    Wire consumption is unchanged — the ``Literal`` type still rejects
    any value other than the tag, and validating a dict from the wire
    still populates the field the same way. This only affects
    in-process construction ergonomics.

    The fix is pattern-based: any ``AnnAssign`` whose annotation is
    ``Literal['x']`` (or ``Annotated[Literal['x'], ...]``) and has no
    default gets ``= 'x'`` appended. No spec-value-specific logic.
    Robust to spec churn as long as the single-value-Literal pattern
    holds.
    """
    import ast

    total_classes = 0
    total_fields = 0
    total_files = 0

    for py_file in sorted(OUTPUT_DIR.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue

        source = py_file.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        edits: list[tuple[int, str]] = []  # (end_lineno, literal_value)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            class_has_edit = False
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if stmt.value is not None:
                    continue  # already has a default
                if (
                    node.name in {"CreateMediaBuyResponse1", "UpdateMediaBuyResponse1"}
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "status"
                ):
                    continue
                literal_value = _extract_single_literal_value(stmt.annotation)
                if literal_value is None:
                    continue
                # ast end_lineno is 1-indexed, inclusive. The annotation
                # ends on that line; we want to append " = '<value>'" at
                # end of that line.
                edits.append((stmt.end_lineno, literal_value))
                total_fields += 1
                class_has_edit = True
            if class_has_edit:
                total_classes += 1

        if not edits:
            continue

        # Apply line-based edits. Sort descending so earlier edits don't
        # shift the line indices of later ones (we're editing in place
        # without inserting new lines, but defensive).
        lines = source.split("\n")
        for end_lineno, value in sorted(edits, key=lambda e: -e[0]):
            idx = end_lineno - 1  # 0-indexed
            # Escape the literal value as a Python string literal.
            # datamodel-codegen only emits str-valued Literals for
            # discriminators in AdCP schemas; if the value isn't a str,
            # skip conservatively.
            if not isinstance(value, str):
                continue
            escaped = repr(value)
            lines[idx] = f"{lines[idx]} = {escaped}"

        py_file.write_text("\n".join(lines))
        total_files += 1

    print(
        f"  ✓ Injected Literal[<single-value>] defaults: "
        f"{total_fields} fields across {total_classes} classes in {total_files} files"
    )


def _extract_single_literal_value(annotation: ast.AST) -> object | None:
    """Return the single Literal value if the annotation is effectively
    ``Literal[X]`` (optionally wrapped in ``Annotated[...]``); else None.

    Handles both shapes datamodel-codegen emits:

    * ``Literal['text']`` — bare Literal
    * ``Annotated[Literal['text'], Field(...)]`` — wrapped in Annotated
      with a Field descriptor (the typical discriminator shape)

    Returns None if the annotation is a Literal with multiple values,
    a Literal over non-strings, or anything else. We only want to
    auto-default the unambiguous single-tag case.
    """
    import ast

    # Unwrap Annotated[X, ...] → X
    if isinstance(annotation, ast.Subscript) and _subscript_base_name(annotation) == "Annotated":
        inner = _first_subscript_arg(annotation)
        if inner is None:
            return None
        annotation = inner

    # Expect Literal[X]
    if not isinstance(annotation, ast.Subscript):
        return None
    if _subscript_base_name(annotation) != "Literal":
        return None

    # Extract the subscript arg(s). Single-value case only.
    # ast.Subscript.slice in 3.9+ is the value directly (not a Tuple for single).
    slice_node = annotation.slice
    if isinstance(slice_node, ast.Tuple):
        # Literal['a', 'b'] — multiple values, skip
        return None
    if not isinstance(slice_node, ast.Constant):
        return None
    return slice_node.value


def _subscript_base_name(node: ast.Subscript) -> str | None:
    """Return the subscripted name (e.g. 'Literal' for Literal['x'])."""
    import ast

    value = node.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _first_subscript_arg(node: ast.Subscript) -> ast.AST | None:
    """Return the first argument of a subscript. For Annotated[X, ...]
    this is X; for single-arg Literal[X] this is the constant node."""
    import ast

    slice_node = node.slice
    if isinstance(slice_node, ast.Tuple):
        return slice_node.elts[0] if slice_node.elts else None
    return slice_node


# ---------------------------------------------------------------------------
# #624: widen documented extension-point list[X] fields to Sequence[X].
#
# Adopters who follow Critical Pattern #1 (subclass a library response type
# and override the parent's list field with a more specific element type)
# hit `# type: ignore[assignment]` on every override under mypy --strict —
# list is invariant in its element type. Sequence is covariant, so a
# Sequence[Parent] parent permits list[Child] override cleanly.
#
# Scope is intentionally narrow: only fields the SDK documents as
# extension points (response payloads adopters routinely subclass, plus
# request bodies that compose extendable sub-records like packages and
# creatives). Internal scalars stay as list.
#
# Allowlist format: (class_name, field_name). datamodel-codegen emits
# bundled response files that each inline copies of subordinate types
# (Placement, TargetingOverlay, etc.); the rewriter walks every generated
# .py file and applies the substitution to every emission of the named
# (class, field) pair so all copies stay consistent.

_SEQUENCE_EXTENSION_POINTS: list[tuple[str, str]] = [
    # Response payloads adopters subclass to add internal-only fields.
    # `UpdateMediaBuySuccessResponse` is the success variant of the
    # `UpdateMediaBuyResponse` discriminated union — emitted as
    # `UpdateMediaBuyResponse1`.
    ("UpdateMediaBuyResponse1", "affected_packages"),
    ("GetMediaBuyDeliveryResponse", "media_buy_deliveries"),
    ("GetCreativeDeliveryResponse", "creatives"),
    ("Signal", "deployments"),
    ("GetSignalsResponse", "signals"),
    ("GetMediaBuysResponse", "media_buys"),
    ("ListCreativesResponse", "creatives"),
    # Request bodies that carry extendable sub-records — adopters subclass
    # the inner record type and need to override the list element type.
    ("PackageRequest", "creatives"),
    ("CreateMediaBuyRequest", "packages"),
    ("UpdateMediaBuyRequest", "packages"),
    # Cross-cutting record types referenced from multiple responses; each
    # bundled response file inlines its own copy. The walker rewrites
    # every emission.
    ("Placement", "format_ids"),
    ("TargetingOverlay", "geo_countries_exclude"),
    ("TargetingOverlay", "geo_regions_exclude"),
    ("TargetingOverlay", "geo_metros_exclude"),
    ("TargetingOverlay", "geo_postal_areas_exclude"),
]


def widen_extension_point_lists_to_sequence():
    """Rewrite ``list[X]`` to ``Sequence[X]`` on documented extension-point fields.

    Walks every generated ``.py`` file under :data:`OUTPUT_DIR`. For each
    file, applies every ``(class, field)`` pair in
    :data:`_SEQUENCE_EXTENSION_POINTS` that matches a class declaration
    in that file. The same ``(class, field)`` pair commonly appears in
    multiple files because bundled response emission inlines copies of
    subordinate types — every emission is rewritten so all paths stay
    consistent. Each rewritten file gets ``from collections.abc import
    Sequence`` added if it isn't already present.

    Pairs that produce zero rewrites across the whole tree emit a WARN
    so allowlist drift surfaces fast (a renamed field or removed class
    means the override pattern this entry was protecting no longer
    exists).

    See `adcp-client-python#624 <https://github.com/adcontextprotocol/adcp-client-python/issues/624>`_
    for the design rationale and the spike that validated the Pydantic
    plugin accepts ``Sequence[Parent]`` parent + ``list[Child]`` child
    override under mypy --strict.
    """
    print("Widening extension-point list[X] fields to Sequence[X] (#624)...")

    # Track total rewrites per (class, field) — a pair with zero hits is
    # a stale allowlist entry and surfaces as a WARN.
    # Track per-pair state across all files:
    #   rewrites: how many list[X] sites were rewritten this run
    #   already_widened: how many sites are already in Sequence[X] form
    # A pair with rewrites == 0 AND already_widened == 0 is genuinely stale
    # (field renamed/removed) and warrants a WARN. A pair with already_widened
    # > 0 is silent — that's the steady-state idempotent run.
    rewrites_per_pair: dict[tuple[str, str], int] = {pair: 0 for pair in _SEQUENCE_EXTENSION_POINTS}
    already_per_pair: dict[tuple[str, str], int] = {pair: 0 for pair in _SEQUENCE_EXTENSION_POINTS}
    files_touched = 0
    total_widened = 0

    for file_path in sorted(OUTPUT_DIR.rglob("*.py")):
        original = file_path.read_text()
        content = original
        widened_in_file = 0

        for class_name, field_name in _SEQUENCE_EXTENSION_POINTS:
            # Quick filter — skip files that don't declare this class.
            if f"class {class_name}(" not in content and f"class {class_name}:" not in content:
                continue
            new_content, did_widen = _widen_field_annotation(content, class_name, field_name)
            if did_widen:
                content = new_content
                widened_in_file += 1
                rewrites_per_pair[(class_name, field_name)] += 1
            elif _field_already_widened(content, class_name, field_name):
                already_per_pair[(class_name, field_name)] += 1

        if widened_in_file == 0:
            continue

        content = _ensure_sequence_import(content)
        file_path.write_text(content)
        files_touched += 1
        total_widened += widened_in_file
        print(f"  ✓ {file_path.relative_to(OUTPUT_DIR)}: widened {widened_in_file} field(s)")

    stale = [
        pair
        for pair in _SEQUENCE_EXTENSION_POINTS
        if rewrites_per_pair[pair] == 0 and already_per_pair[pair] == 0
    ]
    for class_name, field_name in stale:
        print(
            f"  WARN: {class_name}.{field_name} — neither list[X] nor Sequence[X] "
            "found in any generated file (field renamed or removed?)"
        )

    if total_widened == 0:
        print("  No extension-point fields to widen")
    else:
        print(
            f"  ✓ Widened {total_widened} extension-point field(s) across {files_touched} file(s)"
        )


def _widen_field_annotation(content: str, class_name: str, field_name: str) -> tuple[str, bool]:
    """Rewrite ``list[X]`` → ``Sequence[X]`` in one field's annotation.

    Locates ``class {class_name}(...):`` then walks forward to the first
    ``    {field_name}:`` line at class-body indentation, **bounded to the
    target class** so a same-named field on a later class in the same
    file cannot mis-match. Within the AnnAssign's annotation block (which
    may span multiple lines for ``Annotated[..., Field(...)]``), replaces
    the first ``list[`` with ``Sequence[``. Idempotent — a second pass
    over already-widened content is a no-op.
    """
    block = _find_class_field_block(content, class_name, field_name)
    if block is None:
        return content, False
    annotation_start, annotation_end = block
    annotation_block = content[annotation_start:annotation_end]
    # Replace the first list[ inside the annotation only. Generated
    # annotations always have `list[X]` as the outer container; the
    # narrow scope of the allowlist (no `dict[str, list[X]]` entries)
    # makes this safe in practice. If a future entry has nested list,
    # this needs to anchor on the outer container explicitly.
    new_annotation = re.sub(r"\blist\[", "Sequence[", annotation_block, count=1)
    if new_annotation == annotation_block:
        return content, False

    new_content = content[:annotation_start] + new_annotation + content[annotation_end:]
    return new_content, True


def _field_already_widened(content: str, class_name: str, field_name: str) -> bool:
    """Return True when the named field's annotation is already ``Sequence[X]``.

    Used to silence the WARN on idempotent re-runs: a pair that's already
    widened is the steady state, not allowlist drift.
    """
    block = _find_class_field_block(content, class_name, field_name)
    if block is None:
        return False
    annotation_start, annotation_end = block
    return "Sequence[" in content[annotation_start:annotation_end]


def _find_class_field_block(
    content: str, class_name: str, field_name: str
) -> tuple[int, int] | None:
    """Return absolute offsets for one generated class field annotation block.

    This deliberately avoids a DOTALL regex over large generated modules. Some
    bundled beta schemas produce megabyte-scale Python files, and repeatedly
    running lazy cross-line regexes against them can dominate regeneration.
    """
    class_match = re.search(rf"^class {re.escape(class_name)}\b", content, re.MULTILINE)
    if class_match is None:
        return None
    class_body_start = class_match.end()
    next_class = re.compile(r"^class ", re.MULTILINE).search(content, class_body_start)
    region_end = next_class.start() if next_class is not None else len(content)

    cursor = class_body_start
    field_prefix = f"    {field_name}: "
    while cursor < region_end:
        line_end = content.find("\n", cursor, region_end)
        if line_end == -1:
            line_end = region_end
            next_cursor = region_end
        else:
            line_end += 1
            next_cursor = line_end

        if content.startswith(field_prefix, cursor):
            annotation_start = cursor + len(field_prefix)
            block_end = next_cursor
            scan = next_cursor
            while scan < region_end:
                next_end = content.find("\n", scan, region_end)
                if next_end == -1:
                    next_end = region_end
                    next_scan = region_end
                else:
                    next_end += 1
                    next_scan = next_end
                line = content[scan:next_end]
                if re.match(r"^    [a-zA-Z_]", line):
                    break
                block_end = next_scan
                scan = next_scan
            return annotation_start, block_end

        cursor = next_cursor

    return None


def _ensure_sequence_import(content: str) -> str:
    """Add ``from collections.abc import Sequence`` if not already present.

    Inserts after the ``from __future__ import annotations`` line so the
    import sits with sibling stdlib imports rather than landing at the top
    of the file.
    """
    if "from collections.abc import Sequence" in content:
        return content
    # If `collections.abc` is already imported, extend the import line.
    extend_pattern = re.compile(r"^from collections\.abc import ([^\n]+)$", re.MULTILINE)
    match = extend_pattern.search(content)
    if match is not None:
        existing = match.group(1)
        # Maintain alphabetical order if the existing import is sorted.
        names = sorted({*[n.strip() for n in existing.split(",")], "Sequence"})
        new_line = f"from collections.abc import {', '.join(names)}"
        return content[: match.start()] + new_line + content[match.end() :]

    # Otherwise insert before the typing imports block. Codegen emits a
    # ``from typing import ...`` line near the top, so anchor on it.
    typing_pattern = re.compile(r"^from typing import [^\n]+$", re.MULTILINE)
    match = typing_pattern.search(content)
    if match is not None:
        return (
            content[: match.start()]
            + "from collections.abc import Sequence\n"
            + content[match.start() :]
        )

    # Fallback: prepend after the `from __future__` line.
    future_pattern = re.compile(r"^from __future__ import annotations$", re.MULTILINE)
    match = future_pattern.search(content)
    if match is not None:
        return (
            content[: match.end()]
            + "\n\nfrom collections.abc import Sequence"
            + content[match.end() :]
        )

    return "from collections.abc import Sequence\n\n" + content


# Matches the four request-type 'canceled: Literal[True] = True' emissions.
# datamodel-codegen emits '= True' directly from "const": true boolean
# schema properties — it is NOT produced by inject_literal_discriminator_defaults()
# (which already skips bool-valued Literals). Each match rewrites only the
# annotation and the default; the Field description and the rest of the class
# are untouched. The regex is inherently idempotent: 'Literal[True] | None,'
# does not match 'Literal[True],' so a second pass is a no-op.
_CANCELED_FIELD_RE = re.compile(
    r"(    canceled: Annotated\[\n        )"
    r"Literal\[True\]"
    r"(,\n        Field\(\n            description='Cancel[^']*'\n        \),\n    \])"
    r" = True"
)


_UNCHANGED_FIELD_RE = re.compile(
    r"(    unchanged: Annotated\[\n        )"
    r"Literal\[True\]"
    r"(,\n        Field\(\n            description=.*?\n        \),\n    \])"
    r" = True",
    re.DOTALL,
)


def fix_canceled_literal_defaults() -> None:
    """Widen ``canceled: Literal[True] = True`` on request types to ``Literal[True] | None = None``.

    Issue #641: the generated ``= True`` default silently cancels media buys /
    packages when a buyer omits the field from an update request. Changing to
    ``Literal[True] | None = None`` preserves wire semantics (the field still
    only accepts ``true`` when present) while making omission non-destructive.

    Response-side ``canceled: bool | None = False`` fields (status indicators
    like ``core/package.py``) are out of scope — their default is already safe.

    Root cause: ``datamodel-codegen`` emits ``= True`` from the schema's
    ``"const": true`` boolean property. This function corrects that misfire for
    the four request-type emissions listed below.
    """
    targets = [
        OUTPUT_DIR / "media_buy/update_media_buy_request.py",
        OUTPUT_DIR / "media_buy/package_update.py",
        OUTPUT_DIR / "bundled/media_buy/update_media_buy_request.py",
    ]

    total_fixed = 0
    for py_file in targets:
        if not py_file.exists():
            print(f"  {py_file.relative_to(OUTPUT_DIR)}: not found (skipping)")
            continue

        source = py_file.read_text()
        new_source, count = _CANCELED_FIELD_RE.subn(
            r"\1Literal[True] | None\2 = None",
            source,
        )

        if count == 0:
            print(f"  {py_file.relative_to(OUTPUT_DIR)}: no destructive canceled defaults found")
            continue

        py_file.write_text(new_source)
        total_fixed += count
        print(f"  {py_file.relative_to(OUTPUT_DIR)}: fixed {count} canceled field(s)")

    if total_fixed > 0:
        print(f"  ✓ Widened {total_fixed} canceled Literal[True] default(s) to None")
    else:
        print("  No canceled field defaults needed fixing")


def fix_unchanged_literal_defaults() -> None:
    """Make wholesale ``unchanged`` an opt-in field, not a default.

    The schema's ``const: true`` means "if present, this must be true"; it
    does not mean normal wholesale responses should default to unchanged.
    datamodel-code-generator emits ``Literal[True] = True``, which makes
    parsed responses with products/signals look like cache-hit probes. Use
    ``None`` as the default so ``exclude_none=True`` preserves the one-shape
    contract: absence means changed payload, presence means unchanged.
    """
    targets = [
        OUTPUT_DIR / "media_buy" / "get_products_response.py",
        OUTPUT_DIR / "signals" / "get_signals_response.py",
        OUTPUT_DIR / "bundled" / "media_buy" / "get_products_response.py",
        OUTPUT_DIR / "bundled" / "signals" / "get_signals_response.py",
    ]

    total_fixed = 0
    for py_file in targets:
        if not py_file.exists():
            print(f"  {py_file.relative_to(OUTPUT_DIR)}: not found (skipping)")
            continue

        source = py_file.read_text()
        new_source, count = _UNCHANGED_FIELD_RE.subn(
            r"\1Literal[True] | None\2 = None",
            source,
        )

        if count == 0:
            print(f"  {py_file.relative_to(OUTPUT_DIR)}: no unchanged defaults found")
            continue

        py_file.write_text(new_source)
        total_fixed += count
        print(f"  {py_file.relative_to(OUTPUT_DIR)}: fixed {count} unchanged field(s)")

    if total_fixed > 0:
        print(f"  ✓ Widened {total_fixed} unchanged Literal[True] default(s) to None")
    else:
        print("  No unchanged field defaults needed fixing")


def fix_protocol_envelope_status_default() -> None:
    """Default response envelope status to completed for ergonomic construction.

    AdCP 3.1 requires ``status`` on the wire. SDK users constructing typed
    synchronous response models in-process historically omitted it, so give the
    generated base envelope a default while server serialization still emits it.
    """
    target = OUTPUT_DIR / "core" / "protocol_envelope.py"
    if not target.exists():
        print("  core/protocol_envelope.py not found (skipping status default)")
        return

    source = target.read_text()
    if "status: Annotated[\n        task_status.TaskStatus," not in source:
        print("  core/protocol_envelope.py status annotation not found")
        return
    status_start = source.find("    status: Annotated[\n        task_status.TaskStatus,")
    message_start = source.find("\n    message:", status_start)
    if status_start == -1 or message_start == -1:
        print("  core/protocol_envelope.py status annotation not found")
        return
    status_block = source[status_start:message_start]
    if status_block.rstrip().endswith("= task_status.TaskStatus.completed"):
        print("  core/protocol_envelope.py status default already fixed")
        return

    new_block = status_block.rstrip() + " = task_status.TaskStatus.completed"
    new_source = source[:status_start] + new_block + source[message_start:]

    target.write_text(new_source)
    print("  core/protocol_envelope.py: defaulted status to completed")


def fix_trusted_match_runtime_validators() -> None:
    """Restore Trusted Match constraints that datamodel-codegen does not model.

    JSON Schema conditionals and map-key descriptions are either flattened or
    not represented in the generated Pydantic models. Keep the generated types
    aligned with the schema's normative field descriptions.
    """

    provider_registration = OUTPUT_DIR / "trusted_match" / "provider_registration.py"
    if provider_registration.exists():
        source = provider_registration.read_text()
        if "_require_https_endpoint" in source:
            print("  trusted_match/provider_registration.py validators already fixed")
        else:
            if "from pydantic import AnyUrl, ConfigDict, Field, RootModel" in source:
                source = source.replace(
                    "from pydantic import AnyUrl, ConfigDict, Field, RootModel",
                    "from pydantic import AnyUrl, ConfigDict, Field, RootModel, field_validator, model_validator",
                    1,
                )
            else:
                print("  trusted_match/provider_registration.py pydantic import shape not found")
                source = ""

            if source:
                validators_1 = """

    @field_validator('endpoint')
    @classmethod
    def _require_https_endpoint(cls, value: AnyUrl) -> AnyUrl:
        if value.scheme != 'https':
            raise ValueError('endpoint must use https')
        return value

    @model_validator(mode='after')
    def _require_identity_match_dimensions(self) -> TmpProviderRegistration1:
        if self.identity_match is True:
            if not self.countries:
                raise ValueError('countries is required when identity_match is true')
            if not self.uid_types:
                raise ValueError('uid_types is required when identity_match is true')
        return self
"""
                validators_2 = validators_1.replace(
                    "TmpProviderRegistration1", "TmpProviderRegistration2"
                )
                source = source.replace(
                    "\n\nclass TmpProviderRegistration2(AdCPBaseModel):",
                    validators_1 + "\n\nclass TmpProviderRegistration2(AdCPBaseModel):",
                    1,
                )
                source = source.replace(
                    "\n\nclass TmpProviderRegistration(RootModel[TmpProviderRegistration1 | TmpProviderRegistration2]):",
                    validators_2
                    + "\n\nclass TmpProviderRegistration(RootModel[TmpProviderRegistration1 | TmpProviderRegistration2]):",
                    1,
                )
                provider_registration.write_text(source)
                print("  trusted_match/provider_registration.py: added runtime validators")
    else:
        print("  trusted_match/provider_registration.py not found (skipping)")

    identity_match_response = OUTPUT_DIR / "trusted_match" / "identity_match_response.py"
    if not identity_match_response.exists():
        print("  trusted_match/identity_match_response.py not found (skipping)")
        return

    source = identity_match_response.read_text()
    if "_validate_tmpx_provider_ids" in source:
        changed = False
        version_import = "from ..core.version_envelope import AdcpVersionEnvelope\n"
        if "_PROVIDER_ID_PATTERN =" not in source:
            source = source.replace(
                version_import,
                version_import + "\n_PROVIDER_ID_PATTERN = re.compile(r'^[A-Za-z0-9_]{1,64}$')\n",
                1,
            )
            changed = True
        if "class IdentityMatchResponseRouterPublisher(" in source:
            corrected = source.replace(
                "def _validate_tmpx_provider_ids(self) -> IdentityMatchResponse:",
                "def _validate_tmpx_provider_ids(self) -> IdentityMatchResponseRouterPublisher:",
                1,
            )
            changed = changed or corrected != source
            source = corrected
        if changed:
            identity_match_response.write_text(source)
            print("  trusted_match/identity_match_response.py validators repaired")
        else:
            print("  trusted_match/identity_match_response.py validators already fixed")
        return

    if "\nimport re\n" not in source:
        source = source.replace(
            "from __future__ import annotations\n\n",
            "from __future__ import annotations\n\nimport re\n\n",
            1,
        )
    if "from pydantic import ConfigDict, Field" in source:
        source = source.replace(
            "from pydantic import ConfigDict, Field",
            "from pydantic import ConfigDict, Field, model_validator",
            1,
        )
    else:
        print("  trusted_match/identity_match_response.py pydantic import shape not found")
        return

    version_import = "from ..core.version_envelope import AdcpVersionEnvelope\n"
    source = source.replace(
        version_import,
        version_import + "\n_PROVIDER_ID_PATTERN = re.compile(r'^[A-Za-z0-9_]{1,64}$')\n",
        1,
    )
    response_class_match = re.search(
        r"^class (IdentityMatchResponse(?:RouterPublisher)?)\(", source, re.MULTILINE
    )
    if response_class_match is None:
        print("  trusted_match/identity_match_response.py response class not found")
        return
    response_class = response_class_match.group(1)
    source = (
        source.rstrip()
        + f"""

    @model_validator(mode='after')
    def _validate_tmpx_provider_ids(self) -> {response_class}:
        if self.tmpx_providers is None:
            return self
        invalid = [
            provider_id
            for provider_id in self.tmpx_providers
            if not _PROVIDER_ID_PATTERN.fullmatch(provider_id)
        ]
        if invalid:
            raise ValueError('tmpx_providers keys must be valid provider_id values')
        return self
"""
    )
    identity_match_response.write_text(source.rstrip() + "\n")
    print("  trusted_match/identity_match_response.py: added runtime validators")


def fix_beta3_secure_url_constraints() -> None:
    """Restore beta.3 URL constraints dropped for ``format: uri`` fields.

    datamodel-code-generator maps these fields to ``AnyUrl`` but omits their
    accompanying JSON Schema patterns. These URLs cross network or executable
    provenance trust boundaries, so the generated runtime models must retain
    the HTTPS and GitHub-origin requirements.
    """

    constraints = [
        (
            "core/preview_provider.py",
            "PublisherDesignatedPreviewProvider",
            "agent_url",
            "_require_https_agent_url",
            None,
        ),
        (
            "core/presentation_ref.py",
            "PlacementPresentationReference",
            "uri",
            "_require_https_uri",
            None,
        ),
        (
            "core/placement_presentation.py",
            "ImageRef",
            "uri",
            "_require_https_uri",
            None,
        ),
        (
            "core/reference_renderer.py",
            "Provenance",
            "source_repository",
            "_require_github_source_repository",
            "github.com",
        ),
    ]

    for relative_path, class_name, field_name, validator_name, required_host in constraints:
        target = OUTPUT_DIR / relative_path
        if not target.exists():
            print(f"  {relative_path} not found (skipping secure URL constraint)")
            continue

        source = target.read_text()
        if f"def {validator_name}(" in source:
            print(f"  {relative_path} secure URL constraint already fixed")
            continue

        source, import_count = re.subn(
            r"^(from pydantic import .+)$",
            r"\1, field_validator",
            source,
            count=1,
            flags=re.MULTILINE,
        )
        if import_count != 1:
            print(f"  {relative_path} pydantic import shape not found")
            continue

        class_start = source.find(f"class {class_name}(")
        if class_start == -1:
            print(f"  {relative_path} class {class_name} not found")
            continue
        class_end = source.find("\n\nclass ", class_start)
        if class_end == -1:
            class_end = len(source)

        if required_host is None:
            check = (
                "        if value.scheme != 'https':\n"
                f"            raise ValueError('{field_name} must use https')\n"
            )
        else:
            check = (
                "        if value.scheme != 'https' or value.host != "
                f"'{required_host}' or value.port != 443:\n"
                f"            raise ValueError('{field_name} must use "
                f"https://{required_host}/')\n"
                "        if value.username is not None or value.password is not None:\n"
                f"            raise ValueError('{field_name} must not contain credentials')\n"
            )

        validator = (
            f"\n\n    @field_validator('{field_name}')\n"
            "    @classmethod\n"
            f"    def {validator_name}(cls, value: AnyUrl) -> AnyUrl:\n"
            f"{check}"
            "        return value"
        )
        class_block = source[class_start:class_end].rstrip() + validator
        source = source[:class_start] + class_block + source[class_end:]
        target.write_text(source.rstrip() + "\n")
        print(f"  {relative_path}: restored secure URL constraint")


def fix_beta3_package_request_constraints() -> None:
    """Restore beta.3 PackageRequest conditionals dropped by codegen."""

    target = OUTPUT_DIR / "media_buy" / "package_request.py"
    if not target.exists():
        print("  media_buy/package_request.py not found (skipping beta.3 constraints)")
        return
    source = target.read_text()
    if "def _validate_format_params(" in source:
        print("  media_buy/package_request.py beta.3 constraints already fixed")
        return
    source, import_count = re.subn(
        r"^(from pydantic import .+)$",
        r"\1, model_validator",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if import_count != 1:
        print("  media_buy/package_request.py pydantic import shape not found")
        return
    class_start = source.find("class PackageRequest(")
    if class_start == -1:
        print("  media_buy/package_request.py PackageRequest class not found")
        return
    class_end = source.find("\n\nclass ", class_start)
    if class_end == -1:
        class_end = len(source)
    validator = """

    @model_validator(mode='after')
    def _validate_format_params(self) -> PackageRequest:
        if self.params is not None and self.format_kind is None:
            raise ValueError('params requires format_kind')
        if self.params is not None and self.format_kind == 'image':
            if ('width' in self.params) != ('height' in self.params):
                raise ValueError('image params width and height must co-occur')
        return self"""
    class_block = source[class_start:class_end].rstrip() + validator
    target.write_text((source[:class_start] + class_block + source[class_end:]).rstrip() + "\n")
    print("  media_buy/package_request.py: restored beta.3 format param constraints")


def fix_beta3_adagents_renderer_constraints() -> None:
    """Preserve optional catalog role and reference-renderer trust gates."""

    target = OUTPUT_DIR / "adagents.py"
    if not target.exists():
        print("  adagents.py not found (skipping beta.3 renderer constraints)")
        return
    source = target.read_text()

    source = source.replace(
        "        Literal['community_format_registry'],\n",
        "        Literal['community_format_registry'] | None,\n",
    )
    source = source.replace(
        "    ] = 'community_format_registry'\n",
        "    ] = None\n",
    )
    if "    reference_renderer,\n" not in source:
        source = source.replace(
            "    property_tag,\n",
            "    property_tag,\n    reference_renderer,\n",
            1,
        )

    if "def _validate_reference_renderer_catalog(" not in source:
        source, import_count = re.subn(
            r"^(from pydantic import .+)$",
            r"\1, model_validator",
            source,
            count=1,
            flags=re.MULTILINE,
        )
        if import_count != 1:
            print("  adagents.py pydantic import shape not found")
            return
        class_start = source.find(
            "class AdcpAgentsAuthorization(RootModel[AdcpAgentsAuthorization1 | AdcpAgentsAuthorization2]):"
        )
        if class_start == -1:
            print("  adagents.py outer authorization class not found")
            return
        validator = """

    @model_validator(mode='before')
    @classmethod
    def _validate_reference_renderer_catalog(cls, value: Any) -> Any:
        data = value.get('root', value) if isinstance(value, dict) else value
        if not isinstance(data, dict) or not isinstance(data.get('formats'), list):
            return value
        renderer_formats = [
            item
            for item in data['formats']
            if isinstance(item, dict) and item.get('reference_renderer') is not None
        ]
        if not renderer_formats:
            return value
        catalog_etag = data.get('catalog_etag')
        if not isinstance(catalog_etag, str) or not catalog_etag.strip():
            raise ValueError('reference_renderer requires a non-empty catalog_etag')
        if data.get('catalog_role') != 'community_format_registry':
            raise ValueError(
                "reference_renderer requires catalog_role='community_format_registry'"
            )
        for item in renderer_formats:
            renderer = item.get('reference_renderer')
            renderer_model = reference_renderer.ReferenceRenderer.model_validate(renderer)
            if item.get('format_revision') != renderer_model.format_revision:
                raise ValueError(
                    'format_revision must equal reference_renderer.format_revision'
                )
        return value"""
        source = source.rstrip() + validator + "\n"

    target.write_text(source)
    print("  adagents.py: restored beta.3 reference-renderer constraints")


def restore_trusted_match_compatibility_aliases() -> None:
    """Keep 3.1.8 Trusted Match imports available after the 3.1.10 rename.

    AdCP 3.1.10 replaced provider-supplied macro names with publisher-owned
    slot mappings.  The new wire types must be primary, but the SDK's public
    collision aliases still promise the old ``TmpxMacro`` helper classes and
    ``IdentityMatchResponse`` name.  Retain those helpers as compatibility
    models; they are intentionally not referenced by the 3.1.10 response
    fields.
    """

    identity_match_response = OUTPUT_DIR / "trusted_match" / "identity_match_response.py"
    if identity_match_response.exists():
        source = identity_match_response.read_text()
        changed = False
        if "class TmpxMacro(" not in source:
            compatibility_model = """
class TmpxMacro(AdCPBaseModel):
    \"\"\"Deprecated 3.1.8 TMPX macro/value compatibility model.\"\"\"

    model_config = ConfigDict(
        extra='forbid',
    )
    name: Annotated[
        str,
        Field(max_length=64, min_length=1, pattern='^[A-Z][A-Z0-9_]*$'),
    ]
    value: Annotated[str, Field(max_length=1024, min_length=1)]


"""
            source = source.replace(
                "class TmpxProviders(", compatibility_model + "class TmpxProviders(", 1
            )
            changed = True
        if (
            "class IdentityMatchResponseRouterPublisher(" in source
            and "IdentityMatchResponse = IdentityMatchResponseRouterPublisher" not in source
        ):
            source = (
                source.rstrip()
                + "\n\n\nIdentityMatchResponse = IdentityMatchResponseRouterPublisher\n"
            )
            changed = True
        if changed:
            identity_match_response.write_text(source)
            print("  trusted_match/identity_match_response.py: restored compatibility aliases")
        else:
            print(
                "  trusted_match/identity_match_response.py compatibility aliases already restored"
            )

    provider_registration = OUTPUT_DIR / "trusted_match" / "provider_registration.py"
    if provider_registration.exists():
        source = provider_registration.read_text()
        if "class TmpxMacro(" not in source:
            compatibility_model = """
class TmpxMacro(RootModel[str]):
    \"\"\"Deprecated 3.1.8 registered macro-name compatibility model.\"\"\"

    root: Annotated[str, Field(max_length=64, min_length=1, pattern='^[A-Z][A-Z0-9_]*$')]


"""
            source = source.replace("class TmpxSlot(", compatibility_model + "class TmpxSlot(", 1)
            provider_registration.write_text(source)
            print(
                "  trusted_match/provider_registration.py: restored TmpxMacro compatibility model"
            )
        else:
            print(
                "  trusted_match/provider_registration.py TmpxMacro compatibility model already restored"
            )


def fix_publisher_tmpx_mapping_key_constraints() -> None:
    """Repair datamodel-codegen's nested ``propertyNames`` key annotation.

    For the publisher TMPX configuration's map-of-maps, the generator emits
    the outer key as ``StringConstraints(...)`` instead of
    ``Annotated[str, StringConstraints(...)]``.  Pydantic then treats the
    constraint instance as a dataclass type and cannot build the model.
    """

    target = OUTPUT_DIR / "trusted_match" / "publisher_tmpx_config.py"
    if not target.exists():
        print("  trusted_match/publisher_tmpx_config.py not found (skipping)")
        return

    source = target.read_text()
    broken = (
        "            StringConstraints(pattern=r'^[A-Za-z0-9_]+$', min_length=1, max_length=64),\n"
    )
    fixed = (
        "            Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_]+$', "
        "min_length=1, max_length=64)],\n"
    )
    if broken in source:
        target.write_text(source.replace(broken, fixed, 1))
        print("  trusted_match/publisher_tmpx_config.py: fixed outer map key constraint")
    else:
        print("  trusted_match/publisher_tmpx_config.py outer map key already fixed")


def fix_wholesale_cache_scope_defaults() -> None:
    """Default beta 3 wholesale cache scope to public in typed responses."""
    targets = [
        (OUTPUT_DIR / "media_buy" / "get_products_response.py", "CacheScope.public"),
        (OUTPUT_DIR / "signals" / "get_signals_response.py", "CacheScope.public"),
        (OUTPUT_DIR / "bundled" / "media_buy" / "get_products_response.py", "CacheScope.public"),
        (OUTPUT_DIR / "bundled" / "signals" / "get_signals_response.py", "CacheScope.public"),
    ]

    total_fixed = 0
    for py_file, default in targets:
        if not py_file.exists():
            print(f"  {py_file.relative_to(OUTPUT_DIR)}: not found (skipping)")
            continue
        source = py_file.read_text()
        if re.search(
            r"cache_scope: Annotated\[.*?\n    \] = CacheScope\.public", source, re.DOTALL
        ):
            print(f"  {py_file.relative_to(OUTPUT_DIR)}: cache_scope default already fixed")
            continue
        new_source = re.sub(
            r"(    cache_scope: Annotated\[\n        CacheScope \| None," r".*?\n    \]) = None",
            rf"\1 = {default}",
            source,
            count=1,
            flags=re.DOTALL,
        )
        if new_source == source:
            print(f"  {py_file.relative_to(OUTPUT_DIR)}: cache_scope default not found")
            continue
        py_file.write_text(new_source)
        total_fixed += 1
        print(f"  {py_file.relative_to(OUTPUT_DIR)}: defaulted cache_scope to public")

    if total_fixed:
        print(f"  ✓ Defaulted cache_scope on {total_fixed} response model(s)")
    else:
        print("  No cache_scope defaults needed fixing")


def fix_product_publisher_property_model_coercion() -> None:
    """Allow public PublisherProperties aliases inside generated Product models.

    Beta 3 inlines product publisher-property variants separately from
    ``core/publisher-property-selector``. Their wire shapes are compatible,
    but Pydantic rejects instances of the selector classes as the inlined
    product classes. Coerce model instances back to dicts before validating.
    """
    target = OUTPUT_DIR / "core" / "product.py"
    if not target.exists():
        print("  core/product.py not found (skipping publisher property coercion)")
        return

    source = target.read_text()
    if "_coerce_publisher_property_models" in source:
        print("  core/product.py publisher property coercion already fixed")
        return

    if (
        "from pydantic import AnyUrl, AwareDatetime, ConfigDict, EmailStr, Field, RootModel"
        in source
    ):
        source = source.replace(
            "from pydantic import AnyUrl, AwareDatetime, ConfigDict, EmailStr, Field, RootModel",
            "from pydantic import AnyUrl, AwareDatetime, ConfigDict, EmailStr, Field, RootModel, model_validator",
        )
    else:
        print("  core/product.py pydantic import shape not found")
        return

    method = """

    @model_validator(mode='before')
    @classmethod
    def _coerce_publisher_property_models(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get('publisher_properties'), list):
            coerced = []
            changed = False
            for item in data['publisher_properties']:
                if hasattr(item, 'model_dump'):
                    coerced.append(item.model_dump(mode='json', exclude_none=True))
                    changed = True
                else:
                    coerced.append(item)
            if changed:
                data = dict(data)
                data['publisher_properties'] = coerced
        return data
"""

    fixed = 0
    for class_name in ("Product", "Product1", "Product2"):
        marker = f"class {class_name}(AdCPBaseModel):\n"
        idx = source.find(marker)
        if idx == -1:
            print(f"  core/product.py {class_name} not found")
            continue
        config_marker = "    model_config = ConfigDict(\n        extra='allow',\n    )\n"
        config_idx = source.find(config_marker, idx)
        if config_idx == -1:
            print(f"  core/product.py {class_name} model_config not found")
            continue
        insert_at = config_idx + len(config_marker)
        source = source[:insert_at] + method + source[insert_at:]
        fixed += 1

    if fixed:
        target.write_text(source)
        print(f"  core/product.py: added publisher property coercion to {fixed} Product class(es)")
    else:
        print("  No Product publisher property coercion added")


def fix_deprecated_rootmodel_fields() -> None:
    """Remove ``deprecated=True`` from generated RootModel ``root`` fields.

    Pydantic exposes deprecated fields through a descriptor. On RootModel
    classes that descriptor is installed for ``root`` itself, which prevents
    normal validation from assigning the wrapped value. The wrapper type can
    still be documented as deprecated through its description/title; the SDK
    must keep the model constructible.
    """
    fixed = 0

    for py_file in OUTPUT_DIR.rglob("*.py"):
        source = py_file.read_text()
        if "RootModel[" not in source or "deprecated=True" not in source:
            continue

        lines = source.splitlines(keepends=True)
        root_blocks: list[tuple[int, int]] = []
        for match in re.finditer(r"^class ([A-Za-z_]\w*)\b", source, re.MULTILINE):
            header_end = source.find(":\n", match.end())
            if header_end == -1:
                continue
            header = source[match.start() : header_end]
            if "RootModel[" not in header:
                continue
            block = _find_class_field_block(source, match.group(1), "root")
            if block is not None and "deprecated=True" in source[block[0] : block[1]]:
                root_blocks.append(block)

        if not root_blocks:
            continue

        line_starts: list[int] = []
        offset = 0
        for line in lines:
            line_starts.append(offset)
            offset += len(line)

        for start, end in root_blocks:
            for index, line_start in enumerate(line_starts):
                if line_start < start or line_start >= end:
                    continue
                if "deprecated=True" in lines[index]:
                    lines[index] = ""
                    fixed += 1

        py_file.write_text("".join(lines))

    if fixed:
        print(f"  Removed deprecated=True from {fixed} RootModel root field(s)")
    else:
        print("  No deprecated RootModel root fields needed fixing")


def fix_mcp_webhook_operation_id_optional() -> None:
    """Keep MCP webhook ``operation_id`` optional for SDK builders.

    Some registrations do not carry an operation id, and the public helper has
    always accepted ``operation_id=None``. The 3.1 schema tightened the field
    type; preserve SDK ergonomics by making the generated model optional.
    """
    target = OUTPUT_DIR / "core" / "mcp_webhook_payload.py"
    if not target.exists():
        print("  core/mcp_webhook_payload.py not found (skipping operation_id)")
        return
    source = target.read_text()
    if "operation_id: Annotated[\n        str | None," in source:
        print("  core/mcp_webhook_payload.py operation_id already optional")
        return
    new_source = source.replace(
        "operation_id: Annotated[\n        str,\n        Field(",
        "operation_id: Annotated[\n        str | None,\n        Field(",
        1,
    )
    new_source = new_source.replace(
        "    ]\n    task_id: Annotated[",
        "    ] = None\n    task_id: Annotated[",
        1,
    )
    if new_source == source:
        print("  core/mcp_webhook_payload.py operation_id rewrite failed")
        return
    target.write_text(new_source)
    print("  core/mcp_webhook_payload.py: made operation_id optional")


def fix_signal_listing_range_subclasses() -> None:
    """Reuse SignalListing.Range for generated subclasses that redeclare range."""
    replacements = {
        OUTPUT_DIR
        / "signals"
        / "get_signals_response.py": [
            (
                "from ..core.signal_listing import SignalListing\n",
                "from ..core.signal_listing import Range, SignalListing\n",
            ),
            (
                "\n\nclass Range(AdCPBaseModel):\n"
                "    model_config = ConfigDict(\n"
                "        extra='forbid',\n"
                "    )\n"
                "    min: Annotated[float, Field(description='Minimum value (inclusive)')]\n"
                "    max: Annotated[float, Field(description='Maximum value (inclusive)')]\n",
                "",
            ),
        ],
        OUTPUT_DIR
        / "core"
        / "wholesale_feed_event.py": [
            (
                "from .signal_listing import SignalListing\n",
                "from .signal_listing import Range, SignalListing\n",
            ),
            (
                "\n\nclass Range(AdCPBaseModel):\n"
                "    model_config = ConfigDict(\n"
                "        extra='forbid',\n"
                "    )\n"
                "    min: float\n"
                "    max: float\n",
                "",
            ),
        ],
    }

    fixed = 0
    for path, path_replacements in replacements.items():
        if not path.exists():
            continue
        source = path.read_text()
        updated = source
        for old, new in path_replacements:
            updated = updated.replace(old, new, 1)
        if updated != source:
            path.write_text(updated)
            fixed += 1

    if fixed:
        print(f"  Reused SignalListing.Range in {fixed} subclass file(s)")
    else:
        print("  No SignalListing.Range subclass fixes needed")


def restore_signal_catalog_type_alias() -> None:
    """Keep the old deep import path for the renamed signals availability enum."""
    target = OUTPUT_DIR / "enums" / "signal_catalog_type.py"
    if not target.exists():
        print("  signal_catalog_type.py not found (skipping SignalCatalogType alias)")
        return

    source = target.read_text()
    if "class SignalAvailabilityType" not in source:
        print("  SignalAvailabilityType not found (skipping SignalCatalogType alias)")
        return
    if "SignalCatalogType = SignalAvailabilityType" in source:
        print("  SignalCatalogType alias already restored")
        return

    target.write_text(source.rstrip() + "\n\n\nSignalCatalogType = SignalAvailabilityType\n")
    print("  Restored SignalCatalogType compatibility alias")


def restore_format_asset_numbered_aliases() -> None:
    """Restore stable numbered aliases removed by beta 3 renumbering.

    ``Assets94`` was the generated repeatable asset group class in earlier
    schema builds. Some legacy imports still target the generated module
    directly, so point that name at the current class whose discriminator
    default is ``item_type='repeatable_group'``.
    """
    target = OUTPUT_DIR / "core" / "format.py"
    if not target.exists():
        print("  core/format.py not found (skipping Assets94 alias)")
        return

    source = target.read_text()
    if "\nAssets94 = " in source:
        print("  core/format.py Assets94 alias already restored")
        return

    tree = ast.parse(source)
    repeatable_class: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if not isinstance(stmt.target, ast.Name) or stmt.target.id != "item_type":
                continue
            # Match the discriminator either by its already-injected default
            # value or by its ``Literal['repeatable_group']`` annotation. The
            # default is added by ``inject_literal_discriminator_defaults``,
            # which runs after this fix in the registry — so when the codegen
            # emits the discriminator without a default (rc.10 shape), only the
            # annotation is available here.
            has_const_default = (
                isinstance(stmt.value, ast.Constant) and stmt.value.value == "repeatable_group"
            )
            has_literal_annotation = (
                _extract_single_literal_value(stmt.annotation) == "repeatable_group"
            )
            if has_const_default or has_literal_annotation:
                repeatable_class = node.name
                break
        if repeatable_class is not None:
            break

    if repeatable_class is None:
        print("  WARN: repeatable asset group class not found; Assets94 alias not restored")
        return

    target.write_text(
        source.rstrip()
        + "\n\n\n# Backward compatibility for the pre-beta3 generated repeatable group name.\n"
        + f"Assets94 = {repeatable_class}\n"
    )
    print(f"  core/format.py: restored Assets94 -> {repeatable_class}")


def restore_principal_result_aliases() -> None:
    """Expose principal result arms by discriminator instead of numeric suffix.

    datamodel-code-generator numbers anonymous ``result`` variants according
    to aggregate schema traversal order.  Those numbers are implementation
    details and can differ across generator versions or filesystem order.
    """
    specs = {
        "protocol/get_principal_response.py": {
            "PrincipalUnconfiguredResult": "unconfigured",
            "PrincipalCurrentResult": "current",
            "PrincipalRecognizedResult": "recognized",
            "PrincipalReadFailedResult": "failed",
        },
        "protocol/sync_principal_response.py": {
            "PrincipalValidatedResult": "validated",
            "PrincipalAppliedResult": "applied",
            "PrincipalSyncFailedResult": "failed",
        },
    }

    for relative_path, aliases in specs.items():
        target = OUTPUT_DIR / relative_path
        if not target.exists():
            print(f"  {relative_path} not found (skipping principal result aliases)")
            continue

        source = target.read_text()
        tree = ast.parse(source)
        classes_by_kind: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "kind"
                ):
                    kind = _extract_single_literal_value(stmt.annotation)
                    if isinstance(kind, str):
                        classes_by_kind[kind] = node.name
                    break

        missing = sorted(set(aliases.values()) - classes_by_kind.keys())
        if missing:
            raise RuntimeError(f"{relative_path}: principal result kinds not generated: {missing}")

        assignments = [f"{alias} = {classes_by_kind[kind]}" for alias, kind in aliases.items()]
        marker = assignments[0]
        if marker in source:
            print(f"  {relative_path}: principal result aliases already restored")
            continue

        target.write_text(
            source.rstrip()
            + "\n\n\n# Stable aliases for anonymous result arms (selected by discriminator).\n"
            + "\n".join(assignments)
            + "\n"
        )
        print(f"  {relative_path}: restored {len(assignments)} principal result aliases")


def disambiguate_comply_response_arm() -> None:
    """Give the comply response's anonymous ``Arm`` enum a stable name.

    The request schema already generates an unrelated public ``Arm`` enum.
    Codegen 0.63 also calls the response enum ``Arm``, which trips the exact
    collision guard only after regeneration.  Rename the response-local type
    before exports are consolidated so both the release tree and a fresh tree
    have an unambiguous namespace.
    """
    target = OUTPUT_DIR / "compliance" / "comply_test_controller_response.py"
    if not target.exists():
        print("  comply_test_controller_response.py not found (skipping Arm rename)")
        return

    source = target.read_text()
    if "class ComplyResponseArm(" in source:
        print("  compliance response Arm already disambiguated")
        return
    if "class Arm(" not in source:
        print("  compliance response Arm not generated (no rename needed)")
        return

    target.write_text(re.sub(r"\bArm\b", "ComplyResponseArm", source))
    print("  compliance response: renamed Arm -> ComplyResponseArm")


def restore_flattened_contract_field_types() -> None:
    """Restore constraints lost when codegen re-states ``allOf`` fields as ``Any``.

    datamodel-code-generator 0.64 flattens a required field inherited through an
    ``allOf`` and, for these two schemas, emits an untyped local override.  That
    broadens the public model beyond the schema: signal references stop being
    discriminated and creative representations accept arbitrary format kinds.
    Keep the correction here so a clean regeneration retains the wire contract.
    """
    product_target = OUTPUT_DIR / "core" / "product_signal_targeting_option.py"
    if product_target.exists():
        source = product_target.read_text()
        expected = "    signal_ref: Any"
        replacement = """    signal_ref: Annotated[
        signal_ref.SignalRef,
        Field(
            description="Canonical signal reference. Use scope 'product' for a product-local signal defined by this listing; use scope 'data_provider' with data_provider_domain for a signal defined in a data provider's published adagents.json signals[]; use scope 'signal_source' with signal_source_url for a source-native signal."
        ),
    ]"""
        if expected in source:
            vendor_import = "from . import vendor_pricing_option\n"
            if vendor_import not in source:
                raise RuntimeError("product_signal_targeting_option.py: missing vendor import")
            source = source.replace(
                vendor_import, "from . import signal_ref, vendor_pricing_option\n", 1
            )
            source = source.replace(expected, replacement, 1)
            # ``Any`` was imported only for the codegen-erased field.
            source = source.replace(
                "from typing import Annotated, Any\n", "from typing import Annotated\n"
            )
            product_target.write_text(source)
            print("  core/product_signal_targeting_option.py: restored SignalRef discriminator")
        elif replacement in source:
            print(
                "  core/product_signal_targeting_option.py: SignalRef discriminator already restored"
            )
        else:
            raise RuntimeError(
                "product_signal_targeting_option.py: expected signal_ref override not found"
            )
    else:
        print(
            "  core/product_signal_targeting_option.py not found (skipping SignalRef restoration)"
        )

    representation_target = OUTPUT_DIR / "core" / "creative_representation.py"
    if not representation_target.exists():
        print("  core/creative_representation.py not found (skipping representation restoration)")
        return

    source = representation_target.read_text()
    expected = "    format_kind: Any"
    replacement = """    format_kind: Annotated[
        CanonicalFormatKind,
        Field(
            description="Canonical 3.2 path. The canonical format name this manifest targets (e.g., `image`, `video_hosted`, `audio_vast`, `seller_rendered_stateful_display`, `coordinated_placements`). Selects the contract against which the seller validates the manifest's assets. Mutually exclusive with deprecated `format_id`."
        ),
    ]"""
    if expected in source:
        if "from .canonical_format_kind import CanonicalFormatKind\n" not in source:
            anchor = "from .creative_manifest import CreativeManifest\n"
            if anchor not in source:
                raise RuntimeError("creative_representation.py: missing CreativeManifest import")
            source = source.replace(
                anchor, "from .canonical_format_kind import CanonicalFormatKind\n" + anchor, 1
            )
        source = source.replace(expected, replacement, 1)
    elif replacement not in source:
        raise RuntimeError("creative_representation.py: expected format_kind override not found")

    generated_config = """class CreativeRepresentation(CreativeManifest):
    model_config = ConfigDict(
        extra='allow',
    )
"""
    contract_config = """class CreativeRepresentation(CreativeManifest):
    model_config = ConfigDict(
        extra='allow',
        json_schema_extra={
            'not': {
                'anyOf': [
                    {'required': ['format_id']},
                    {'required': ['format_option_ref']},
                    {'required': ['representation_selection']},
                ]
            }
        },
    )
"""
    if generated_config in source:
        source = source.replace(generated_config, contract_config, 1)
    elif "json_schema_extra=" not in source:
        raise RuntimeError("creative_representation.py: expected model configuration not found")

    if "@model_validator(mode='before')" not in source:
        if "from pydantic import ConfigDict, Field\n" not in source:
            raise RuntimeError("creative_representation.py: missing Pydantic import")
        source = source.replace(
            "from pydantic import ConfigDict, Field\n",
            "from pydantic import ConfigDict, Field, model_validator\n",
            1,
        )
        source = (
            source.rstrip()
            + """

    @model_validator(mode='before')
    @classmethod
    def _reject_seller_bound_manifest_fields(cls, data: Any) -> Any:
        \"\"\"Representations cannot carry seller-side manifest selectors.\"\"\"
        if isinstance(data, dict):
            forbidden = ('format_id', 'format_option_ref', 'representation_selection')
            present = [field for field in forbidden if field in data]
            if present:
                raise ValueError(
                    'creative representations must not include ' + ', '.join(present)
                )
        return data
"""
        )
    representation_target.write_text(source)
    print("  core/creative_representation.py: restored canonical format contract")


def enforce_transformer_output_contract() -> None:
    """Require a transformer to declare canonical or legacy output formats."""
    target = OUTPUT_DIR / "core" / "transformer.py"
    if not target.exists():
        print("  core/transformer.py not found (skipping transformer output contract)")
        return

    source = target.read_text()
    if "def _require_output_format_declaration" in source:
        old_condition = (
            "        if self.output_capability_ids is None and self.output_format_ids is None:\n"
        )
        new_condition = """        # Read Pydantic's stored values directly so validation itself does not
        # emit a deprecation warning for the still-supported legacy field.
        if (
            self.__dict__.get('output_capability_ids') is None
            and self.__dict__.get('output_format_ids') is None
        ):
"""
        if old_condition in source:
            target.write_text(source.replace(old_condition, new_condition, 1))
            print("  core/transformer.py: updated output contract deprecation handling")
            return
        print("  core/transformer.py: output contract already enforced")
        return
    if "class Transformer(" not in source:
        raise RuntimeError("transformer.py: Transformer class not found")
    if "from pydantic import AnyUrl, ConfigDict, Field, RootModel\n" not in source:
        raise RuntimeError("transformer.py: missing Pydantic import")

    source = source.replace(
        "from pydantic import AnyUrl, ConfigDict, Field, RootModel\n",
        "from pydantic import AnyUrl, ConfigDict, Field, RootModel, model_validator\n",
        1,
    )
    target.write_text(
        source.rstrip()
        + """

    @model_validator(mode='after')
    def _require_output_format_declaration(self) -> Transformer:
        \"\"\"At least one output declaration is required by the schema.\"\"\"
        # Read Pydantic's stored values directly so validation itself does not
        # emit a deprecation warning for the still-supported legacy field.
        if (
            self.__dict__.get('output_capability_ids') is None
            and self.__dict__.get('output_format_ids') is None
        ):
            raise ValueError(
                'one of output_capability_ids or deprecated output_format_ids is required'
            )
        return self
"""
    )
    print("  core/transformer.py: enforced output declaration requirement")


def restore_constructible_response_bases() -> None:
    """Keep selected public response names as constructible Pydantic models.

    The generated numbered arms are still the schema-specific parsing surface and
    remain available through the existing aliases.  A top-level union alias,
    however, is not constructible and breaks callers that used the stable response
    model API. Restore the former envelope base and make every generated arm a
    subclass of it. The shared mixin dispatches ``Base.model_validate`` through
    the arms, preserving both forms without losing task-specific wire fields.
    """
    response_specs = (
        ("compliance/comply_test_controller_response.py", "ComplyTestControllerResponse"),
        (
            "content_standards/create_content_standards_response.py",
            "CreateContentStandardsResponse",
        ),
        ("content_standards/list_content_standards_response.py", "ListContentStandardsResponse"),
        ("account/sync_governance_response.py", "SyncGovernanceResponse"),
        (
            "content_standards/update_content_standards_response.py",
            "UpdateContentStandardsResponse",
        ),
    )

    for relative_path, base_name in response_specs:
        target = OUTPUT_DIR / relative_path
        if not target.exists():
            print(f"  {relative_path} not found (skipping constructible response base)")
            continue

        source = target.read_text()
        if _RESPONSE_ARM_DISPATCH_IMPORT not in source:
            future_import = "from __future__ import annotations\n\n"
            if future_import not in source:
                raise RuntimeError(f"{relative_path}: missing future annotations import")
            source = source.replace(
                future_import, future_import + _RESPONSE_ARM_DISPATCH_IMPORT + "\n", 1
            )
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise RuntimeError(f"{relative_path}: invalid generated Python") from exc

        arms = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and re.fullmatch(rf"{re.escape(base_name)}\d+", node.name)
        ]
        if not arms:
            print(f"  {relative_path}: response arms not generated (skipping constructible base)")
            continue

        stable_base_exists = any(
            isinstance(node, ast.ClassDef) and node.name == base_name for node in tree.body
        )
        lines = source.splitlines(keepends=True)
        for node in tree.body:
            target_names: list[str] = []
            if isinstance(node, ast.Assign):
                target_names = [item.id for item in node.targets if isinstance(item, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_names = [node.target.id]
            if base_name in target_names:
                end_line = node.end_lineno or node.lineno
                for line_number in range(node.lineno - 1, end_line):
                    lines[line_number] = ""
        source = "".join(lines)

        arm_names = [
            arm.name for arm in sorted(arms, key=lambda arm: int(arm.name.removeprefix(base_name)))
        ]
        for arm in arms:
            arm_header = re.compile(rf"^class {re.escape(arm.name)}\([^\n]*\):$", re.MULTILINE)
            source, replacements = arm_header.subn(
                f"class {arm.name}({base_name}):", source, count=1
            )
            if replacements != 1:
                raise RuntimeError(f"{relative_path}: unable to rewrite {arm.name} base")

        first_arm = min(arms, key=lambda arm: arm.lineno)
        arm_marker = f"class {first_arm.name}("
        first_arm_position = source.find(arm_marker)
        if first_arm_position < 0:
            raise RuntimeError(f"{relative_path}: unable to locate {first_arm.name}")
        arm_list = ",\n            ".join(arm_names)
        compatibility_base = f"""class {base_name}(ResponseArmDispatchMixin, AdcpVersionEnvelope, ProtocolEnvelope):
    \"\"\"Constructible compatibility base for generated response arms.\"\"\"

    @classmethod
    def _response_arm_models(cls) -> tuple[type[{base_name}], ...]:
        return (
            {arm_list},
        )


"""
        if not stable_base_exists:
            source = source[:first_arm_position] + compatibility_base + source[first_arm_position:]
        else:
            base_marker = f"class {base_name}("
            base_position = source.find(base_marker)
            if base_position < 0:
                raise RuntimeError(f"{relative_path}: unable to update {base_name} base")
            source = source[:base_position] + compatibility_base + source[first_arm_position:]

        target.write_text(source.rstrip() + "\n")
        print(f"  {relative_path}: restored constructible {base_name} base")


def restore_response_variant_aliases() -> None:
    """Restore numbered response arms from schema data, not hand-written payloads.

    datamodel-code-generator currently collapses several task responses to a
    common envelope class with no task-specific fields. The SDK still exposes
    numbered response-arm classes for ergonomic construction/parsing aliases.
    Keep those public names, but derive their fields from the JSON schemas so
    the compatibility layer cannot silently drift from protocol shape.
    """

    response_specs: tuple[tuple[str, str], ...] = (
        ("account/get_account_financials_response.py", "GetAccountFinancialsResponse"),
        ("account/sync_accounts_response.py", "SyncAccountsResponse"),
        ("brand/acquire_rights_response.py", "AcquireRightsResponse"),
        ("brand/get_brand_identity_response.py", "GetBrandIdentityResponse"),
        ("brand/get_rights_response.py", "GetRightsResponse"),
        ("brand/update_rights_response.py", "UpdateRightsResponse"),
        ("content_standards/calibrate_content_response.py", "CalibrateContentResponse"),
        ("content_standards/get_content_standards_response.py", "GetContentStandardsResponse"),
        ("content_standards/get_media_buy_artifacts_response.py", "GetMediaBuyArtifactsResponse"),
        (
            "content_standards/validate_content_delivery_response.py",
            "ValidateContentDeliveryResponse",
        ),
        ("creative/get_creative_features_response.py", "GetCreativeFeaturesResponse"),
        ("creative/preview_creative_response.py", "PreviewCreativeResponse"),
        ("creative/sync_creatives_response.py", "SyncCreativesResponse"),
        ("media_buy/build_creative_response.py", "BuildCreativeResponse"),
        ("media_buy/create_media_buy_response.py", "CreateMediaBuyResponse"),
        ("media_buy/log_event_response.py", "LogEventResponse"),
        (
            "media_buy/provide_performance_feedback_response.py",
            "ProvidePerformanceFeedbackResponse",
        ),
        ("media_buy/sync_audiences_response.py", "SyncAudiencesResponse"),
        ("media_buy/sync_catalogs_response.py", "SyncCatalogsResponse"),
        ("media_buy/sync_event_sources_response.py", "SyncEventSourcesResponse"),
        ("media_buy/update_media_buy_response.py", "UpdateMediaBuyResponse"),
        ("signals/activate_signal_response.py", "ActivateSignalResponse"),
    )

    fixed = 0

    def _schema_relative(relative: str) -> Path:
        rel = Path(relative).with_suffix(".json")
        return Path(*(part.replace("_", "-") for part in rel.parts))

    def _pascal(value: str) -> str:
        words = re.split(r"[^A-Za-z0-9]+", value)
        return "".join(word[:1].upper() + word[1:] for word in words if word)

    def _singular_pascal(value: str) -> str:
        if value.endswith("ies"):
            value = value[:-3] + "y"
        elif value.endswith("ses"):
            value = value[:-2]
        elif value.endswith("s") and not value.endswith("ss"):
            value = value[:-1]
        return _pascal(value)

    def _load_schema(schema_rel: Path) -> dict[str, Any]:
        return json.loads((SCHEMA_DIR / schema_rel).read_text())

    def _schema_title(schema_rel: Path) -> str:
        schema = _load_schema(schema_rel)
        title = schema.get("title")
        if isinstance(title, str) and title.strip():
            return title
        return schema_rel.stem

    def _generated_module_path(schema_rel: Path) -> Path:
        parts = [part.replace("-", "_") for part in schema_rel.with_suffix(".py").parts]
        return OUTPUT_DIR / Path(*parts)

    def _generated_class_name(schema_rel: Path) -> str:
        fallback = _pascal(_schema_title(schema_rel))
        module_path = _generated_module_path(schema_rel)
        if not module_path.exists():
            return fallback
        try:
            tree = ast.parse(module_path.read_text())
        except SyntaxError:
            return fallback
        class_names = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        if not class_names:
            return fallback
        normalized_fallback = fallback.lower()
        for name in class_names:
            if name.lower() == normalized_fallback:
                return name
        stem_fallback = _pascal(schema_rel.stem).lower()
        for name in class_names:
            if name.lower() == stem_fallback:
                return name
        return class_names[-1]

    def _safe_import_alias(module_stem: str, used: set[str]) -> str:
        base = module_stem.replace("-", "_")
        alias = f"{base}_1"
        index = 1
        while alias in used:
            index += 1
            alias = f"{base}_{index}"
        used.add(alias)
        return alias

    def _json_pointer_get(schema: dict[str, Any], pointer: str) -> Any:
        if not pointer.startswith("#/"):
            raise KeyError(pointer)
        node: Any = schema
        for raw_part in pointer[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                node = node[int(part)]
            elif isinstance(node, dict):
                node = node[part]
            else:
                raise KeyError(pointer)
        return node

    def _merge_all_of(parts: list[Any]) -> dict[str, Any] | None:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in parts:
            if not isinstance(part, dict):
                return None
            if "$ref" in part or "oneOf" in part or "anyOf" in part:
                return None
            if part.get("type") not in {None, "object"} and "properties" not in part:
                return None
            merged["properties"].update(deepcopy(part.get("properties") or {}))
            merged["required"].extend(part.get("required") or [])
            if "additionalProperties" in part:
                merged["additionalProperties"] = part["additionalProperties"]
        merged["required"] = sorted(set(merged["required"]))
        return merged

    class Emitter:
        def __init__(self, relative: str, base: str, schema_rel: Path):
            self.relative = relative
            self.base = base
            self.schema_rel = schema_rel
            self.imports: dict[str, set[str]] = {}
            self.pydantic_imports: set[str] = {"ConfigDict"}
            self.typing_imports: set[str] = {"TypeAlias"}
            self.used_aliases: set[str] = set()
            self.import_aliases: dict[str, str] = {}
            self.nested: list[str] = []
            self.nested_names: set[str] = set()
            self.local_ref_types: dict[str, str] = {}
            self.root_schema: dict[str, Any] = {}
            self.needs_protocol_envelope = False
            self.needs_media_buy_helpers = False
            self.needs_sequence = False
            self.datetime_imports: set[str] = set()

        def add_import(self, line: str) -> None:
            self.imports.setdefault(line, set())

        def import_alias(self, import_line: str, module_stem: str) -> str:
            if import_line in self.import_aliases:
                return self.import_aliases[import_line]
            alias = _safe_import_alias(module_stem, self.used_aliases)
            self.import_aliases[import_line] = alias
            self.add_import(import_line.format(alias=alias))
            return alias

        def ref_type(self, schema: dict[str, Any]) -> str:
            ref = schema.get("$ref")
            if not isinstance(ref, str):
                return "Any"
            if ref.startswith("#"):
                if ref in self.local_ref_types:
                    return self.local_ref_types[ref]
                try:
                    ref_schema = _json_pointer_get(self.root_schema, ref)
                except (KeyError, IndexError, ValueError, TypeError):
                    self.typing_imports.add("Any")
                    return "Any"
                name = _pascal(ref.rsplit("/", 1)[-1])
                typ = self.type_for(name, ref_schema if isinstance(ref_schema, dict) else {})
                self.local_ref_types[ref] = typ
                return typ
            ref_rel = _resolve_schema_ref(self.schema_rel, ref)
            parts = list(ref_rel.parts)
            module_stem = ref_rel.stem.replace("-", "_")
            class_name = _generated_class_name(ref_rel)
            if parts[0] == "enums":
                alias = self.import_alias(
                    f"from ..enums import {module_stem} as {{alias}}", module_stem
                )
                return f"{alias}.{class_name}"
            if parts[0] == "core":
                alias = self.import_alias(
                    f"from ..core import {module_stem} as {{alias}}", module_stem
                )
                return f"{alias}.{class_name}"
            if parts[0] == Path(self.relative).parts[0].replace("_", "-"):
                alias = self.import_alias(f"from . import {module_stem} as {{alias}}", module_stem)
                return f"{alias}.{class_name}"
            module_dir = parts[0].replace("-", "_")
            alias = self.import_alias(
                f"from ..{module_dir} import {module_stem} as {{alias}}", module_stem
            )
            return f"{alias}.{class_name}"

        def string_type(self, schema: dict[str, Any]) -> str:
            constraints: list[str] = []
            if isinstance(schema.get("pattern"), str):
                constraints.append(f"pattern={schema['pattern']!r}")
            if isinstance(schema.get("minLength"), int):
                constraints.append(f"min_length={schema['minLength']}")
            if isinstance(schema.get("maxLength"), int):
                constraints.append(f"max_length={schema['maxLength']}")
            if constraints:
                self.typing_imports.add("Annotated")
                self.pydantic_imports.add("StringConstraints")
                return f"Annotated[str, StringConstraints({', '.join(constraints)})]"
            if schema.get("format") == "uri":
                self.pydantic_imports.add("AnyUrl")
                return "AnyUrl"
            if schema.get("format") == "date-time":
                self.pydantic_imports.add("AwareDatetime")
                return "AwareDatetime"
            if schema.get("format") == "date":
                self.datetime_imports.add("date")
                return "date"
            return "str"

        def constrained_number_type(self, base: str, schema: dict[str, Any]) -> str:
            constraints: list[str] = []
            if "minimum" in schema:
                constraints.append(f"ge={schema['minimum']!r}")
            if "maximum" in schema:
                constraints.append(f"le={schema['maximum']!r}")
            if "exclusiveMinimum" in schema:
                constraints.append(f"gt={schema['exclusiveMinimum']!r}")
            if "exclusiveMaximum" in schema:
                constraints.append(f"lt={schema['exclusiveMaximum']!r}")
            if not constraints:
                return base
            self.typing_imports.add("Annotated")
            self.pydantic_imports.add("Field")
            return f"Annotated[{base}, Field({', '.join(constraints)})]"

        def maybe_constrain_array(self, typ: str, schema: dict[str, Any]) -> str:
            constraints: list[str] = []
            if isinstance(schema.get("minItems"), int):
                constraints.append(f"min_length={schema['minItems']}")
            if isinstance(schema.get("maxItems"), int):
                constraints.append(f"max_length={schema['maxItems']}")
            if not constraints:
                return typ
            self.typing_imports.add("Annotated")
            self.pydantic_imports.add("Field")
            return f"Annotated[{typ}, Field({', '.join(constraints)})]"

        def type_for(self, name: str, schema: dict[str, Any]) -> str:
            if "$ref" in schema:
                return self.ref_type(schema)
            if "const" in schema:
                self.typing_imports.add("Literal")
                return f"Literal[{schema['const']!r}]"
            enum = schema.get("enum")
            if isinstance(enum, list) and enum:
                self.typing_imports.add("Literal")
                values = ", ".join(repr(v) for v in enum)
                return f"Literal[{values}]"
            if "oneOf" in schema or "anyOf" in schema:
                variants = schema.get("oneOf") or schema.get("anyOf") or []
                types = []
                for index, variant in enumerate(variants, 1):
                    if isinstance(variant, dict):
                        if variant.get("type") == "null":
                            types.append("None")
                        else:
                            types.append(self.type_for(f"{name}{index}", variant))
                unique_types = list(dict.fromkeys(types))
                if unique_types:
                    return " | ".join(unique_types)
                self.typing_imports.add("Any")
                return "Any"
            if "allOf" in schema:
                merged = _merge_all_of(schema.get("allOf") or [])
                if merged is not None:
                    return self.emit_nested(_pascal(name), merged)
                self.typing_imports.add("Any")
                return "Any"
            schema_type = schema.get("type")
            if isinstance(schema_type, list):
                non_null = [item for item in schema_type if item != "null"]
                if len(non_null) == 1:
                    schema_type = non_null[0]
                else:
                    self.typing_imports.add("Any")
                    return "Any"
            if schema_type == "string":
                return self.string_type(schema)
            if schema_type == "integer":
                return self.constrained_number_type("int", schema)
            if schema_type == "number":
                return self.constrained_number_type("float", schema)
            if schema_type == "boolean":
                return "bool"
            if schema_type == "array":
                item_schema = schema.get("items")
                if isinstance(item_schema, dict):
                    if item_schema.get("type") == "object" and isinstance(
                        item_schema.get("properties"), dict
                    ):
                        class_name = self.emit_nested(_singular_pascal(name), item_schema)
                        return self.maybe_constrain_array(f"list[{class_name}]", schema)
                    return self.maybe_constrain_array(
                        f"list[{self.type_for(_singular_pascal(name), item_schema)}]",
                        schema,
                    )
                self.typing_imports.add("Any")
                return self.maybe_constrain_array("list[Any]", schema)
            if (
                schema_type == "object"
                or "properties" in schema
                or "additionalProperties" in schema
            ):
                if isinstance(schema.get("properties"), dict) and schema["properties"]:
                    return self.emit_nested(_pascal(name), schema)
                pattern_props = schema.get("patternProperties")
                if isinstance(pattern_props, dict) and pattern_props:
                    pattern, value_schema = next(iter(pattern_props.items()))
                    key_type = "str"
                    if pattern:
                        self.typing_imports.add("Annotated")
                        self.pydantic_imports.add("StringConstraints")
                        key_type = f"Annotated[str, StringConstraints(pattern={pattern!r})]"
                    value_type = "Any"
                    if isinstance(value_schema, dict):
                        value_type = self.type_for(f"{name}_value", value_schema)
                    return f"dict[{key_type}, {value_type}]"
                additional = schema.get("additionalProperties")
                if isinstance(additional, dict):
                    return f"dict[str, {self.type_for(f'{name}_value', additional)}]"
                self.typing_imports.add("Any")
                return "dict[str, Any]"
            self.typing_imports.add("Any")
            return "Any"

        def emit_nested(self, preferred: str, schema: dict[str, Any]) -> str:
            class_name = preferred
            suffix = 1
            while class_name in self.nested_names:
                suffix += 1
                class_name = f"{preferred}{suffix}"
            self.nested_names.add(class_name)
            required = set(schema.get("required") or [])
            lines = [
                f"class {class_name}(AdcpVersionEnvelope):",
                "    model_config = ConfigDict(extra='allow')",
            ]
            props = schema.get("properties") or {}
            if not props:
                lines.append("    pass")
            for prop_name, prop_schema in props.items():
                if not isinstance(prop_schema, dict):
                    self.typing_imports.add("Any")
                    typ = "Any"
                else:
                    typ = self.type_for(prop_name, prop_schema)
                if prop_name in required:
                    lines.append(f"    {prop_name}: {typ}")
                else:
                    lines.append(f"    {prop_name}: {typ} | None = None")
            self.nested.append("\n".join(lines))
            return class_name

        def emit_response_class(self, class_name: str, arm: dict[str, Any]) -> str:
            props = arm.get("properties") or {}
            required = set(arm.get("required") or [])
            is_submitted = (
                props.get("status", {}).get("const") == "submitted" and "task_id" in props
            )
            bases = (
                "AdcpVersionEnvelope, ProtocolEnvelope" if is_submitted else "AdcpVersionEnvelope"
            )
            if is_submitted:
                self.needs_protocol_envelope = True
            lines = [f"class {class_name}({bases}):"]
            if is_submitted:
                lines.append("    model_config = ConfigDict(extra='allow', validate_default=True)")
            else:
                lines.append("    model_config = ConfigDict(extra='allow')")
            is_sync_media_buy_success = self.base in {
                "CreateMediaBuyResponse",
                "UpdateMediaBuyResponse",
            } and class_name.endswith("1")
            if is_sync_media_buy_success and "status" not in props:
                # AdCP 3.2 omits the 3.1 synchronous task-envelope status from
                # this arm. The SDK still declares it for negotiated 3.0/3.1
                # compatibility and so the legacy normalizer never injects an
                # unknown extra into ``extra='forbid'`` subclasses.
                self.typing_imports.add("Literal")
                lines.append("    status: Literal['completed'] = 'completed'")
            for prop_name, prop_schema in props.items():
                if is_submitted and prop_name == "status":
                    alias = self.import_alias(
                        "from ..enums import task_status as {alias}", "task_status"
                    )
                    self.typing_imports.add("Literal")
                    lines.append(
                        f"    status: Literal[{alias}.TaskStatus.submitted] = "
                        f"{alias}.TaskStatus.submitted"
                    )
                    continue
                if (
                    self.base in {"CreateMediaBuyResponse", "UpdateMediaBuyResponse"}
                    and class_name.endswith("1")
                    and prop_name == "status"
                ):
                    self.typing_imports.add("Literal")
                    lines.append("    status: Literal['completed'] = 'completed'")
                    continue
                if not isinstance(prop_schema, dict):
                    self.typing_imports.add("Any")
                    typ = "Any"
                else:
                    typ = self.type_for(prop_name, prop_schema)
                if (
                    self.base == "UpdateMediaBuyResponse"
                    and class_name.endswith("1")
                    and prop_name == "affected_packages"
                    and typ.startswith("list[")
                ):
                    self.needs_sequence = True
                    typ = f"Sequence[{typ.removeprefix('list[')}"
                if prop_name in required:
                    const = prop_schema.get("const")
                    if isinstance(const, str):
                        lines.append(f"    {prop_name}: {typ} = {const!r}")
                    else:
                        lines.append(f"    {prop_name}: {typ}")
                else:
                    lines.append(f"    {prop_name}: {typ} | None = None")
            if self.base in {
                "CreateMediaBuyResponse",
                "UpdateMediaBuyResponse",
            } and class_name.endswith("1"):
                self.needs_media_buy_helpers = True
                lines.append("")
                lines.append("    @model_validator(mode='before')")
                lines.append("    @classmethod")
                lines.append("    def _normalize_legacy_status(cls, data: Any) -> Any:")
                lines.append("        if not isinstance(data, dict):")
                lines.append("            return data")
                lines.append("        raw_status = unwrap_enum_value(data.get('status'))")
                lines.append(
                    "        media_buy_status = unwrap_enum_value(data.get('media_buy_status'))"
                )
                lines.append("        if raw_status is None:")
                lines.append("            data = dict(data)")
                lines.append("            data['status'] = 'completed'")
                lines.append("        elif raw_status == 'completed':")
                lines.append("            data = dict(data)")
                lines.append("            data['status'] = 'completed'")
                lines.append(
                    "        elif media_buy_status is None and raw_status in MEDIA_BUY_LEGACY_STATUS_VALUES:"
                )
                lines.append("            data = dict(data)")
                lines.append("            data['media_buy_status'] = raw_status")
                lines.append("            data['status'] = 'completed'")
                lines.append(
                    "        elif media_buy_status is not None and raw_status == media_buy_status:"
                )
                lines.append("            data = dict(data)")
                lines.append("            data['status'] = 'completed'")
                lines.append("        return data")
            return "\n".join(lines)

        def render(self, schema: dict[str, Any]) -> str:
            self.root_schema = schema
            arms = schema.get("oneOf") or schema.get("anyOf") or []
            if not arms:
                arms = [schema]
            if self.base == "BuildCreativeResponse" and len(arms) == 6:
                # Preserve the long-standing public numbering where
                # Response1 is the simple success arm and Response2 is the
                # error arm; append newer schema branches after those.
                arms = [arms[index] for index in (0, 4, 1, 2, 3, 5)]
            class_names = [f"{self.base}{index}" for index in range(1, len(arms) + 1)]
            response_classes = [
                self.emit_response_class(name, arm) for name, arm in zip(class_names, arms)
            ]
            if self.needs_protocol_envelope:
                self.add_import("from ..core.protocol_envelope import ProtocolEnvelope")
            if self.needs_media_buy_helpers:
                self.add_import(
                    "from adcp.types.media_buy_status_helpers import "
                    "MEDIA_BUY_LEGACY_STATUS_VALUES, unwrap_enum_value"
                )
                self.pydantic_imports.add("model_validator")
                self.typing_imports.add("Any")
            if any(re.search(r"\bAny\b", block) for block in self.nested + response_classes):
                self.typing_imports.add("Any")
            header = [
                "# generated by datamodel-codegen:",
                f"#   filename:  {self.relative.replace('.py', '.json')}",
                "#   timestamp:  preserved-by-post-generate-fixes",
                "",
                "from __future__ import annotations",
                "",
            ]
            if self.datetime_imports:
                header.extend(
                    [f"from datetime import {', '.join(sorted(self.datetime_imports))}", ""]
                )
            if self.needs_sequence:
                header.extend(["from collections.abc import Sequence", ""])
            header.extend(
                [
                    f"from typing import {', '.join(sorted(self.typing_imports))}",
                    "",
                    f"from pydantic import {', '.join(sorted(self.pydantic_imports))}",
                    "",
                    "from ..core.version_envelope import AdcpVersionEnvelope",
                ]
            )
            for import_line in sorted(self.imports):
                header.append(import_line)
            body = []
            body.extend(self.nested)
            body.extend(response_classes)
            union = " | ".join(class_names)
            body.append(f"{self.base}: TypeAlias = {union}")
            all_names = [self.base, *class_names, *sorted(self.nested_names)]
            all_block = ["__all__ = ["]
            for name in all_names:
                all_block.append(f"    {name!r},")
            all_block.append("]")
            body.append("\n".join(all_block))
            return "\n".join(header) + "\n\n\n" + "\n\n\n".join(body) + "\n"

    for relative, base in response_specs:
        target = OUTPUT_DIR / relative
        if not target.exists():
            print(f"  {relative} not found (skipping response arms)")
            continue
        schema_rel = _schema_relative(relative)
        schema_path = SCHEMA_DIR / schema_rel
        if not schema_path.exists():
            print(f"  {schema_rel} not found (skipping response arms)")
            continue
        emitter = Emitter(relative, base, schema_rel)
        new_source = emitter.render(_load_schema(schema_rel))
        if target.read_text() != new_source:
            target.write_text(new_source)
            fixed += 1
            print(f"  {relative}: regenerated response arms from schema")
        else:
            print(f"  {relative}: schema-derived response arms already current")

    print(f"  ✓ Regenerated schema-derived response arms: {fixed}")


def fix_comply_controller_account_optional() -> None:
    """Keep comply_test_controller request constructible for discovery calls."""
    target = OUTPUT_DIR / "compliance" / "comply_test_controller_request.py"
    if not target.exists():
        print("  compliance/comply_test_controller_request.py not found (skipping account)")
        return
    source = target.read_text()
    if "account: Annotated[\n        Account | None," in source:
        print("  compliance/comply_test_controller_request.py account already optional")
        return
    new_source = source.replace(
        "account: Annotated[\n        Account,\n        Field(",
        "account: Annotated[\n        Account | None,\n        Field(",
        1,
    )
    new_source = new_source.replace(
        "    ]\n",
        "    ] = None\n",
        1 if new_source == source else 0,
    )
    if new_source == source:
        print("  compliance/comply_test_controller_request.py account rewrite failed")
        return
    # The generic bracket replacement above is intentionally avoided because
    # the request has many fields. Anchor on the account block tail instead.
    new_source = new_source.replace(
        '            description="Sandbox account assertion. The runner MUST set sandbox: true on every comply_test_controller request. The seller MUST refuse the request (returning a structured error) if the targeted account is not a sandbox account in the seller\'s persisted records. This field is a caller-side declaration of intent — it does not grant sandbox status; sellers verify against their own account state. The (Sandbox) verification tier is defined by this gate: real production endpoints accept sandbox-flagged traffic and process it without real-world side effects, no separate test-mode endpoint required. See spec issue #3755 and the (Sandbox) framing in #4379."\n        ),\n    ]\n',
        '            description="Sandbox account assertion. The runner MUST set sandbox: true on every comply_test_controller request. The seller MUST refuse the request (returning a structured error) if the targeted account is not a sandbox account in the seller\'s persisted records. This field is a caller-side declaration of intent — it does not grant sandbox status; sellers verify against their own account state. The (Sandbox) verification tier is defined by this gate: real production endpoints accept sandbox-flagged traffic and process it without real-world side effects, no separate test-mode endpoint required. See spec issue #3755 and the (Sandbox) framing in #4379."\n        ),\n    ] = None\n',
        1,
    )
    target.write_text(new_source)
    print("  compliance/comply_test_controller_request.py: made account optional")


def fix_check_governance_status_alias() -> None:
    """Accept legacy ``status`` as the renamed ``verdict`` field."""
    target = OUTPUT_DIR / "governance" / "check_governance_response.py"
    if not target.exists():
        print("  governance/check_governance_response.py not found (skipping status alias)")
        return
    source = target.read_text()
    if "def _status_to_verdict" in source:
        new_source = source.replace(
            "def _status_to_verdict(cls, data):", "def _status_to_verdict(cls, data: Any) -> Any:"
        )
        if new_source != source:
            target.write_text(new_source)
        print("  governance/check_governance_response.py status alias already installed")
        return
    source = source.replace(
        "from pydantic import AwareDatetime, ConfigDict, Field",
        "from pydantic import AwareDatetime, ConfigDict, Field, model_validator",
        1,
    )
    marker = "class CheckGovernanceResponse(AdcpVersionEnvelope):\n"
    method = """class CheckGovernanceResponse(AdcpVersionEnvelope):
    @model_validator(mode='before')
    @classmethod
    def _status_to_verdict(cls, data: Any) -> Any:
        if isinstance(data, dict) and 'verdict' not in data and 'status' in data:
            data = dict(data)
            data['verdict'] = data['status']
        return data

"""
    if marker not in source:
        print("  governance/check_governance_response.py status alias marker not found")
        return
    target.write_text(source.replace(marker, method, 1))
    print("  governance/check_governance_response.py: mapped status to verdict")


def fix_report_plan_outcome_status_alias() -> None:
    """Accept legacy ``status`` as renamed ``outcome_state``."""
    target = OUTPUT_DIR / "governance" / "report_plan_outcome_response.py"
    if not target.exists():
        print("  governance/report_plan_outcome_response.py not found (skipping status alias)")
        return
    source = target.read_text()
    if "def _status_to_outcome_state" in source:
        new_source = source.replace(
            "def _status_to_outcome_state(cls, data):",
            "def _status_to_outcome_state(cls, data: Any) -> Any:",
        )
        if new_source != source:
            target.write_text(new_source)
        print("  governance/report_plan_outcome_response.py status alias already installed")
        return
    source = source.replace(
        "from pydantic import ConfigDict, Field",
        "from pydantic import ConfigDict, Field, model_validator",
        1,
    )
    marker = "class ReportPlanOutcomeResponse(AdcpVersionEnvelope):\n"
    method = """class ReportPlanOutcomeResponse(AdcpVersionEnvelope):
    @model_validator(mode='before')
    @classmethod
    def _status_to_outcome_state(cls, data: Any) -> Any:
        if isinstance(data, dict) and 'outcome_state' not in data and 'status' in data:
            data = dict(data)
            data['outcome_state'] = data['status']
        return data

"""
    if marker not in source:
        print("  governance/report_plan_outcome_response.py status alias marker not found")
        return
    target.write_text(source.replace(marker, method, 1))
    print("  governance/report_plan_outcome_response.py: mapped status to outcome_state")


def fix_response_payload_jws_required_literals() -> None:
    """Keep const literals required on the response-payload JWS schema."""
    target = OUTPUT_DIR / "core" / "response_payload_jws_envelope.py"
    if not target.exists():
        return

    source = target.read_text()
    updated = source.replace(
        "    ] = 'adcp-response-payload+jws'\n"
        "    task: Annotated[Task, Field(description='Designated task whose response payload is signed.')]\n",
        "    ]\n"
        "    task: Annotated[Task, Field(description='Designated task whose response payload is signed.')]\n",
        1,
    )

    if updated != source:
        target.write_text(updated)
        print("  core/response_payload_jws_envelope.py: required typ literal")
    else:
        print("  core/response_payload_jws_envelope.py typ literal already required")


def fix_verify_brand_claim_models() -> None:
    """Restore fields datamodel-codegen drops for oneOf + allOf brand claim schemas."""
    request = OUTPUT_DIR / "brand" / "verify_brand_claim_request.py"
    response = OUTPUT_DIR / "brand" / "verify_brand_claim_response.py"
    bulk_response = OUTPUT_DIR / "brand" / "verify_brand_claims_response.py"

    if request.exists() and "claim_type:" not in request.read_text():
        request.write_text(
            """# generated by datamodel-codegen:
#   filename:  brand/verify_brand_claim_request.json

from __future__ import annotations

from enum import Enum
from typing import Any, Annotated

from pydantic import AnyUrl, ConfigDict, Field

from ..core.version_envelope import AdcpVersionEnvelope


class ClaimType(Enum):
    subsidiary = 'subsidiary'
    parent = 'parent'
    property = 'property'
    trademark = 'trademark'


class VerifyBrandClaimRequest(AdcpVersionEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    claim_type: Annotated[
        ClaimType,
        Field(description='Discriminates the kind of brand claim being verified.'),
    ]
    claim: Annotated[
        dict[str, Any],
        Field(description='Claim payload. Shape varies by claim_type.'),
    ]
"""
        )
        print("  brand/verify_brand_claim_request.py: restored claim fields")

    if response.exists() and "VerifyBrandClaimSuccessResponse" not in response.read_text():
        response.write_text(
            """# generated by datamodel-codegen:
#   filename:  brand/verify_brand_claim_response.json

from __future__ import annotations

from enum import Enum
from typing import Any, Annotated

from pydantic import AnyUrl, ConfigDict, Field

from ..core import context as context_1
from ..core import error as error_1
from ..core import ext as ext_1
from ..core.protocol_envelope import ProtocolEnvelope
from ..core.version_envelope import AdcpVersionEnvelope
from . import verification_status


class ClaimType(Enum):
    subsidiary = 'subsidiary'
    parent = 'parent'
    property = 'property'
    trademark = 'trademark'


class VerifyBrandClaimSuccessResponse(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    claim_type: Annotated[
        ClaimType,
        Field(description="Echoes the request's claim_type for caller-side routing."),
    ]
    verification_status: Annotated[
        verification_status.VerificationStatus,
        Field(description='Verification status for this claim.'),
    ]
    details: Annotated[
        dict[str, Any] | None,
        Field(description='Per-claim-type response fields. Shape varies by claim_type.'),
    ] = None
    context_note: Annotated[
        str | None,
        Field(description='Public free-text context the brand chooses to surface.', max_length=500),
    ] = None
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


class VerifyBrandClaimErrorResponse(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    errors: Annotated[list[error_1.Error], Field(min_length=1)]
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


VerifyBrandClaimResponse = VerifyBrandClaimSuccessResponse | VerifyBrandClaimErrorResponse
"""
        )
        print("  brand/verify_brand_claim_response.py: restored response arms")

    response_schema = SCHEMA_DIR / "brand" / "verify-brand-claim-response.json"
    if (
        response.exists()
        and response_schema.exists()
        and '"signed_response"' in response_schema.read_text()
    ):
        response.write_text(
            """# generated by datamodel-codegen:
#   filename:  brand/verify_brand_claim_response.json

from __future__ import annotations

from enum import Enum
from typing import Any, Annotated, Literal

from adcp.types.base import AdCPBaseModel
from pydantic import AnyUrl, ConfigDict, Field

from ..core import context as context_1
from ..core import error as error_1
from ..core import ext as ext_1
from ..core.protocol_envelope import ProtocolEnvelope
from ..core.version_envelope import AdcpVersionEnvelope
from . import verification_status


class ClaimType(Enum):
    subsidiary = 'subsidiary'
    parent = 'parent'
    property = 'property'
    trademark = 'trademark'


class VerifyBrandClaimSignedSuccessPayload(AdCPBaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    claim_type: ClaimType
    verification_status: verification_status.VerificationStatus
    details: dict[str, Any] | None = None
    context_note: Annotated[str | None, Field(max_length=500)] = None
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


class VerifyBrandClaimPayload(AdCPBaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    typ: Annotated[
        Literal['adcp-response-payload+jws'],
        Field(description='Type discriminator preventing cross-profile replay.'),
    ]
    task: Annotated[
        Literal['verify_brand_claim'],
        Field(description='Designated task whose response payload is signed.'),
    ]
    brand_domain: Annotated[
        str,
        Field(
            description='Brand tenant whose policy store produced the answer. The signer MUST derive this from server-side tenant resolution, not caller-supplied request fields.',
            pattern='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$',
        ),
    ]
    agent_url: Annotated[
        AnyUrl,
        Field(
            description='Canonical URL of the responding brand agent entry whose response-signing key verifies this envelope.'
        ),
    ]
    request_hash: Annotated[
        str,
        Field(
            description='sha256: prefix plus unpadded base64url SHA-256 of the canonical request-binding object for this call.',
            pattern='^sha256:[A-Za-z0-9_-]{43}$',
        ),
    ]
    iat: Annotated[int, Field(description='Issued-at time as Unix epoch seconds.', ge=0)]
    exp: Annotated[
        int,
        Field(
            description='Expiration time as Unix epoch seconds. Online verifiers reject envelopes after this time, allowing only implementation-defined clock skew.',
            ge=0,
        ),
    ]
    response: VerifyBrandClaimSignedSuccessPayload


class VerifyBrandClaimSignedResponse(AdCPBaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    protected: Annotated[
        str,
        Field(
            description='Base64url-encoded JWS protected header. The decoded header MUST include alg, kid, and typ: adcp-response-payload+jws, and MUST NOT include the RFC 7797 b64 header. Verifiers enforce the key purpose by resolving kid to a JWK with adcp_use: response-signing.',
            pattern='^[A-Za-z0-9_-]+$',
        ),
    ]
    payload: Annotated[
        VerifyBrandClaimPayload,
        Field(
            description='Decoded signed payload. Signers compute the JWS payload bytes from the RFC 8785/JCS canonicalization of this object.'
        ),
    ]
    signature: Annotated[
        str,
        Field(
            description='Base64url-encoded JWS signature over the protected header and canonicalized payload.',
            pattern='^[A-Za-z0-9_-]+$',
        ),
    ]


class VerifyBrandClaimSuccessResponse(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    claim_type: Annotated[
        ClaimType,
        Field(description="Echoes the request's claim_type for caller-side routing."),
    ]
    verification_status: Annotated[
        verification_status.VerificationStatus,
        Field(
            description='Verification status. Not every status applies to every claim_type - see the task page for the applicable subset. Renamed from `status` in 3.1 to free the top-level `status` key for the envelope task-status (TaskStatus) under MCP flat-on-the-wire serialization (#4878).'
        ),
    ]
    signed_response: Annotated[
        VerifyBrandClaimSignedResponse,
        Field(
            description='Payload-envelope JWS attesting the canonical success response for verify_brand_claim. The signed payload response MUST match the unsigned task-body fields on this response, excluding signed_response and protocol/version envelope fields.'
        ),
    ]
    details: Annotated[
        dict[str, Any] | None,
        Field(
            description="Per-claim-type response fields. Shape varies - see the task page for each claim_type's expected fields."
        ),
    ] = None
    context_note: Annotated[
        str | None,
        Field(description='Public - free-text context the brand chooses to surface.', max_length=500),
    ] = None
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


class VerifyBrandClaimErrorResponse(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    errors: Annotated[list[error_1.Error], Field(min_length=1)]
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


VerifyBrandClaimResponse = VerifyBrandClaimSuccessResponse | VerifyBrandClaimErrorResponse
"""
        )
        print("  brand/verify_brand_claim_response.py: restored signed response fields")

    bulk_response_schema = SCHEMA_DIR / "brand" / "verify-brand-claims-response.json"
    if (
        bulk_response.exists()
        and bulk_response_schema.exists()
        and '"signed_response"' in bulk_response_schema.read_text()
    ):
        bulk_response.write_text(
            """# generated by datamodel-codegen:
#   filename:  brand/verify_brand_claims_response.json

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from adcp.types.base import AdCPBaseModel
from pydantic import AnyUrl, ConfigDict, Field, RootModel

from ..core import context as context_1
from ..core import error as error_1
from ..core import ext as ext_1
from ..core.protocol_envelope import ProtocolEnvelope
from ..core.version_envelope import AdcpVersionEnvelope
from . import verification_status


class ClaimType(Enum):
    subsidiary = 'subsidiary'
    parent = 'parent'
    property = 'property'
    trademark = 'trademark'


class ResultEntry1(AdCPBaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    claim_type: Annotated[
        ClaimType,
        Field(description="Echoes the request item's claim_type for caller-side routing."),
    ]
    status: Annotated[
        verification_status.VerificationStatus,
        Field(
            description='Verification status for this claim. Not every status applies to every claim_type - see the single-target task page for the applicable subset.'
        ),
    ]
    details: Annotated[
        dict[str, Any] | None,
        Field(
            description="Per-claim-type response fields. Shape varies - see the single-target task page for each claim_type's expected fields."
        ),
    ] = None
    context_note: Annotated[
        str | None,
        Field(description='Public - free-text context the brand chooses to surface.', max_length=500),
    ] = None


class ResultEntry2(AdCPBaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    error: error_1.Error


class ResultEntry(RootModel[ResultEntry1 | ResultEntry2]):
    root: Annotated[
        ResultEntry1 | ResultEntry2,
        Field(
            description='One entry in `results[]`. Either a per-claim success (claim_type + status + optional details/context_note) or a per-claim error (error field only). Mirrors the single-target `verify_brand_claim` response success arm shape.'
        ),
    ]

    def __getattr__(self, name: str) -> Any:
        \"\"\"Proxy attribute access to the wrapped type.\"\"\"
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self.root, name)


class VerifyBrandClaimsSignedSuccessPayload(AdCPBaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    results: Annotated[list[ResultEntry], Field(min_length=1)]
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


class VerifyBrandClaimsPayload(AdCPBaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    typ: Annotated[
        Literal['adcp-response-payload+jws'],
        Field(description='Type discriminator preventing cross-profile replay.'),
    ]
    task: Annotated[
        Literal['verify_brand_claims'],
        Field(description='Designated task whose response payload is signed.'),
    ]
    brand_domain: Annotated[
        str,
        Field(
            description='Brand tenant whose policy store produced the answer. The signer MUST derive this from server-side tenant resolution, not caller-supplied request fields.',
            pattern='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$',
        ),
    ]
    agent_url: Annotated[
        AnyUrl,
        Field(
            description='Canonical URL of the responding brand agent entry whose response-signing key verifies this envelope.'
        ),
    ]
    request_hash: Annotated[
        str,
        Field(
            description='sha256: prefix plus unpadded base64url SHA-256 of the canonical request-binding object for this call.',
            pattern='^sha256:[A-Za-z0-9_-]{43}$',
        ),
    ]
    iat: Annotated[int, Field(description='Issued-at time as Unix epoch seconds.', ge=0)]
    exp: Annotated[
        int,
        Field(
            description='Expiration time as Unix epoch seconds. Online verifiers reject envelopes after this time, allowing only implementation-defined clock skew.',
            ge=0,
        ),
    ]
    response: VerifyBrandClaimsSignedSuccessPayload


class VerifyBrandClaimsSignedResponse(AdCPBaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    protected: Annotated[
        str,
        Field(
            description='Base64url-encoded JWS protected header. The decoded header MUST include alg, kid, and typ: adcp-response-payload+jws, and MUST NOT include the RFC 7797 b64 header. Verifiers enforce the key purpose by resolving kid to a JWK with adcp_use: response-signing.',
            pattern='^[A-Za-z0-9_-]+$',
        ),
    ]
    payload: Annotated[
        VerifyBrandClaimsPayload,
        Field(
            description='Decoded signed payload. Signers compute the JWS payload bytes from the RFC 8785/JCS canonicalization of this object.'
        ),
    ]
    signature: Annotated[
        str,
        Field(
            description='Base64url-encoded JWS signature over the protected header and canonicalized payload.',
            pattern='^[A-Za-z0-9_-]+$',
        ),
    ]


class VerifyBrandClaimsResponseBulk(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    results: Annotated[
        list[ResultEntry],
        Field(
            description="Per-claim results, positionally aligned with the request's claims.",
            min_length=1,
        ),
    ]
    signed_response: Annotated[
        VerifyBrandClaimsSignedResponse,
        Field(
            description='Payload-envelope JWS attesting the canonical bulk success response for verify_brand_claims. The signed payload response MUST match the unsigned task-body fields on this response, excluding signed_response and protocol/version envelope fields.'
        ),
    ]
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


class VerifyBrandClaimsErrorResponse(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    errors: Annotated[list[error_1.Error], Field(min_length=1)]
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None


VerifyBrandClaimsResponse = VerifyBrandClaimsResponseBulk | VerifyBrandClaimsErrorResponse
"""
        )
        print("  brand/verify_brand_claims_response.py: restored signed response fields")

    if bulk_response.exists():
        source = bulk_response.read_text()
        if "results:" not in source and "class VerifyBrandClaimsResponseBulk" in source:
            source = source.replace(
                "from typing import Any, Annotated, Any",
                "from typing import Any, Annotated",
                1,
            )
            if "from ..core import context as context_1" not in source:
                source = source.replace(
                    "from ..core import error as error_1\n",
                    "from ..core import context as context_1\n"
                    "from ..core import error as error_1\n"
                    "from ..core import ext as ext_1\n",
                    1,
                )
            source = source.replace(
                "class VerifyBrandClaimsResponseBulk(AdcpVersionEnvelope, ProtocolEnvelope):\n    pass\n",
                """class VerifyBrandClaimsResponseBulk(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(
        extra='allow',
    )
    results: Annotated[
        list[ResultEntry] | None,
        Field(
            description="Per-claim results, positionally aligned with the request's claims.",
            min_length=1,
        ),
    ] = None
    errors: Annotated[list[error_1.Error] | None, Field(min_length=1)] = None
    context: context_1.ContextObject | None = None
    ext: ext_1.ExtensionObject | None = None
""",
                1,
            )
            bulk_response.write_text(source)
            print("  brand/verify_brand_claims_response.py: restored result fields")

    bulk_request = OUTPUT_DIR / "brand" / "verify_brand_claims_request.py"
    if bulk_request.exists():
        source = bulk_request.read_text()
        if "class VerifyBrandClaimsRequest(" not in source:
            source = (
                source.rstrip()
                + "\n\n\nclass VerifyBrandClaimsRequest(VerifyBrandClaimsRequestBulk):\n    pass\n"
            )
            bulk_request.write_text(source)
            print("  brand/verify_brand_claims_request.py: added non-Bulk request alias")


def fix_signal_coverage_forecast_point_types() -> None:
    """Align signal coverage point narrowing with strict mypy.

    datamodel-codegen emits SignalCoverageForecast.Point as a subclass of
    ForecastPoint, then narrows metrics to a sibling Metrics model. Runtime
    validation is fine, but strict mypy rejects the field override because the
    sibling type is not assignable to the parent field. Making the local range
    and metrics models inherit their forecast_point counterparts preserves the
    schema intent while keeping subclass overrides type-compatible.
    """
    target = OUTPUT_DIR / "core" / "signal_coverage_forecast.py"
    if not target.exists():
        print("  core/signal_coverage_forecast.py: not found (skipping)")
        return

    source = target.read_text()
    source = source.replace(
        "from . import forecast_point_dimensions, forecast_range\n"
        "from .forecast_point import ForecastPoint\n"
        "from .forecast_range import ForecastRange\n",
        "from . import forecast_point, forecast_point_dimensions, forecast_range\n",
        1,
    )
    source = source.replace(
        "class CoverageRate(ForecastRange):", "class CoverageRate(forecast_point.CoverageRate):", 1
    )
    source = source.replace(
        "class Metrics(AdCPBaseModel):", "class Metrics(forecast_point.Metrics):", 1
    )
    source = source.replace(
        "class Point(ForecastPoint):", "class Point(forecast_point.ForecastPoint):", 1
    )
    source = source.replace(
        "    metrics: Annotated[\n"
        "        Metrics | None,\n"
        "        Field(\n"
        "            description='Forecasted metric values. Keys are forecastable-metric enum values for delivery/engagement or event-type enum values for outcomes. Values are ForecastRange objects (low/mid/high). Use { \"mid\": value } for point estimates. When budget is present, these are the expected metrics at that spend level. When budget is omitted, these represent total available inventory — use spend to express the estimated cost. Additional keys beyond the documented properties are allowed for event-type values (purchase, lead, app_install, etc.).'\n"
        "        ),\n"
        "    ] = None\n",
        "    metrics: Annotated[\n"
        "        Metrics,\n"
        "        Field(\n"
        "            description='Forecasted metric values. Keys are forecastable-metric enum values for delivery/engagement or event-type enum values for outcomes. Values are ForecastRange objects (low/mid/high). Use { \"mid\": value } for point estimates. When budget is present, these are the expected metrics at that spend level. When budget is omitted, these represent total available inventory — use spend to express the estimated cost. Additional keys beyond the documented properties are allowed for event-type values (purchase, lead, app_install, etc.).'\n"
        "        ),\n"
        "    ]\n",
        1,
    )

    target.write_text(source)
    print("  core/signal_coverage_forecast.py: aligned Point field overrides")


def fix_registry_collection_payload_status_override() -> None:
    """Suppress strict-mypy noise for registry collection removal payloads.

    The 3.1 registry event schema models collection removal as a
    ``CollectionPayload`` subclass whose ``status`` field is narrowed to the
    literal ``"removed"``. Runtime validation is correct, but mypy rejects the
    subclass field override because the parent field is ``Status | None``.
    """
    target = OUTPUT_DIR / "core" / "registry_event.py"
    if not target.exists():
        print("  core/registry_event.py: not found (skipping)")
        return

    source = target.read_text()
    before = "    status: Literal['removed'] = 'removed'\n"
    after = "    status: Literal['removed'] = 'removed'  # type: ignore[assignment]\n"
    if after in source:
        print("  core/registry_event.py: collection status override already suppressed")
        return
    if before not in source:
        print("  core/registry_event.py: collection status override not found")
        return

    target.write_text(source.replace(before, after, 1))
    print("  core/registry_event.py: suppressed collection status override")


def fix_list_creatives_format_reference_xor() -> None:
    """Restore list_creatives response ``format_id`` XOR ``format_kind``.

    The schema models list_creatives creative records as a oneOf:
    legacy items require ``format_id`` and forbid ``format_kind``;
    canonical items require ``format_kind`` and forbid ``format_id``.
    datamodel-code-generator preserves the required fields but drops the
    opposing ``not`` constraints, so add runtime validators to the generated
    branch models.
    """

    target = OUTPUT_DIR / "creative" / "list_creatives_response.py"
    if not target.exists():
        print("  creative/list_creatives_response.py: not found (skipping)")
        return

    source = target.read_text()
    if "class Creative(AdCPBaseModel):" in source and "class Creatives(" not in source:
        if "Creatives = Creative\nCreatives1 = Creative" in source:
            print("  creative/list_creatives_response.py: merged creative XOR already fixed")
            return

        source = source.replace(
            "from pydantic import AwareDatetime, ConfigDict, Field, RootModel, StringConstraints",
            "from pydantic import AwareDatetime, ConfigDict, Field, RootModel, StringConstraints, model_validator",
            1,
        )
        merged_validator = """

    @model_validator(mode='after')
    def _validate_format_reference_xor(self) -> Creative:
        if (self.format_id is None) == (self.format_kind is None):
            raise ValueError('exactly one of format_id and format_kind is required')
        return self


Creatives = Creative
Creatives1 = Creative
"""
        marker = "\n\nclass ListCreativesResponse(AdcpVersionEnvelope, ProtocolEnvelope):"
        if marker not in source:
            raise RuntimeError("ListCreativesResponse marker missing from merged creative output")
        target.write_text(source.replace(marker, merged_validator + marker, 1))
        print("  creative/list_creatives_response.py: restored merged creative XOR and aliases")
        return

    if "_reject_canonical_format_ref" in source and "_reject_legacy_format_ref" in source:
        print("  creative/list_creatives_response.py: format reference XOR already fixed")
        return

    source = source.replace(
        "from pydantic import AwareDatetime, ConfigDict, Field, RootModel, StringConstraints",
        "from pydantic import AwareDatetime, ConfigDict, Field, RootModel, StringConstraints, model_validator",
        1,
    )

    legacy_validator = """

    @model_validator(mode='after')
    def _reject_canonical_format_ref(self) -> Creatives:
        if self.format_kind is not None:
            raise ValueError('format_id and format_kind are mutually exclusive')
        return self
"""
    canonical_validator = """

    @model_validator(mode='after')
    def _reject_legacy_format_ref(self) -> Creatives1:
        if self.format_id is not None:
            raise ValueError('format_id and format_kind are mutually exclusive')
        return self
"""

    if "_reject_canonical_format_ref" not in source:
        source = source.replace(
            "\n\nclass Creatives1(AdCPBaseModel):",
            legacy_validator + "\n\nclass Creatives1(AdCPBaseModel):",
            1,
        )
    if "_reject_legacy_format_ref" not in source:
        source = source.replace(
            "\n\nclass ListCreativesResponse(AdcpVersionEnvelope, ProtocolEnvelope):",
            canonical_validator
            + "\n\nclass ListCreativesResponse(AdcpVersionEnvelope, ProtocolEnvelope):",
            1,
        )

    target.write_text(source)
    print("  creative/list_creatives_response.py: added format reference XOR validators")


def fix_creative_manifest_standalone_asset_coercion() -> None:
    """Let aggregate manifests accept the SDK's standalone asset models.

    The 3.2 aggregate asset-union schema inlines structurally duplicate asset
    classes. Pydantic therefore rejects a public ``ImageAsset`` instance even
    though its wire representation is valid for the inline ``ImageAsset``.
    Normalize model instances to wire dictionaries before union validation.
    """

    target = OUTPUT_DIR / "core" / "creative_manifest.py"
    if not target.exists():
        return
    source = target.read_text()
    # The injected normalizer is typed with Any, which datamodel-codegen does
    # not otherwise need for this schema.
    source = source.replace(
        "from typing import Annotated\n",
        "from typing import Annotated, Any\n",
        1,
    )
    if "_coerce_standalone_assets" in source:
        target.write_text(source)
        return
    source = source.replace(
        "from pydantic import ConfigDict, Field, RootModel, StringConstraints",
        "from pydantic import ConfigDict, Field, RootModel, StringConstraints, model_validator",
        1,
    )
    helper = """

def _normalize_asset_models(value: Any) -> Any:
    if isinstance(value, AdCPBaseModel):
        return value.model_dump(mode='json', exclude_none=True)
    if isinstance(value, list):
        return [_normalize_asset_models(item) for item in value]
    return value
"""
    source = source.replace("\n\nclass Assets(", helper + "\n\nclass Assets(", 1)
    validator = """

    @model_validator(mode='before')
    @classmethod
    def _coerce_standalone_assets(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get('assets'), dict):
            return data
        return {
            **data,
            'assets': {key: _normalize_asset_models(value) for key, value in data['assets'].items()},
        }
"""
    config = "    model_config = ConfigDict(\n        extra='allow',\n    )"
    source = source.replace(config, config + validator, 2)
    target.write_text(source)
    print("  core/creative_manifest.py: added standalone asset coercion")


def fix_update_rights_legacy_response_defaults() -> None:
    """Accept pre-3.2 update_rights success payloads when no version is sent."""

    target = OUTPUT_DIR / "brand" / "update_rights_response.py"
    if not target.exists():
        return
    source = target.read_text()
    source = source.replace(
        "    generation_credentials: list[generation_credential_1.GenerationCredential]\n"
        "    rights_constraint: Any\n",
        "    generation_credentials: list[generation_credential_1.GenerationCredential] = Field(\n"
        "        default_factory=list\n"
        "    )\n"
        "    rights_constraint: Any | None = None\n",
        1,
    )
    target.write_text(source)


def strip_extra_blank_lines_at_eof() -> None:
    """Normalize generated Python files to one trailing newline."""
    changed = 0
    for path in OUTPUT_DIR.rglob("*.py"):
        source = path.read_text()
        normalized = source.rstrip("\n") + "\n"
        if normalized != source:
            path.write_text(normalized)
            changed += 1
    print(f"  generated Python EOF whitespace normalized ({changed} files)")


def fix_compliance_task_completion_response_ref() -> None:
    """Point the completion JSON Pointer at the restored success arm.

    datamodel-code-generator names ``create-media-buy-response#/oneOf/0`` as
    ``Field0``. ``restore_response_variant_aliases`` replaces that generated
    module with stable semantic arm names, where the same schema is
    ``CreateMediaBuyResponse1``.
    """
    target = OUTPUT_DIR / "compliance" / "task_completion_data.py"
    if not target.exists():
        return
    source = target.read_text()
    fixed = source.replace(
        "create_media_buy_response.Field0",
        "create_media_buy_response.CreateMediaBuyResponse1",
    )
    if fixed != source:
        target.write_text(fixed)
        print("  compliance/task_completion_data.py: restored create-media-buy success arm")


def fix_product_fields_item_reference() -> None:
    """Point the get-products item fragment at its generated enum.

    ``product-fields.json`` is a root array whose ``#/items`` schema becomes
    ``ProductResponseField``. datamodel-code-generator nevertheless emits a
    reference to a non-existent ``product_fields.Items`` when that fragment is
    used by ``get-products-request.json``.
    """
    target = OUTPUT_DIR / "media_buy" / "get_products_request.py"
    if not target.exists():
        return
    source = target.read_text()
    fixed = source.replace(
        "product_fields.Items",
        "product_fields.ProductResponseField",
    )
    if fixed != source:
        target.write_text(fixed)
        print("  media_buy/get_products_request.py: restored product field item enum")


def restore_get_products_field_compatibility_enum() -> None:
    """Restore the public combined get-products projection enum.

    Beta.9 split canonical product fields into ``product-fields.json`` while
    retaining get-products-only compatibility values inline. Codegen models
    those as two separate enums, but earlier SDK releases exposed their union
    as ``GetProductsField`` (the generated ``Field1`` class). Recreate that
    union from the generated enum members so the public API remains source
    compatible without duplicating the schema vocabulary by hand.
    """

    target = OUTPUT_DIR / "media_buy" / "get_products_request.py"
    product_fields = OUTPUT_DIR / "media_buy" / "product_fields.py"
    if not target.exists() or not product_fields.exists():
        return
    source = target.read_text()
    if "class Field1(StrEnum):" in source:
        return

    def enum_members(module_source: str, class_name: str) -> list[tuple[str, str]]:
        tree = ast.parse(module_source)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name != class_name:
                continue
            members: list[tuple[str, str]] = []
            for statement in node.body:
                if (
                    isinstance(statement, ast.Assign)
                    and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ):
                    members.append((statement.targets[0].id, statement.value.value))
            return members
        return []

    members = enum_members(product_fields.read_text(), "ProductResponseField")
    members.extend(enum_members(source, "Fields"))
    deduplicated = list(dict.fromkeys(members))
    marker = "\n\nclass GetProductsRequest(AdcpVersionEnvelope):"
    if not deduplicated or marker not in source:
        return
    body = "\n".join(f"    {name} = {value!r}" for name, value in deduplicated)
    compatibility_enum = (
        "\n\nclass Field1(StrEnum):\n"
        '    """Compatibility union of canonical and get-products-only fields."""\n\n'
        f"{body}\n"
    )
    target.write_text(source.replace(marker, compatibility_enum + marker, 1))
    print("  media_buy/get_products_request.py: restored combined field enum")


def fix_audience_evidence_attestation_subject() -> None:
    """Keep the audience-evidence attestation subject narrowed to its resource arm.

    The schema's allOf constrains ``attestation_refs[].subject`` to the
    ``resource`` variant for the evidence snapshot. Codegen instead emits
    three overlapping RootModel unions and applies another ``type``
    discriminator around them, so every wrapper advertises ``brand``,
    ``agent``, and ``resource`` and Pydantic rejects the duplicate tags.
    The generated merged resource arm has the required audience-evidence
    resource type, content digest, namespace, and identifier. Its numeric
    suffix is an implementation detail that changes as schemas evolve.
    """
    target = OUTPUT_DIR / "core" / "audience_evidence.py"
    if not target.exists():
        return
    source = target.read_text()
    class_start = source.find("class AttestationRef(AttestationReference):")
    next_class = source.find("\nclass AudienceEvidence(", class_start)
    if class_start < 0 or next_class < 0:
        return
    subject_class = None
    for match in re.finditer(
        r"class (Subject\d+)\(AdCPBaseModel\):\n(?P<body>.*?)(?=\n\nclass )",
        source,
        flags=re.DOTALL,
    ):
        body = match.group("body")
        if (
            "claims/subjects/audience-evidence" in body
            and "content_digest: Annotated[str" in body
            and "namespace: Annotated[" in body
            and "id: Annotated[" in body
        ):
            subject_class = match.group(1)
            break
    if subject_class is None:
        return
    fixed_class = f"class AttestationRef(AttestationReference):\n    subject: {subject_class}\n\n"
    fixed = source[:class_start] + fixed_class + source[next_class + 1 :]
    if fixed != source:
        target.write_text(fixed)
        print("  core/audience_evidence.py: narrowed attestation subject to resource arm")


def fix_legacy_purchase_accepted_losses() -> None:
    """Restore coordinator-input constraints codegen cannot represent.

    The beta.5 schema expresses a non-empty enum array with ``oneOf`` plus a
    negated empty-array arm. datamodel-code-generator interprets that negated
    arm as an empty object model and emits ``list[AcceptedLoss] |
    AcceptedLosses``. Besides accepting the wrong shape in the annotation,
    Pydantic can raise a raw ``TypeError`` while trying the empty model arm.
    The protocol contract is simply a list, with length and enum validation.
    Codegen also drops ``uniqueItems``, ``contains``, and ``minProperties``;
    inject validators and JSON Schema metadata so this public SDK-local model
    preserves those constraints independently of the coordinator.
    """

    target = OUTPUT_DIR / "media_buy" / "legacy_purchase_continuation_input.py"
    if not target.exists():
        return
    source = target.read_text()
    fixed = re.sub(
        r"\n\nclass AcceptedLosses\(AdCPBaseModel\):\n    pass\n",
        "",
        source,
        count=1,
    )
    fixed = fixed.replace("list[AcceptedLoss] | AcceptedLosses", "list[AcceptedLoss]", 1)
    if "from pydantic import " in fixed and "field_validator" not in fixed:
        fixed = re.sub(
            r"from pydantic import ([^\n]+)",
            lambda match: f"from pydantic import {match.group(1)}, field_validator",
            fixed,
            count=1,
        )
    fixed = fixed.replace(
        "description='Non-empty subset of the product IDs bound into the continuation.',\n"
        "            min_length=1,\n",
        "description='Non-empty subset of the product IDs bound into the continuation.',\n"
        "            min_length=1,\n"
        "            json_schema_extra={'uniqueItems': True},\n",
        1,
    )
    fixed = fixed.replace(
        "description='Exact loss set returned with the continuation. Missing, extra, or stale consent fails before mutation.',\n"
        "            min_length=2,\n",
        "description='Exact loss set returned with the continuation. Missing, extra, or stale consent fails before mutation.',\n"
        "            min_length=2,\n"
        "            json_schema_extra={\n"
        "                'uniqueItems': True,\n"
        "                'allOf': [\n"
        "                    {'contains': {'const': 'feed_version_not_atomic'}},\n"
        "                    {'contains': {'const': 'pricing_version_not_atomic'}},\n"
        "                ],\n"
        "            },\n",
        1,
    )
    # Newer datamodel-code-generator releases preserve these JSON Schema
    # constraints themselves. Keep the compatibility injection idempotent
    # when the generated source already contains the same metadata.
    selected_metadata = "            json_schema_extra={'uniqueItems': True},\n"
    while selected_metadata * 2 in fixed:
        fixed = fixed.replace(selected_metadata * 2, selected_metadata)
    accepted_metadata = (
        "            json_schema_extra={\n"
        "                'uniqueItems': True,\n"
        "                'allOf': [\n"
        "                    {'contains': {'const': 'feed_version_not_atomic'}},\n"
        "                    {'contains': {'const': 'pricing_version_not_atomic'}},\n"
        "                ],\n"
        "            },\n"
    )
    while accepted_metadata * 2 in fixed:
        fixed = fixed.replace(accepted_metadata * 2, accepted_metadata)
    fixed = fixed.replace(
        "description='Proposed create_media_buy payload. Before mutation the coordinator validates this object against create-media-buy-request.json from source_adcp_version, requires explicit-package mode, and requires its package product IDs to equal selected_product_ids.'\n"
        "        ),\n"
        "    ]",
        "description='Proposed create_media_buy payload. Before mutation the coordinator validates this object against create-media-buy-request.json from source_adcp_version, requires explicit-package mode, and requires its package product IDs to equal selected_product_ids.',\n"
        "            min_length=1,\n"
        "        ),\n"
        "    ]",
        1,
    )
    validator_marker = "    def _accepted_losses_match_schema("
    if validator_marker not in fixed and "field_validator" in fixed:
        fixed = (
            fixed.rstrip()
            + """


    @field_validator('selected_product_ids')
    @classmethod
    def _selected_product_ids_are_unique(
        cls, values: list[SelectedProductId]
    ) -> list[SelectedProductId]:
        if len(values) != len({value.root for value in values}):
            raise ValueError('selected_product_ids must contain unique items')
        return values

    @field_validator('accepted_losses')
    @classmethod
    def _accepted_losses_match_schema(
        cls, values: list[AcceptedLoss]
    ) -> list[AcceptedLoss]:
        value_set = set(values)
        if len(values) != len(value_set):
            raise ValueError('accepted_losses must contain unique items')
        required = {
            AcceptedLoss.feed_version_not_atomic,
            AcceptedLoss.pricing_version_not_atomic,
        }
        if not required.issubset(value_set):
            raise ValueError('accepted_losses must include the required compatibility losses')
        return values
"""
        )
    if fixed != source:
        target.write_text(fixed)
        print("  media_buy/legacy_purchase_continuation_input.py: narrowed accepted_losses")


def preserve_request_signing_operation_strings() -> None:
    """Keep request-signing operation lists source-compatible with beta.5.

    Beta.6 adds an item-level pattern to the three AdCP operation-name lists.
    datamodel-code-generator represents the referenced constrained string as a
    ``RootModel[str]``, which changes parsed list items from ``str`` objects to
    wrappers.  Preserve the new validation while keeping ordinary string
    values so existing membership checks and string operations keep working.

    The JSON-RPC protocol-method lists intentionally retain their existing
    generated item models; this compatibility fix is scoped to the three
    fields whose public runtime shape changed in beta.6.
    """

    targets = (
        OUTPUT_DIR / "protocol" / "get_adcp_capabilities_response.py",
        OUTPUT_DIR / "bundled" / "protocol" / "get_adcp_capabilities_response.py",
    )
    operation_item = "Annotated[str, Field(pattern='^[a-z][a-z0-9_]*$')]"
    item_model = re.compile(r"list\[(?:RequiredForItem|WarnForItem|SupportedForItem)\d*\]")

    for target in targets:
        if not target.exists():
            continue
        source = target.read_text()
        class_start = source.find("class RequestSigning(AdCPBaseModel):")
        class_end = source.find("\n\nclass ", class_start)
        if class_start < 0 or class_end < 0:
            continue
        request_signing = source[class_start:class_end]
        fixed_class, replacements = item_model.subn(f"list[{operation_item}]", request_signing)
        if replacements:
            fixed = source[:class_start] + fixed_class + source[class_end:]
            target.write_text(fixed)
            print(f"  {target.relative_to(OUTPUT_DIR)}: preserved request-signing strings")


def enforce_change_term_runtime_constraints() -> None:
    """Preserve beta.9 change-right verifier constraints in Pydantic models.

    datamodel-code-generator represents the constraint ``oneOf`` arms but
    drops each arm's nested ``anyOf(required=...)`` rule, along with the
    schema's ``x-adcp-validation`` cross-field assertions.  Restore those
    checks so direct Python model construction cannot create a proposal that
    the released JSON Schema or seller resolver would reject.
    """

    action_path = OUTPUT_DIR / "core" / "canonical_media_buy_action.py"
    if action_path.exists():
        source = action_path.read_text()
        for action_type in ("Action", "Action2", "Action3"):
            source = source.replace(
                f"    action: {action_type} | None = None\n",
                f"    action: {action_type}\n",
            )
        action_path.write_text(source)
        print("  core/canonical_media_buy_action.py: restored required action fields")

    constraints_path = OUTPUT_DIR / "media_buy" / "change_term_constraints.py"
    if constraints_path.exists():
        source = constraints_path.read_text()
        pydantic_import = "from pydantic import AwareDatetime, ConfigDict, Field, RootModel"
        if f"{pydantic_import}, model_validator" not in source:
            source = source.replace(
                pydantic_import,
                f"{pydantic_import}, model_validator",
                1,
            )
        required_fields = {
            "MediaBuyChangeTermConstraints1": (
                "max_delta_amount",
                "max_delta_percent",
                "min_result_amount",
                "max_result_amount",
            ),
            "MediaBuyChangeTermConstraints2": (
                "max_change",
                "earliest_result",
                "latest_result",
                "minimum_notice",
            ),
            "MediaBuyChangeTermConstraints3": (
                "max_additions",
                "max_removals",
                "max_result_count",
            ),
            "MediaBuyChangeTermConstraints4": (
                "minimum_notice",
                "earliest_effective_at",
                "latest_effective_at",
            ),
        }
        for class_name, fields in required_fields.items():
            marker = f"class {class_name}(AdCPBaseModel):"
            start = source.find(marker)
            if start < 0:
                continue
            next_class = source.find("\n\nclass ", start + len(marker))
            end = len(source) if next_class < 0 else next_class
            block = source[start:end]
            if "def _require_portable_bound" in block:
                continue
            field_tuple = repr(fields)
            validator = f"""

    @model_validator(mode='after')
    def _require_portable_bound(self) -> {class_name}:
        if not any(getattr(self, name) is not None for name in {field_tuple}):
            raise ValueError('at least one portable constraint bound is required')
        return self
"""
            source = source[:end] + validator + source[end:]
        constraints_path.write_text(source)
        print("  media_buy/change_term_constraints.py: restored anyOf required bounds")

    term_path = OUTPUT_DIR / "media_buy" / "change_term.py"
    if term_path.exists():
        source = term_path.read_text()
        source = source.replace(
            "from pydantic import ConfigDict, Field, RootModel",
            "from pydantic import ConfigDict, Field, RootModel, model_validator",
        )
        class_start = source.find("class MediaBuyChangeTerm(AdCPBaseModel):")
        if class_start >= 0 and "def _validate_constraint_action" not in source[class_start:]:
            source = (
                source.rstrip()
                + """

    @model_validator(mode='after')
    def _validate_constraint_action(self) -> MediaBuyChangeTerm:
        if self.constraints is None:
            return self
        kind = self.constraints.kind
        allowed = {
            'budget': {
                'increase_budget', 'decrease_budget', 'reallocate_budget',
                'update_budget_allocation', 'update_spend_target',
            },
            'flight': {'extend_flight', 'shorten_flight', 'update_flight_dates'},
            'package_count': {'add_packages', 'remove_packages'},
            'effective_timing': {'pause', 'resume', 'cancel'},
        }
        action = self.action.value
        if action not in allowed.get(kind, set()):
            raise ValueError('constraint kind is incompatible with action')
        return self
"""
                + "\n"
            )
            term_path.write_text(source)
            print("  media_buy/change_term.py: restored constraint/action compatibility")

    terms_path = OUTPUT_DIR / "media_buy" / "commercial_terms.py"
    if terms_path.exists():
        source = terms_path.read_text()
        source = source.replace(
            "from pydantic import AwareDatetime, ConfigDict, Field",
            "from pydantic import AwareDatetime, ConfigDict, Field, model_validator",
        )
        class_start = source.find("class CommercialTerms(AdCPBaseModel):")
        if class_start >= 0 and "def _validate_change_term_set" not in source[class_start:]:
            source = (
                source.rstrip()
                + """

    @model_validator(mode='after')
    def _validate_change_term_set(self) -> CommercialTerms:
        if self.change_terms is None:
            return self
        actions = [term.action.value for term in self.change_terms]
        term_ids = [term.term_id for term in self.change_terms]
        if len(set(actions)) != len(actions):
            raise ValueError('change_terms must be uniquely keyed by action')
        if len(set(term_ids)) != len(term_ids):
            raise ValueError('change_terms term_id values must be unique')
        currencies = set()
        for purchase in self.purchases:
            if purchase.pricing is None:
                raise ValueError('accepted commercial-term purchases require resolved pricing')
            currencies.add(purchase.pricing.currency)
        for term in self.change_terms:
            if term.constraints is None:
                continue
            constraint = term.constraints.root
            if constraint.kind == 'budget':
                money_fields = (
                    constraint.max_delta_amount,
                    constraint.min_result_amount,
                    constraint.max_result_amount,
                )
                if any(money is not None and money.currency not in currencies for money in money_fields):
                    raise ValueError('change-term monetary constraint currency must match purchases')
                if (
                    constraint.min_result_amount is not None
                    and constraint.max_result_amount is not None
                    and constraint.min_result_amount.amount > constraint.max_result_amount.amount
                ):
                    raise ValueError('change-term minimum result exceeds maximum result')
            elif constraint.kind == 'flight':
                if (
                    constraint.earliest_result is not None
                    and constraint.latest_result is not None
                    and constraint.earliest_result > constraint.latest_result
                ):
                    raise ValueError('change-term earliest result exceeds latest result')
            elif constraint.kind == 'effective_timing' and (
                constraint.earliest_effective_at is not None
                and constraint.latest_effective_at is not None
                and constraint.earliest_effective_at > constraint.latest_effective_at
            ):
                raise ValueError('change-term earliest effective time exceeds latest time')
        return self
"""
                + "\n"
            )
            terms_path.write_text(source)
            print("  media_buy/commercial_terms.py: restored change-term set invariants")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="generated_poc tree to modify",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    """Apply all post-generation fixes."""
    global OUTPUT_DIR

    args = _parse_args(argv)
    OUTPUT_DIR = args.output_dir.resolve()
    print("Applying post-generation fixes...")

    fixes = [
        add_model_validator_to_product,
        fix_preview_render_self_reference,
        fix_brand_manifest_references,
        fix_enum_defaults,
        fix_preview_creative_request_discriminator,
        add_deprecated_field_metadata,
        apply_open_payload_config,
        fix_typed_additional_properties,
        fix_deprecated_rootmodel_fields,
        fix_constr_type_annotations,
        unwrap_rootmodel_unions,
        add_rootmodel_getattr_proxy,
        fix_list_field_shadowing,
        rewrite_response_list_to_sequence,
        fix_reuse_model_discriminator_bug,
        fix_allof_merge_field_override_conflicts,
        expose_account_reference_union_fields,
        fix_postal_union_arm_order,
        fix_postal_country_system_pairing,
        fix_adagents_duplicate_aliases,
        restore_format_category_deprecation_shim,
        restore_signal_catalog_type_alias,
        restore_format_asset_numbered_aliases,
        restore_principal_result_aliases,
        disambiguate_comply_response_arm,
        restore_flattened_contract_field_types,
        enforce_transformer_output_contract,
        restore_constructible_response_bases,
        restore_response_variant_aliases,
        fix_compliance_task_completion_response_ref,
        restore_get_products_field_compatibility_enum,
        fix_product_fields_item_reference,
        fix_audience_evidence_attestation_subject,
        fix_legacy_purchase_accepted_losses,
        preserve_request_signing_operation_strings,
        enforce_change_term_runtime_constraints,
        inject_literal_discriminator_defaults,
        widen_extension_point_lists_to_sequence,
        fix_canceled_literal_defaults,
        fix_unchanged_literal_defaults,
        fix_protocol_envelope_status_default,
        fix_trusted_match_runtime_validators,
        fix_beta3_secure_url_constraints,
        fix_beta3_package_request_constraints,
        fix_beta3_adagents_renderer_constraints,
        restore_trusted_match_compatibility_aliases,
        fix_publisher_tmpx_mapping_key_constraints,
        fix_wholesale_cache_scope_defaults,
        fix_product_publisher_property_model_coercion,
        fix_signal_listing_range_subclasses,
        fix_comply_controller_account_optional,
        fix_check_governance_status_alias,
        fix_report_plan_outcome_status_alias,
        fix_response_payload_jws_required_literals,
        fix_verify_brand_claim_models,
        fix_signal_coverage_forecast_point_types,
        fix_registry_collection_payload_status_override,
        fix_creative_manifest_standalone_asset_coercion,
        fix_update_rights_legacy_response_defaults,
        fix_list_creatives_format_reference_xor,
        rewrite_generated_enums_to_strenum,
        remove_unused_pydantic_field_imports,
        strip_extra_blank_lines_at_eof,
    ]
    for fix in fixes:
        print(f"Running {fix.__name__}...", flush=True)
        fix()

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
