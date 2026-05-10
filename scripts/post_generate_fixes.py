#!/usr/bin/env python3
"""
Post-generation fixes for generated Pydantic models.

This script applies necessary modifications to generated files that cannot be
handled by datamodel-code-generator directly:

1. Adds model_validators to types requiring mutual exclusivity checks
2. Fixes self-referential RootModel type annotations
3. Fixes BrandManifest forward references
4. Adds deprecated=True to fields marked deprecated in JSON schema
5. Unwraps specified RootModel unions to plain Union type aliases (#155)
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "src" / "adcp" / "types" / "generated_poc"
SCHEMA_DIR = REPO_ROOT / "schemas" / "cache"


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
        return

    with open(brand_manifest_file) as f:
        content = f.read()

    # Check if already fixed (using enum member, not string)
    if "FeedFormat.google_merchant_center" in content:
        print("  brand_manifest.py enum defaults already correct")
        return

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
            # Check if already has deprecated=True for this field
            field_start = content.find(f"{field_name}:")
            if field_start == -1:
                continue

            # Find the Field( after this field definition
            field_section = content[field_start : field_start + 500]
            if "deprecated=True" in field_section.split("] = ")[0]:
                continue  # Already fixed

            # Pattern to find Field( and add deprecated=True after it
            # Use DOTALL to match across newlines
            pattern = rf"({field_name}:\s*Annotated\[[\s\S]*?Field\(\s*\n?\s*)"
            match = re.search(pattern, content)

            if match:
                # Insert deprecated=True after Field(
                insert_pos = match.end()
                # Check what comes after - if it's description=, add before it
                after_match = content[insert_pos : insert_pos + 50]
                if after_match.strip().startswith("description="):
                    new_content = (
                        content[:insert_pos]
                        + "deprecated=True,\n            "
                        + content[insert_pos:]
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

        # Find RootModel union classes using AST
        tree = ast.parse(source)
        lines = source.split("\n")
        insertions: list[tuple[int, str]] = []  # (line_index, class_name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.end_lineno:
                continue
            for base in node.bases:
                base_src = ast.get_source_segment(source, base)
                if base_src and "RootModel[" in base_src and "|" in base_src:
                    insertions.append((node.end_lineno, node.name))
                    break

        if not insertions:
            continue

        # Insert __getattr__ methods (reverse order to preserve line numbers)
        method_lines = [
            "",
            "    def __getattr__(self, name: str) -> Any:",
            '        """Proxy attribute access to the wrapped type."""',
            "        if name.startswith('_'):",
            "            raise AttributeError(name)",
            "        return getattr(self.root, name)",
        ]

        for end_lineno, class_name in sorted(insertions, reverse=True):
            for i, method_line in enumerate(method_lines):
                lines.insert(end_lineno + i, method_line)

        source = "\n".join(lines)
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
    """Strip bogus ``source: Literal['reuse']`` subclasses.

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
        r"\n\s*class (\w+)\((\w+)\):\n\s*source: Literal\['reuse'\]\n",
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


def main():
    """Apply all post-generation fixes."""
    print("Applying post-generation fixes...")

    add_model_validator_to_product()
    fix_preview_render_self_reference()
    fix_brand_manifest_references()
    fix_enum_defaults()
    fix_preview_creative_request_discriminator()
    add_deprecated_field_metadata()
    fix_constr_type_annotations()
    unwrap_rootmodel_unions()
    add_rootmodel_getattr_proxy()
    fix_list_field_shadowing()
    rewrite_response_list_to_sequence()
    fix_reuse_model_discriminator_bug()
    restore_format_category_deprecation_shim()
    inject_literal_discriminator_defaults()

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
