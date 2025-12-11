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


def fix_mcp_webhook_payload_references():
    """Fix response type references in mcp_webhook_payload.py.

    The async-response-data.json schema references response types like
    CreateMediaBuyResponse, but the code generator creates CreateMediaBuyResponse1
    and CreateMediaBuyResponse2 (for success/error variants in oneOf schemas).

    This fix updates the type annotations to use the correct union of both variants.
    """
    webhook_file = OUTPUT_DIR / "core" / "mcp_webhook_payload.py"

    if not webhook_file.exists():
        print("  mcp_webhook_payload.py not found (skipping)")
        return

    with open(webhook_file) as f:
        content = f.read()

    # Check if already fixed
    if "CreateMediaBuyResponse1 | create_media_buy_response.CreateMediaBuyResponse2" in content:
        print("  mcp_webhook_payload.py already fixed")
        return

    # Map of incorrect references to their correct union types
    # Each response schema has oneOf with success (1) and error (2) variants
    replacements = [
        (
            "create_media_buy_response.CreateMediaBuyResponse",
            "create_media_buy_response.CreateMediaBuyResponse1 | create_media_buy_response.CreateMediaBuyResponse2",
        ),
        (
            "update_media_buy_response.UpdateMediaBuyResponse",
            "update_media_buy_response.UpdateMediaBuyResponse1 | update_media_buy_response.UpdateMediaBuyResponse2",
        ),
        (
            "sync_creatives_response.SyncCreativesResponse",
            "sync_creatives_response.SyncCreativesResponse1 | sync_creatives_response.SyncCreativesResponse2",
        ),
        (
            "get_products_response.GetProductsResponse",
            "get_products_response.GetProductsResponse",  # This one doesn't have oneOf, keep as is
        ),
    ]

    original_content = content
    for old, new in replacements:
        # Only replace if the new value is different
        if old != new:
            content = content.replace(old, new)

    if content != original_content:
        with open(webhook_file, "w") as f:
            f.write(content)
        print("  mcp_webhook_payload.py response type references fixed")
    else:
        print("  mcp_webhook_payload.py no changes needed")


