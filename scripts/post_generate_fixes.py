#!/usr/bin/env python3
"""
Post-generation fixes for generated Pydantic models.

This script applies necessary modifications to generated files that cannot be
handled by datamodel-code-generator directly:

1. Adds model_validators to types requiring mutual exclusivity checks
2. Fixes self-referential RootModel type annotations
3. Fixes BrandManifest forward references
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "src" / "adcp" / "types" / "generated_poc"


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


def fix_format_id_references():
    """Fix format_id module-qualified references in generated files.

    datamodel-code-generator imports format_id module with alias format_id_1
    but then references it with module qualification, causing Pydantic forward
    reference resolution issues. This replaces module-qualified names with
    direct imports of the FormatId1 and FormatId2 types.
    """
    # Find all files that might have format_id_1 references
    # Rather than maintain a hardcoded list, search for files with the pattern
    import glob

    files_to_fix = []
    for pattern in ["core/**/*.py", "media_buy/**/*.py", "creative/**/*.py"]:
        files_to_fix.extend(OUTPUT_DIR.glob(pattern))

    for file_path in files_to_fix:
        if not file_path.exists():
            print(f"  {file_path.name} not found (skipping)")
            continue

        with open(file_path) as f:
            content = f.read()

        # Check if already fixed
        if "format_id_1.FormatId" not in content:
            continue

        # Replace the aliased imports with direct type imports
        # Handle both relative imports: from . import ... and from ..core import ...
        content = content.replace(
            "from . import format_id as format_id_1",
            "from .format_id import FormatId1, FormatId2"
        )
        content = content.replace(
            "from ..core import format_id as format_id_1",
            "from ..core.format_id import FormatId1, FormatId2"
        )

        # Replace module-qualified references with direct type names
        content = content.replace("format_id_1.FormatId1", "FormatId1")
        content = content.replace("format_id_1.FormatId2", "FormatId2")

        with open(file_path, "w") as f:
            f.write(content)

        print(f"  {file_path.name} format_id references fixed")


def fix_format_id_union_references():
    """Fix FormatId2-only references to use FormatId1 | FormatId2 union.

    When datamodel-code-generator sees a discriminated union in format-id.json,
    it sometimes generates references to just FormatId2 instead of the full union.
    This fixes field annotations to accept both variants.
    """
    files_to_fix = []
    for pattern in ["core/**/*.py", "creative/**/*.py", "media_buy/**/*.py"]:
        files_to_fix.extend(OUTPUT_DIR.glob(pattern))

    for file_path in files_to_fix:
        if not file_path.exists():
            continue

        with open(file_path) as f:
            content = f.read()

        # Check if file has FormatId2 field annotations that should be unions
        if "FormatId2," not in content and "FormatId2]" not in content:
            continue

        original_content = content

        # Replace field type annotations: FormatId2 -> FormatId1 | FormatId2
        # Match patterns like: field: Annotated[FormatId2, ...] or field: FormatId2
        import re

        # Pattern 1: Annotated[FormatId2, Field(...)]
        content = re.sub(
            r"Annotated\[\s*FormatId2\s*,",
            "Annotated[FormatId1 | FormatId2,",
            content,
        )

        # Pattern 2: list[FormatId2]
        content = re.sub(r"list\[FormatId2\]", "list[FormatId1 | FormatId2]", content)

        if content != original_content:
            with open(file_path, "w") as f:
                f.write(content)
            print(f"  {file_path.name} FormatId union references fixed")


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
        "Field(\n            discriminator='request_type',\n            description='Request to generate previews"
    )

    with open(preview_request_file, "w") as f:
        f.write(content)

    print("  preview_creative_request.py discriminator added")


def main():
    """Apply all post-generation fixes."""
    print("Applying post-generation fixes...")

    add_model_validator_to_product()
    fix_preview_render_self_reference()
    fix_brand_manifest_references()
    fix_enum_defaults()
    fix_format_id_references()
    fix_format_id_union_references()
    fix_preview_creative_request_discriminator()

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
