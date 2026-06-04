#!/usr/bin/env python3
# ruff: noqa: E501
"""
Post-generation fixes for generated Pydantic models.

This script applies necessary modifications to generated files that cannot be
handled by datamodel-code-generator directly:

1. Adds model_validators to types requiring mutual exclusivity checks
2. Fixes self-referential RootModel type annotations
3. Fixes BrandManifest forward references
4. Adds deprecated=True to fields marked deprecated in JSON schema
5. Unwraps specified RootModel unions to plain Union type aliases (#155)
6. Widens canceled: Literal[True] = True on request types to | None = None (#641)
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

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
    match = re.search(r"^class ([A-Za-z_]\w*)\b", content, re.MULTILINE)
    return match.group(1) if match else None


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
    else:
        new_body = "    model_config = ConfigDict(\n        extra='allow',\n    )\n" + body

    return (
        content[: match.start()] + header + new_body + content[match.end() :],
        "updated",
    )


def _ensure_configdict_import(content: str) -> str:
    if "ConfigDict" not in content or "from pydantic import" not in content:
        return content
    if re.search(r"^from pydantic import .*ConfigDict", content, re.MULTILINE):
        return content
    return re.sub(
        r"^from pydantic import ([^\n]+)$",
        lambda m: (
            "from pydantic import "
            + ", ".join(sorted({*[part.strip() for part in m.group(1).split(",")], "ConfigDict"}))
        ),
        content,
        count=1,
        flags=re.MULTILINE,
    )


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
    """Replace constr(pattern=...) with Annotated[str, StringConstraints(pattern=...)] in generated files.

    datamodel-code-generator uses constr(pattern=...) as dict key types, but mypy's
    Pydantic v2 plugin rejects this form. The correct form is Annotated[str, StringConstraints(...)].
    """
    fixed_count = 0

    for py_file in OUTPUT_DIR.rglob("*.py"):
        with open(py_file) as f:
            content = f.read()

        if "constr(pattern=" not in content:
            continue

        original = content

        # Replace constr(pattern=r'...') with Annotated[str, StringConstraints(pattern=r'...')]
        content = re.sub(
            r"constr\(pattern=(r'[^']*')\)",
            r"Annotated[str, StringConstraints(pattern=\1)]",
            content,
        )

        # Replace 'constr' in imports with 'StringConstraints'
        content = re.sub(r"\bconstr\b", "StringConstraints", content)

        if content != original:
            with open(py_file, "w") as f:
                f.write(content)
            fixed_count += 1

    if fixed_count > 0:
        print(f"  Replaced constr(pattern=...) with StringConstraints in {fixed_count} file(s)")
    else:
        print("  No constr(pattern=...) annotations needed fixing")


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
            f"  ✓ Widened {total_widened} extension-point field(s) "
            f"across {files_touched} file(s)"
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

    # Otherwise insert after the typing imports block. Codegen always emits
    # ``from typing import Annotated`` near the top, so anchor on it.
    typing_pattern = re.compile(r"^from typing import [^\n]+$", re.MULTILINE)
    match = typing_pattern.search(content)
    if match is not None:
        return (
            content[: match.end()]
            + "\nfrom collections.abc import Sequence"
            + content[match.end() :]
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
            if isinstance(stmt.value, ast.Constant) and stmt.value.value == "repeatable_group":
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


def restore_response_variant_aliases() -> None:
    """Restore public numbered response arms for beta 3 envelope-only responses.

    Beta 3 moved many response schemas to a common protocol envelope shape.
    The SDK has long exposed numbered success/error/submitted arm classes, and
    helpers/tests/adopters construct those classes directly. Reintroduce thin,
    extra-allowing compatibility arms and make the public response name a union
    when the generator no longer emits arms.
    """

    common_header = """


# Backward-compatible SDK response arms. Upstream beta 3 schemas collapse this
# task response to the common protocol envelope, but the Python SDK keeps the
# historical numbered variants as ergonomic construction/parsing aliases.
from typing import Any, Literal, TypeAlias

from pydantic import ConfigDict

from ..core import error as error_1
"""

    media_header = """


# Backward-compatible SDK response arms. Upstream beta 3 schemas collapse this
# task response to the common protocol envelope, but the Python SDK keeps the
# historical numbered variants as ergonomic construction/parsing aliases.
from typing import Any, Literal, TypeAlias

from pydantic import AwareDatetime, ConfigDict, model_validator

from adcp.types.media_buy_status_helpers import MEDIA_BUY_LEGACY_STATUS_VALUES, unwrap_enum_value