def create_pricing_option_base():
    """Create PricingOptionBase class with adapter support fields.

    This class adds 'supported' and 'unsupported_reason' fields to all pricing
    options, allowing adapters to indicate whether a pricing option is supported.

    These fields are not in upstream schemas but are used by adapters.
    """
    pricing_dir = OUTPUT_DIR / "pricing_options"
    base_file = pricing_dir / "pricing_option_base.py"

    if not pricing_dir.exists():
        print("  pricing_options directory not found (skipping)")
        return

    # Create the base class file
    base_content = '''# Pricing option base class with support fields
# These fields are not in upstream schemas but are used by adapters
# to indicate whether a pricing option is supported

from __future__ import annotations

from typing import Annotated

from adcp.types.base import AdCPBaseModel
from pydantic import Field


class PricingOptionBase(AdCPBaseModel):
    """Base class for pricing options with support indicator fields.

    These fields allow adapters to indicate whether a particular pricing
    option is supported by the underlying ad platform.
    """

    supported: Annotated[
        bool | None,
        Field(
            description="Whether this pricing option is supported by the current adapter"
        ),
    ] = None
    unsupported_reason: Annotated[
        str | None,
        Field(
            description="Human-readable reason why this pricing option is not supported (only when supported=False)"
        ),
    ] = None
'''

    with open(base_file, "w") as f:
        f.write(base_content)

    print("  pricing_option_base.py created")

    # Update all pricing option files to inherit from PricingOptionBase
    pricing_option_files = [
        "cpc_option.py",
        "cpcv_option.py",
        "cpm_auction_option.py",
        "cpm_fixed_option.py",
        "cpp_option.py",
        "cpv_option.py",
        "flat_rate_option.py",
        "vcpm_auction_option.py",
        "vcpm_fixed_option.py",
    ]

    # Map of file to its main pricing option class name
    file_to_class = {
        "cpc_option.py": "CpcPricingOption",
        "cpcv_option.py": "CpcvPricingOption",
        "cpm_auction_option.py": "CpmAuctionPricingOption",
        "cpm_fixed_option.py": "CpmFixedRatePricingOption",
        "cpp_option.py": "CppPricingOption",
        "cpv_option.py": "CpvPricingOption",
        "flat_rate_option.py": "FlatRatePricingOption",
        "vcpm_auction_option.py": "VcpmAuctionPricingOption",
        "vcpm_fixed_option.py": "VcpmFixedRatePricingOption",
    }

    for filename in pricing_option_files:
        file_path = pricing_dir / filename
        if not file_path.exists():
            continue

        with open(file_path) as f:
            content = f.read()

        class_name = file_to_class[filename]

        # Check if already using PricingOptionBase
        if "PricingOptionBase" in content:
            continue

        # Add import for PricingOptionBase
        if "from .pricing_option_base import PricingOptionBase" not in content:
            # Add the import after existing imports
            # Find a good place - after the pydantic import
            if "from pydantic import" in content:
                content = content.replace(
                    "from pydantic import",
                    "from pydantic import",
                    1,
                )
                # Add the import line after the pydantic line
                lines = content.split("\n")
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if line.startswith("from pydantic import"):
                        new_lines.append("")
                        new_lines.append("from .pricing_option_base import PricingOptionBase")
                content = "\n".join(new_lines)

        # Replace AdCPBaseModel with PricingOptionBase for the main class only
        # Be careful not to replace it for nested classes like PriceGuidance, Parameters, etc.
        content = content.replace(
            f"class {class_name}(AdCPBaseModel):",
            f"class {class_name}(PricingOptionBase):",
        )

        # Remove the AdCPBaseModel import if it's no longer used
        # (but keep it if there are other classes in the file that need it)
        if "AdCPBaseModel)" not in content and "from adcp.types.base import AdCPBaseModel" in content:
            content = content.replace(
                "from adcp.types.base import AdCPBaseModel\n",
                "",
            )

        with open(file_path, "w") as f:
            f.write(content)

    print("  pricing option files updated to use PricingOptionBase")

    # Update pricing_options/__init__.py to export PricingOptionBase and all pricing options
    init_file = pricing_dir / "__init__.py"
    init_content = '''# generated by datamodel-codegen:
#   filename:  .schema_temp
#   timestamp: 2025-11-22T15:23:24+00:00

from .cpc_option import CpcPricingOption
from .cpcv_option import CpcvPricingOption
from .cpm_auction_option import CpmAuctionPricingOption
from .cpm_fixed_option import CpmFixedRatePricingOption
from .cpp_option import CppPricingOption
from .cpv_option import CpvPricingOption
from .flat_rate_option import FlatRatePricingOption
from .pricing_option_base import PricingOptionBase
from .vcpm_auction_option import VcpmAuctionPricingOption
from .vcpm_fixed_option import VcpmFixedRatePricingOption

__all__ = [
    "CpcPricingOption",
    "CpcvPricingOption",
    "CpmAuctionPricingOption",
    "CpmFixedRatePricingOption",
    "CppPricingOption",
    "CpvPricingOption",
    "FlatRatePricingOption",
    "PricingOptionBase",
    "VcpmAuctionPricingOption",
    "VcpmFixedRatePricingOption",
]
'''
    with open(init_file, "w") as f:
        f.write(init_content)

    print("  pricing_options/__init__.py updated with exports")


def main():
    """Apply all post-generation fixes."""
    print("Applying post-generation fixes...")

    add_model_validator_to_product()
    fix_preview_render_self_reference()
    fix_brand_manifest_references()
    fix_enum_defaults()
    fix_preview_creative_request_discriminator()
    fix_mcp_webhook_payload_references()
    create_pricing_option_base()

    print("\n✓ Post-generation fixes complete\n")


if __name__ == "__main__":
    main()
