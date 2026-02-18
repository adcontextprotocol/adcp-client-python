#!/usr/bin/env python3
"""
Post-generation fixes for generated Pydantic models.

This script applies necessary modifications to generated files that cannot be
handled by datamodel-code-generator directly:

1. Adds model_validators to types requiring mutual exclusivity checks
2. Fixes self-referential RootModel type annotations
3. Fixes BrandManifest forward references
4. Adds deprecated=True to fields marked deprecated in JSON schema
"""

from __future__ import annotations

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

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