from ..core import error as error_1
from ..core import ext as ext_1
from ..core import package as package_1
from ..core.protocol_envelope import ProtocolEnvelope
from ..enums import media_buy_status as media_buy_status_1
from ..enums import task_status as task_status_1
"""
    update_media_header = media_header.replace(
        "from typing import Any, Literal, TypeAlias",
        "from collections.abc import Sequence\nfrom typing import Any, Literal, TypeAlias",
    )

    simple_error_arms: dict[str, tuple[str, str, str]] = {
        "media_buy/build_creative_response.py": (
            "BuildCreativeResponse",
            "BuildCreativeResponse1",
            "BuildCreativeResponse2",
        ),
        "media_buy/provide_performance_feedback_response.py": (
            "ProvidePerformanceFeedbackResponse",
            "ProvidePerformanceFeedbackResponse1",
            "ProvidePerformanceFeedbackResponse2",
        ),
        "account/sync_accounts_response.py": (
            "SyncAccountsResponse",
            "SyncAccountsResponse1",
            "SyncAccountsResponse2",
        ),
        "media_buy/log_event_response.py": (
            "LogEventResponse",
            "LogEventResponse1",
            "LogEventResponse2",
        ),
        "media_buy/sync_event_sources_response.py": (
            "SyncEventSourcesResponse",
            "SyncEventSourcesResponse1",
            "SyncEventSourcesResponse2",
        ),
        "media_buy/sync_audiences_response.py": (
            "SyncAudiencesResponse",
            "SyncAudiencesResponse1",
            "SyncAudiencesResponse2",
        ),
        "account/get_account_financials_response.py": (
            "GetAccountFinancialsResponse",
            "GetAccountFinancialsResponse1",
            "GetAccountFinancialsResponse2",
        ),
        "content_standards/calibrate_content_response.py": (
            "CalibrateContentResponse",
            "CalibrateContentResponse1",
            "CalibrateContentResponse2",
        ),
        "content_standards/validate_content_delivery_response.py": (
            "ValidateContentDeliveryResponse",
            "ValidateContentDeliveryResponse1",
            "ValidateContentDeliveryResponse2",
        ),
        "brand/get_rights_response.py": (
            "GetRightsResponse",
            "GetRightsResponse1",
            "GetRightsResponse2",
        ),
    }

    fixed = 0

    def _remove_original_response_class(source: str, base: str) -> str:
        """Remove the generator's envelope-only class before restoring a union alias."""
        source = re.sub(
            rf"\n\nclass {re.escape(base)}\(AdcpVersionEnvelope, ProtocolEnvelope\):\n    pass\n",
            "\n",
            source,
        )
        return _sync_protocol_envelope_import(source)

    def _normalize_existing_arms(target: Path, base: str) -> None:
        """Keep compatibility arms payload-shaped and expose final names as aliases."""
        original = target.read_text()
        source = _remove_original_response_class(original, base)
        new_source = re.sub(
            r"class ([A-Za-z]+Response[12])\(AdcpVersionEnvelope, ProtocolEnvelope\):",
            r"class \1(AdcpVersionEnvelope):",
            source,
        )
        new_source = new_source.replace(
            "from typing import Any, Literal\n",
            "from typing import Any, Literal, TypeAlias\n",
        )
        new_source = re.sub(
            rf"\n{re.escape(base)} = ",
            f"\n{base}: TypeAlias = ",
            new_source,
        )
        new_source = _sync_protocol_envelope_import(new_source)
        if new_source != original:
            target.write_text(new_source)

    def _write_if_needed(relative: str, base: str, marker: str, snippet: str) -> None:
        nonlocal fixed
        target = OUTPUT_DIR / relative
        if not target.exists():
            print(f"  {relative} not found (skipping response arms)")
            return
        source = target.read_text()
        if marker in source:
            _normalize_existing_arms(target, base)
            print(f"  {relative}: response arms already restored")
            return
        source = _remove_original_response_class(source, base)
        target.write_text(source.rstrip() + snippet.rstrip() + "\n")
        _normalize_existing_arms(target, base)
        fixed += 1

    _write_if_needed(
        "signals/activate_signal_response.py",
        "ActivateSignalResponse",
        "class ActivateSignalResponse1",
        """


# Backward-compatible SDK response arms. Upstream beta 3 schemas collapse this
# task response to the common protocol envelope, but the Python SDK keeps the
# historical numbered variants as ergonomic construction/parsing aliases.
from typing import Literal, TypeAlias

from pydantic import ConfigDict

from ..core import deployment as deployment_1
from ..core import error as error_1


class ActivateSignalResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    deployments: list[deployment_1.Deployment]
    sandbox: bool | None = None


class ActivateSignalResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


ActivateSignalResponse: TypeAlias = ActivateSignalResponse1 | ActivateSignalResponse2
""",
    )

    restored_payload_arms: list[tuple[str, str, str, str]] = [
        (
            "brand/acquire_rights_response.py",
            "AcquireRightsResponse",
            "class AcquireRightsResponse1",
            """


from typing import Any, Literal, TypeAlias

from pydantic import ConfigDict

from ..core import error as error_1


class AcquireRightsResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    rights_id: str
    brand_id: str
    terms: Any
    generation_credentials: list[Any]
    rights_constraint: Any
    rights_status: Literal['acquired'] | None = None
    status: Literal['acquired'] | None = None


class AcquireRightsResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    rights_id: str
    brand_id: str
    rights_status: Literal['pending_approval'] | None = None
    status: Literal['pending_approval'] | None = None
    detail: str | None = None
    estimated_response_time: str | None = None


class AcquireRightsResponse3(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    rights_id: str
    brand_id: str
    reason: str
    rights_status: Literal['rejected'] | None = None
    status: Literal['rejected'] | None = None
    suggestions: list[str] | None = None


class AcquireRightsResponse4(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


AcquireRightsResponse: TypeAlias = (
    AcquireRightsResponse1
    | AcquireRightsResponse2
    | AcquireRightsResponse3
    | AcquireRightsResponse4
)
""",
        ),
        (
            "content_standards/get_content_standards_response.py",
            "GetContentStandardsResponse",
            "class GetContentStandardsResponse1",
            """


from typing import TypeAlias

from pydantic import ConfigDict

from ..core import error as error_1


class GetContentStandardsResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')


class GetContentStandardsResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


GetContentStandardsResponse: TypeAlias = (
    GetContentStandardsResponse1 | GetContentStandardsResponse2
)
""",
        ),
        (
            "brand/get_brand_identity_response.py",
            "GetBrandIdentityResponse",
            "class GetBrandIdentityResponse1",
            """


from typing import TypeAlias

from pydantic import ConfigDict

from ..core import error as error_1


class GetBrandIdentityResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    brand_id: str
    house: str
    names: dict[str, Any]
    description: str | None = None
    tagline: str | None = None
    industries: list[str] | None = None
    tone: Any = None
    visual_guidelines: Any = None
    colors: Any = None
    logos: Any = None
    fonts: Any = None
    assets: Any = None
    rights: Any = None
    voice_synthesis: Any = None
    keller_type: Any = None
    available_fields: list[str] | None = None


class GetBrandIdentityResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


GetBrandIdentityResponse: TypeAlias = GetBrandIdentityResponse1 | GetBrandIdentityResponse2
""",
        ),
        (
            "creative/get_creative_features_response.py",
            "GetCreativeFeaturesResponse",
            "class GetCreativeFeaturesResponse1",
            """


from typing import TypeAlias

from pydantic import ConfigDict

from ..core import creative_consumption as creative_consumption_1
from ..core import error as error_1
from . import creative_feature_result as creative_feature_result_1


class GetCreativeFeaturesResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    results: list[creative_feature_result_1.CreativeFeatureResult]
    detail_url: str | None = None
    pricing_option_id: str | None = None
    vendor_cost: float | None = None
    currency: str | None = None
    consumption: creative_consumption_1.CreativeConsumption | None = None


class GetCreativeFeaturesResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


GetCreativeFeaturesResponse: TypeAlias = (
    GetCreativeFeaturesResponse1 | GetCreativeFeaturesResponse2
)
""",
        ),
        (
            "content_standards/get_media_buy_artifacts_response.py",
            "GetMediaBuyArtifactsResponse",
            "class GetMediaBuyArtifactsResponse1",
            """


from typing import Any, TypeAlias

from pydantic import ConfigDict

from ..core import error as error_1
from . import artifact as artifact_1


class ArtifactRecord(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    record_id: str
    artifact: artifact_1.Artifact
    timestamp: str | None = None
    package_id: str | None = None
    country: str | None = None
    channel: str | None = None
    brand_context: dict[str, Any] | None = None
    local_verdict: str | None = None


class GetMediaBuyArtifactsResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    media_buy_id: str
    artifacts: list[ArtifactRecord]
    collection_info: dict[str, Any] | None = None
    pagination: Any = None


class GetMediaBuyArtifactsResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


GetMediaBuyArtifactsResponse: TypeAlias = (
    GetMediaBuyArtifactsResponse1 | GetMediaBuyArtifactsResponse2
)
""",
        ),
    ]

    for relative, base, marker, snippet in restored_payload_arms:
        _write_if_needed(relative, base, marker, snippet)

    for relative, (base, success, error) in simple_error_arms.items():
        snippet = (
            common_header
            + f"""

class {success}(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    status: Any = None


class {error}(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


{base}: TypeAlias = {success} | {error}
"""
        )
        _write_if_needed(relative, base, f"class {success}", snippet)

    _write_if_needed(
        "media_buy/create_media_buy_response.py",
        "CreateMediaBuyResponse",
        "class CreateMediaBuyResponse1",
        media_header
        + """
class CreateMediaBuyResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    media_buy_id: str
    packages: list[package_1.Package]
    buyer_ref: str | None = None
    confirmed_at: AwareDatetime | None
    revision: int
    media_buy_status: media_buy_status_1.MediaBuyStatus | None = None
    status: Literal["completed"]

    @model_validator(mode='before')
    @classmethod
    def _normalize_legacy_status(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_status = unwrap_enum_value(data.get("status"))
        media_buy_status = unwrap_enum_value(data.get("media_buy_status"))
        if raw_status is None:
            data = dict(data)
            data["status"] = "completed"
        elif raw_status == "completed":
            data = dict(data)
            data["status"] = "completed"
        elif media_buy_status is None and raw_status in MEDIA_BUY_LEGACY_STATUS_VALUES:
            data = dict(data)
            data["media_buy_status"] = raw_status
            data["status"] = "completed"
        elif media_buy_status is not None and raw_status == media_buy_status:
            data = dict(data)
            data["status"] = "completed"
        return data


class CreateMediaBuyResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


class CreateMediaBuyResponse3(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(extra='allow', use_enum_values=True, validate_default=True)
    status: Literal[task_status_1.TaskStatus.submitted] = task_status_1.TaskStatus.submitted
    task_id: str
    errors: list[error_1.Error] | None = None
    ext: ext_1.ExtensionObject | None = None

    @model_validator(mode='before')
    @classmethod
    def _normalize_submitted_status(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("status") == "submitted":
            data = dict(data)
            data["status"] = task_status_1.TaskStatus.submitted
        return data


CreateMediaBuyResponse: TypeAlias = (
    CreateMediaBuyResponse1 | CreateMediaBuyResponse2 | CreateMediaBuyResponse3
)

__all__ = [
    "CreateMediaBuyResponse",
    "CreateMediaBuyResponse1",
    "CreateMediaBuyResponse2",
    "CreateMediaBuyResponse3",
]
""",
    )

    _write_if_needed(
        "media_buy/update_media_buy_response.py",
        "UpdateMediaBuyResponse",
        "class UpdateMediaBuyResponse1",
        update_media_header
        + """

class UpdateMediaBuyResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    media_buy_id: str
    affected_packages: Sequence[package_1.Package] | None = None
    packages: list[package_1.Package] | None = None
    buyer_ref: str | None = None
    revision: int
    media_buy_status: media_buy_status_1.MediaBuyStatus | None = None
    status: Literal["completed"]

    @model_validator(mode='before')
    @classmethod
    def _normalize_legacy_status(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_status = unwrap_enum_value(data.get("status"))
        media_buy_status = unwrap_enum_value(data.get("media_buy_status"))
        if raw_status is None:
            data = dict(data)
            data["status"] = "completed"
        elif raw_status == "completed":
            data = dict(data)
            data["status"] = "completed"
        elif media_buy_status is None and raw_status in MEDIA_BUY_LEGACY_STATUS_VALUES:
            data = dict(data)
            data["media_buy_status"] = raw_status
            data["status"] = "completed"
        elif media_buy_status is not None and raw_status == media_buy_status:
            data = dict(data)
            data["status"] = "completed"
        return data


class UpdateMediaBuyResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


class UpdateMediaBuyResponse3(AdcpVersionEnvelope, ProtocolEnvelope):
    model_config = ConfigDict(extra='allow', use_enum_values=True, validate_default=True)
    status: Literal[task_status_1.TaskStatus.submitted] = task_status_1.TaskStatus.submitted
    task_id: str
    errors: list[error_1.Error] | None = None
    ext: ext_1.ExtensionObject | None = None

    @model_validator(mode='before')
    @classmethod
    def _normalize_submitted_status(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("status") == "submitted":
            data = dict(data)
            data["status"] = task_status_1.TaskStatus.submitted
        return data


UpdateMediaBuyResponse: TypeAlias = (
    UpdateMediaBuyResponse1 | UpdateMediaBuyResponse2 | UpdateMediaBuyResponse3
)

__all__ = [
    "UpdateMediaBuyResponse",
    "UpdateMediaBuyResponse1",
    "UpdateMediaBuyResponse2",
    "UpdateMediaBuyResponse3",
]
""",
    )

    _write_if_needed(
        "creative/preview_creative_response.py",
        "PreviewCreativeResponse",
        "class PreviewCreativeResponse1",
        common_header
        + """
from . import preview_render as preview_render_1


class PreviewInput(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    name: str | None = None


class Preview(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    renders: list[preview_render_1.PreviewRender]
    preview_id: str | None = None
    input: PreviewInput | None = None


class PreviewCreativeResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    response_type: Literal['single'] | None = None
    previews: list[Preview]
    expires_at: Any = None


class PreviewCreativeResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    response_type: Literal['batch'] | None = None
    results: list[Any]


class PreviewCreativeResponse3(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    response_type: Literal['variant'] | None = None
    variant_id: str | None = None
    rendered: list[Any] | None = None


PreviewCreativeResponse: TypeAlias = (
    PreviewCreativeResponse1 | PreviewCreativeResponse2 | PreviewCreativeResponse3
)
""",
    )

    _write_if_needed(
        "media_buy/sync_catalogs_response.py",
        "SyncCatalogsResponse",
        "class SyncCatalogsResponse1",
        common_header
        + """
from ..enums import catalog_action


class Catalog(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    catalog_id: str
    action: catalog_action.CatalogAction
    item_count: int | None = None
    items_pending: int | None = None
    errors: list[error_1.Error] | None = None


class SyncCatalogsResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    catalogs: list[Catalog]
    status: Any = None


class SyncCatalogsResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


SyncCatalogsResponse: TypeAlias = SyncCatalogsResponse1 | SyncCatalogsResponse2
""",
    )

    _write_if_needed(
        "creative/sync_creatives_response.py",
        "SyncCreativesResponse",
        "class Creative(",
        common_header
        + """
from ..enums import creative_action


class Creative(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    creative_id: str
    action: creative_action.CreativeAction | str
    errors: list[error_1.Error] | None = None


class SyncCreativesResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    creatives: list[Creative]
    status: Any = None


class SyncCreativesResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


SyncCreativesResponse: TypeAlias = SyncCreativesResponse1 | SyncCreativesResponse2
""",
    )

    _write_if_needed(
        "brand/update_rights_response.py",
        "UpdateRightsResponse",
        "class UpdateRightsResponse1",
        common_header
        + """

class UpdateRightsResponse1(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    rights_id: str
    terms: dict[str, Any] | None = None


class UpdateRightsResponse2(AdcpVersionEnvelope):
    model_config = ConfigDict(extra='allow')
    errors: list[error_1.Error]


UpdateRightsResponse: TypeAlias = UpdateRightsResponse1 | UpdateRightsResponse2
""",
    )

    if fixed:
        print(f"  Restored response variant arms in {fixed} module(s)")
    else:
        print("  Response variant arms already restored")


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


