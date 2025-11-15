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
    """Add model_validators to PublisherProperty and Product classes."""
    product_file = OUTPUT_DIR / "product.py"

    with open(product_file) as f:
        content = f.read()

    # Check if validators already exist
    if "validate_mutual_exclusivity" in content and "validate_publisher_properties_items" in content:
        print("  product.py validators already exist")
        return

    # Add model_validator to imports if not present
    if "model_validator" not in content:
        content = content.replace(
            "from pydantic import AwareDatetime, ConfigDict, Field, RootModel",
            "from pydantic import AwareDatetime, ConfigDict, Field, RootModel, model_validator"
        )

    # Add validator to PublisherProperty class
    if "validate_mutual_exclusivity" not in content:
        # Find the PublisherProperty class - match from class definition to the next class definition
        # PublisherProperty ends right before the "class Product" line
        publisher_property_pattern = r"(class PublisherProperty\(AdCPBaseModel\):.*?)\n\nclass Product\(AdCPBaseModel\):"
        match = re.search(publisher_property_pattern, content, re.DOTALL)

        if not match:
            raise RuntimeError(
                "Could not find PublisherProperty class definition. "
                "Schema may have changed - update post_generate_fixes.py"
            )

        validator_code = '''

    @model_validator(mode='after')
    def validate_mutual_exclusivity(self) -> 'PublisherProperty':
        """Enforce mutual exclusivity between property_ids and property_tags."""
        from adcp.validation import validate_publisher_properties_item

        # Convert to dict for validation
        data = self.model_dump()
        validate_publisher_properties_item(data)
        return self
'''
        # Insert validator at end of PublisherProperty class
        content = content.replace(
            match.group(0),
            match.group(1) + validator_code + "\n\nclass Product(AdCPBaseModel):"
        )

        # Verify it was added
        if "validate_mutual_exclusivity" not in content:
            raise RuntimeError("Failed to add validate_mutual_exclusivity to PublisherProperty")

    # Add validator to Product class
    if "validate_publisher_properties_items" not in content:
        # Find the Product class and its last field definition
        # Look for the last field before either a blank line, validator, or end of file
        # This is more robust than looking for a specific field name
        product_pattern = r"(class Product\(AdCPBaseModel\):.*?)(\n\n|\n    @model_validator|\Z)"
        match = re.search(product_pattern, content, re.DOTALL)

        if not match:
            raise RuntimeError(
                "Could not find Product class definition. "
                "Schema may have changed - update post_generate_fixes.py"
            )

        validator_code = '''

    @model_validator(mode='after')
    def validate_publisher_properties_items(self) -> 'Product':
        """Validate all publisher_properties items.

        Note: Individual PublisherProperty objects already have their own
        model_validator, so this validator is mainly for consistency
        and to ensure validation happens even if items are constructed
        with model_construct (bypassing validation).
        """
        from adcp.validation import validate_product

        # Convert to dict for validation
        data = self.model_dump()
        validate_product(data)
        return self
'''
        # Insert validator before the matched separator
        separator = match.group(2)
        content = content.replace(match.group(0), match.group(1) + validator_code + separator)

        # Verify it was added
        if "validate_publisher_properties_items" not in content:
            raise RuntimeError("Failed to add validate_publisher_properties_items to Product")

    with open(product_file, "w") as f:
        f.write(content)

    print("  product.py validators added")


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
        if "from . import brand_manifest as brand_manifest_1" in content:
            # Replace with correct reference
            content = content.replace(
                "from . import brand_manifest as brand_manifest_1",
                "from . import brand_manifest_ref as brand_manifest_1"
            )

            with open(file_path, "w") as f:
                f.write(content)

            print(f"  {filename} BrandManifest reference fixed")
        else:
            print(f"  {filename} already fixed or doesn't need fixing")


def main():
    """Apply all post-generation fixes."""
    print("Applying post-generation fixes...")

    add_model_validator_to_product()
    fix_preview_render_self_reference()
    fix_brand_manifest_references()

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
