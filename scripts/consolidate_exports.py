#!/usr/bin/env python3
"""
Create a consolidated export file that re-exports all types from generated_poc modules.

This script analyzes all modules in generated_poc/ and creates a single generated.py
that imports and re-exports all public types, handling naming conflicts appropriately.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

GENERATED_POC_DIR = Path(__file__).parent.parent / "src" / "adcp" / "types" / "generated_poc"
OUTPUT_FILE = Path(__file__).parent.parent / "src" / "adcp" / "types" / "_generated.py"


def extract_exports_from_module(module_path: Path) -> set[str]:
    """Extract all public class and type alias names from a Python module."""
    with open(module_path) as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return set()

    exports = set()

    # Only look at module-level nodes (not inside classes)
    for node in tree.body:
        # Class definitions
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                exports.add(node.name)
        # Module-level assignments (type aliases)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    # Only export if it looks like a type name (starts with capital)
                    if target.id and target.id[0].isupper():
                        exports.add(target.id)

    return exports


def generate_consolidated_exports() -> str:
    """Generate the consolidated exports file content."""

    # Discover all modules recursively (including subdirectories)
    # Process enums/ first so canonical enum definitions take priority over inline duplicates
    def _module_sort_key(p: Path) -> tuple[int, str]:
        rel = p.relative_to(GENERATED_POC_DIR)
        is_enum = rel.parts[0] == "enums" if len(rel.parts) > 1 else False
        return (0 if is_enum else 1, str(p))

    modules = sorted(GENERATED_POC_DIR.rglob("*.py"), key=_module_sort_key)
    modules = [m for m in modules if m.stem != "__init__" and not m.stem.startswith(".")]

    print(f"Found {len(modules)} modules to consolidate")

    # Build import statements and collect all exports
    # Track which module first defined each export name
    export_to_module: dict[str, str] = {}
    import_lines = []
    all_exports = set()
    collisions = []

    # Special handling for known collisions
    # We need BOTH versions of these types available, so import them with qualified names
    known_collisions = {
        "Package": {"package", "create_media_buy_response", "get_media_buys_response"},
        # DeliveryStatus appears in get_media_buy_delivery_response (5 values) and
        # get_media_buys_response (6 values, adds not_delivering). Export both with
        # qualified names so aliases.py can re-export the superset as the canonical one.
        "DeliveryStatus": {"get_media_buy_delivery_response", "get_media_buys_response"},
        # Note: "Catalog" also collides between core.catalog and media_buy.sync_catalogs_response.
        # We intentionally let core.catalog win (first-seen, since core/ sorts before media_buy/).
        # The response-level Catalog is imported directly in aliases.py as SyncCatalogResult.
    }

    special_imports = []
    collision_modules_seen: dict[str, set[str]] = {name: set() for name in known_collisions}

    for module_path in modules:
        # Get relative path from generated_poc directory
        rel_path = module_path.relative_to(GENERATED_POC_DIR)
        # Convert to module path (e.g., "core/error.py" -> "core.error")
        module_parts = list(rel_path.parts[:-1]) + [rel_path.stem]
        module_name = ".".join(module_parts)
        # For display, use the stem only
        display_name = rel_path.stem

        exports = extract_exports_from_module(module_path)

        if not exports:
            continue

        # Filter out names that collide with already-exported names
        unique_exports = set()
        for export_name in exports:
            # Special case: Known collisions - track all modules that define them
            if export_name in known_collisions and display_name in known_collisions[export_name]:
                collision_modules_seen[export_name].add(module_name)
                export_to_module[export_name] = module_name  # Track that we've seen it
                continue  # Don't add to unique_exports, we'll handle specially

            if export_name in export_to_module:
                first_module = export_to_module[export_name]
                # Collision detected - skip this duplicate
                collisions.append(
                    f"  {export_name}: defined in both {first_module} and {module_name} (using {first_module})"
                )
            else:
                unique_exports.add(export_name)
                export_to_module[export_name] = module_name

        if not unique_exports:
            print(f"  {display_name}: 0 unique exports (all collisions)")
            continue

        print(f"  {display_name}: {len(unique_exports)} exports")

        # Create import statement with only unique exports
        exports_str = ", ".join(sorted(unique_exports))
        import_line = f"from adcp.types.generated_poc.{module_name} import {exports_str}"
        import_lines.append(import_line)

        all_exports.update(unique_exports)

    # Generate special imports for all known collisions
    for type_name, modules_seen in collision_modules_seen.items():
        if not modules_seen:
            continue
        collisions.append(
            f"  {type_name}: defined in {sorted(modules_seen)} (all exported with qualified names)"
        )
        for module_name in sorted(modules_seen):
            # Create qualified name from module path (e.g., "core.package" -> "Package")
            parts = module_name.split(".")
            qualified_name = (
                f"_{type_name}From{parts[-1].replace('_', ' ').title().replace(' ', '')}"
            )
            special_imports.append(
                f"from adcp.types.generated_poc.{module_name} import {type_name} as {qualified_name}"
            )
            all_exports.add(qualified_name)

    if collisions:
        print("\n⚠️  Name collisions detected (duplicates skipped):")
        for collision in sorted(collisions):
            print(collision)

    # Generate file content
    generation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        '"""INTERNAL: Consolidated generated types.',
        "",
        "DO NOT import from this module directly.",
        "Use 'from adcp import Type' or 'from adcp.types.stable import Type' instead.",
        "",
        "This module consolidates all generated types from generated_poc/ into a single",
        "namespace for convenience. The leading underscore signals this is private API.",
        "",
        "Auto-generated by datamodel-code-generator from JSON schemas.",
        "DO NOT EDIT MANUALLY.",
        "",
        "Generated from: https://github.com/adcontextprotocol/adcp/tree/main/schemas",
        f"Generation date: {generation_date}",
        '"""',
        "# ruff: noqa: E501, I001",
        "from __future__ import annotations",
        "",
        "# Import all types from generated_poc modules",
    ]

    lines.extend(import_lines)

    # Add special imports for name collisions
    if special_imports:
        lines.extend(
            [
                "",
                "# Special imports for name collisions (qualified names for types defined in multiple modules)",
            ]
        )
        lines.extend(special_imports)

    # Add backward compatibility aliases (only if source exists)
    aliases = {}
    if "AdvertisingChannels" in all_exports:
        aliases["Channels"] = "AdvertisingChannels"
    # Package from get_media_buys_response is a distinct enriched view with creative approvals
    # and delivery snapshots. Export as MediaBuyPackage to avoid collision with core Package.
    if "_PackageFromGetMediaBuysResponse" in all_exports:
        aliases["MediaBuyPackage"] = "_PackageFromGetMediaBuysResponse"
    # DeliveryStatus from get_media_buys_response is a superset (adds not_delivering).
    # Export as the canonical DeliveryStatus so users can compare against all values.
    if "_DeliveryStatusFromGetMediaBuysResponse" in all_exports:
        aliases["DeliveryStatus"] = "_DeliveryStatusFromGetMediaBuysResponse"

    all_exports_with_aliases = all_exports | set(aliases.keys())

    alias_lines = []
    if aliases:
        alias_lines.extend(
            [
                "",
                "# Backward compatibility aliases for renamed types",
            ]
        )
        for alias, target in aliases.items():
            alias_lines.append(f"{alias} = {target}")

    lines.extend(alias_lines)

    # Add backwards-compat stubs for types removed from upstream schemas.
    # Kept so existing code importing them continues to work.
    # Model stubs accept any payload (extra="allow").
    # PromotedOfferingsRequirement is preserved as an Enum since it was one upstream.
    compat_lines = [
        "",
        "# Backwards-compat: Types removed from upstream schemas.",
        "from adcp.types.base import AdCPBaseModel as _AdCPBaseModel",
        "from enum import Enum as _Enum",
        "from pydantic import ConfigDict as _ConfigDict",
        "",
        "class BrandManifest(_AdCPBaseModel):",
        '    model_config = _ConfigDict(extra="allow")',
        "",
        "",
        "# PromotedOfferings and related types are superseded by Catalog with type='offering'.",
        "class PromotedOfferings(_AdCPBaseModel):",
        '    model_config = _ConfigDict(extra="allow")',
        "",
        "",
        "class PromotedOfferingsAssetRequirements(_AdCPBaseModel):",
        '    model_config = _ConfigDict(extra="allow")',
        "",
        "",
        "# Preserved as Enum (not model) so attribute access and iteration still work.",
        "class PromotedOfferingsRequirement(str, _Enum):",
        "    si_agent_url = 'si_agent_url'",
        "    offerings = 'offerings'",
        "    brand_logos = 'brand.logos'",
        "    brand_colors = 'brand.colors'",
        "    brand_tone = 'brand.tone'",
        "    brand_assets = 'brand.assets'",
        "    brand_product_catalog = 'brand.product_catalog'",
        "",
        "",
        "class PromotedProducts(_AdCPBaseModel):",
        '    model_config = _ConfigDict(extra="allow")',
        "",
        "",
        "class AssetSelectors(_AdCPBaseModel):",
        '    model_config = _ConfigDict(extra="allow")',
        "",
    ]
    lines.extend(compat_lines)
    all_exports_with_aliases = all_exports_with_aliases | {
        "AssetSelectors",
        "BrandManifest",
        "PromotedOfferings",
        "PromotedOfferingsAssetRequirements",
        "PromotedOfferingsRequirement",
        "PromotedProducts",
    }

    # Format __all__ list with proper line breaks (max 100 chars per line)
    # Exclude private names that are alias targets (internal intermediates only).
    # Private names that external modules import (e.g., _PackageFromPackage used by aliases.py)
    # must remain in __all__ so mypy allows the import.
    internal_alias_targets = {v for v in aliases.values() if v.startswith("_")}
    exports_list = sorted(
        name
        for name in all_exports_with_aliases
        if not name.startswith("_") or name not in internal_alias_targets
    )
    all_lines = ["", "# Explicit exports", "__all__ = ["]

    current_line = "    "
    for i, export in enumerate(exports_list):
        export_str = f'"{export}"'
        if i < len(exports_list) - 1:
            export_str += ","

        # Check if adding this export would exceed line length
        test_line = current_line + export_str + " "
        if len(test_line) > 100 and current_line.strip():
            # Start new line
            all_lines.append(current_line.rstrip())
            current_line = "    " + export_str + " "
        else:
            current_line += export_str + " "

    # Add last line
    if current_line.strip():
        all_lines.append(current_line.rstrip())

    all_lines.append("]")
    all_lines.append("")

    lines.extend(all_lines)

    # Add model_rebuild() calls for types with forward references
    # This resolves Pydantic forward references after all types are imported
    rebuild_lines = [
        "",
        "# Rebuild models with forward references",
        "# This must happen AFTER all imports to resolve forward reference chains",
        "",
        "# Import individual modules needed for rebuilding",
        "from adcp.types import generated_poc  # noqa: F401",
        "",
        "# Rebuild models that reference other models via forward refs",
        "CreativeManifest.model_rebuild()",
        "PreviewCreativeRequest1.model_rebuild()",
        "PreviewCreativeRequest2.model_rebuild()",
        "PreviewCreativeRequest.model_rebuild()",
        "",
    ]
    lines.extend(rebuild_lines)

    return "\n".join(lines)


def main():
    """Generate consolidated exports file."""
    print("Generating consolidated exports from generated_poc modules...")

    if not GENERATED_POC_DIR.exists():
        print(f"Error: {GENERATED_POC_DIR} does not exist")
        return 1

    content = generate_consolidated_exports()

    print(f"\nWriting {OUTPUT_FILE}...")
    OUTPUT_FILE.write_text(content)

    # Run black to format the generated file.
    # Try uv run first (works in the project virtualenv), then fall back to sys.executable.
    print("Formatting with black...")
    black_commands = [
        ["uv", "run", "black", str(OUTPUT_FILE), "--quiet"],
        [sys.executable, "-m", "black", str(OUTPUT_FILE), "--quiet"],
    ]
    formatted = False
    for cmd in black_commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                print("✓ Formatted with black")
                formatted = True
                break
        except FileNotFoundError:
            continue
    if not formatted:
        print("⚠ Could not format with black (not installed)")

    print("✓ Successfully generated consolidated exports")
    export_count = len(
        [
            name
            for name in content.split("__all__ = [")[1].split("]")[0].strip("[]").split(",")
            if name.strip()
        ]
    )
    print(f"  Total exports: {export_count}")

    return 0


if __name__ == "__main__":
    exit(main())