def main():
    """Apply all post-generation fixes."""
    print("Applying post-generation fixes...")

    fixes = [
        add_model_validator_to_product,
        fix_preview_render_self_reference,
        fix_brand_manifest_references,
        fix_enum_defaults,
        fix_preview_creative_request_discriminator,
        add_deprecated_field_metadata,
        apply_open_payload_config,
        fix_deprecated_rootmodel_fields,
        fix_constr_type_annotations,
        unwrap_rootmodel_unions,
        add_rootmodel_getattr_proxy,
        fix_list_field_shadowing,
        rewrite_response_list_to_sequence,
        fix_reuse_model_discriminator_bug,
        restore_format_category_deprecation_shim,
        inject_literal_discriminator_defaults,
        widen_extension_point_lists_to_sequence,
        fix_canceled_literal_defaults,
        fix_unchanged_literal_defaults,
        fix_protocol_envelope_status_default,
        fix_wholesale_cache_scope_defaults,
        fix_product_publisher_property_model_coercion,
        fix_mcp_webhook_operation_id_optional,
        fix_signal_listing_range_subclasses,
        restore_signal_catalog_type_alias,
        restore_format_asset_numbered_aliases,
        restore_response_variant_aliases,
        fix_comply_controller_account_optional,
        fix_check_governance_status_alias,
        fix_report_plan_outcome_status_alias,
        fix_response_payload_jws_required_literals,
        fix_verify_brand_claim_models,
        fix_signal_coverage_forecast_point_types,
        strip_extra_blank_lines_at_eof,
    ]
    for fix in fixes:
        print(f"Running {fix.__name__}...", flush=True)
        fix()

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
