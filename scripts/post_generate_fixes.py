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

    Note: As of schema v1.0.0, publisher_properties uses inline object definitions
    rather than separate PublisherProperty class. This function handles both structures
    but FAILS LOUDLY if patterns don't match - we control code generation so failures
    indicate bugs we must fix, not edge cases to handle gracefully.
    """
    product_file = OUTPUT_DIR / "product.py"

    if not product_file.exists():
        # Product schema missing entirely - this is OK, schema may have removed it
        print("  product.py not found (schema may have changed)")
        return

    with open(product_file) as f:
        content = f.read()

    # Check if validators already exist
    if "validate_publisher_properties_items" in content:
        print("  product.py validators already exist")
        return

    # Check if Product class exists
    if "class Product" not in content:
        # No Product class means schema changed significantly - this is OK
        print("  product.py has no Product class (schema changed)")
        return

    # Check if publisher_properties field exists
    if "publisher_properties" not in content:
        # No publisher_properties means validation not needed - this is OK
        print("  product.py has no publisher_properties field (validation not needed)")
        return

    # At this point: Product class exists with publisher_properties field
    # We MUST add validation successfully or fail loudly

    # Add model_validator to imports
    if "model_validator" not in content:
        if "from pydantic import AwareDatetime, ConfigDict, Field, RootModel" in content:
            content = content.replace(
                "from pydantic import AwareDatetime, ConfigDict, Field, RootModel",
                "from pydantic import AwareDatetime, ConfigDict, Field, RootModel, model_validator",
            )
        else:
            raise RuntimeError(
                "Cannot add model_validator import - pydantic import pattern changed. "
                "Update post_generate_fixes.py to match new pattern."
            )

    # Check for separate PublisherProperty class (old schema structure)
    if "class PublisherProperty" in content:
        # Old structure - add validator to PublisherProperty class
        pattern = (
            r"(class PublisherProperty\(AdCPBaseModel\):.*?)\n\nclass Product\(AdCPBaseModel\):"
        )
        match = re.search(pattern, content, re.DOTALL)

        if not match:
            raise RuntimeError(
                "Found PublisherProperty class but pattern match failed. "
                "Update post_generate_fixes.py regex to match generated structure."
            )

        validator = '''

    @model_validator(mode='after')
    def validate_mutual_exclusivity(self) -> 'PublisherProperty':
        """Enforce mutual exclusivity between property_ids and property_tags."""
        from adcp.validation import validate_publisher_properties_item

        data = self.model_dump()
        validate_publisher_properties_item(data)
        return self
'''
        content = content.replace(
            match.group(0), match.group(1) + validator + "\n\nclass Product(AdCPBaseModel):"
        )

        if "validate_mutual_exclusivity" not in content:
            raise RuntimeError(
                "PublisherProperty validator injection failed (string replace unsuccessful)"
            )

        print("  product.py PublisherProperty validator added")

    # Add validator to Product class (required for both old and new structures)
    pattern = r"(class Product\(AdCPBaseModel\):.*?)(\n\n|\n    @model_validator|\Z)"
    match = re.search(pattern, content, re.DOTALL)

    if not match:
        raise RuntimeError(
            "Cannot find Product class boundaries for validator injection. "
            "Update post_generate_fixes.py regex to match generated structure."
        )

    validator = '''

    @model_validator(mode='after')
    def validate_publisher_properties_items(self) -> 'Product':
        """Validate all publisher_properties items."""
        from adcp.validation import validate_product

        data = self.model_dump()
        validate_product(data)
        return self
'''
    separator = match.group(2)
    content = content.replace(match.group(0), match.group(1) + validator + separator)

    if "validate_publisher_properties_items" not in content:
        raise RuntimeError("Product validator injection failed (string replace unsuccessful)")

    print("  product.py Product validator added")

    # Write the modified content
    with open(product_file, "w") as f:
        f.write(content)


def fix_preview_render_self_reference():
    """Fix self-referential RootModel in preview_render.py."""
    preview_file = OUTPUT_DIR / "preview_render.py"

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
    """Fix BrandManifest forward references in multiple files."""
    files_to_fix = [
        "promoted_offerings.py",
        "create_media_buy_request.py",
        "get_products_request.py",
    ]

    for filename in files_to_fix:
        file_path = OUTPUT_DIR / filename

        if not file_path.exists():
            print(f"  {filename} not found (skipping)")
            continue

        with open(file_path) as f:
            content = f.read()

        # Check if needs fixing
        needs_fix = False

        # Fix import if needed
        if "from . import brand_manifest_ref as brand_manifest_1" in content:
            content = content.replace(
                "from . import brand_manifest_ref as brand_manifest_1",
                "from . import brand_manifest as brand_manifest_1",
            )
            needs_fix = True

        # Fix BrandManifest references (should be BrandManifest1 in brand_manifest.py)
        if "brand_manifest_1.BrandManifest " in content:
            content = content.replace(
                "brand_manifest_1.BrandManifest ",
                "brand_manifest_1.BrandManifest1 ",
            )
            needs_fix = True

        if needs_fix:
            with open(file_path, "w") as f:
                f.write(content)
            print(f"  {filename} BrandManifest reference fixed")
        else:
            print(f"  {filename} already fixed or doesn't need fixing")


def fix_enum_defaults():
    """Fix enum default values in generated files.

    datamodel-code-generator sometimes creates string defaults for enum fields
    instead of enum member defaults, causing mypy errors.

    Note: brand_manifest_ref.py was a stale file and has been removed.
    The enum defaults in brand_manifest.py are already correct.
    """
    brand_manifest_file = OUTPUT_DIR / "brand_manifest.py"

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


def main():
    """Apply all post-generation fixes."""
    print("Applying post-generation fixes...")

    add_model_validator_to_product()
    fix_preview_render_self_reference()
    fix_brand_manifest_references()
    fix_enum_defaults()

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
