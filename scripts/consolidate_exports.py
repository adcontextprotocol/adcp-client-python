#!/usr/bin/env python3
"""
Create a consolidated export file that re-exports all types from generated_poc modules.

This script analyzes all modules in generated_poc/ and creates a single generated.py
that imports and re-exports all public types, handling naming conflicts appropriately.
"""

from __future__ import annotations

import ast
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
    # Discover all modules
    modules = sorted(GENERATED_POC_DIR.glob("*.py"))
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
    KNOWN_COLLISIONS = {
        "Package": {"package", "create_media_buy_response"},
    }

    special_imports = []
    collision_modules_seen: dict[str, set[str]] = {name: set() for name in KNOWN_COLLISIONS}

    for module_path in modules:
        module_name = module_path.stem
        exports = extract_exports_from_module(module_path)

        if not exports:
            continue

        # Filter out names that collide with already-exported names
        unique_exports = set()
        for export_name in exports:
            # Special case: Known collisions - track all modules that define them
            if export_name in KNOWN_COLLISIONS and module_name in KNOWN_COLLISIONS[export_name]:
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
            print(f"  {module_name}: 0 unique exports (all collisions)")
            continue

        print(f"  {module_name}: {len(unique_exports)} exports")

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
            qualified_name = f"_{type_name}From{module_name.replace('_', ' ').title().replace(' ', '')}"
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
        lines.extend(["", "# Special imports for name collisions (qualified names for types defined in multiple modules)"])
        lines.extend(special_imports)

    # Add backward compatibility aliases (only if source exists)
    aliases = {}
    if "AdvertisingChannels" in all_exports:
        aliases["Channels"] = "AdvertisingChannels"

    all_exports_with_aliases = all_exports | set(aliases.keys())

    alias_lines = []
    if aliases:
        alias_lines.extend([
            "",
            "# Backward compatibility aliases for renamed types",
        ])
        for alias, target in aliases.items():
            alias_lines.append(f"{alias} = {target}")

    lines.extend(alias_lines)

    # Format __all__ list with proper line breaks (max 100 chars per line)
    exports_list = sorted(list(all_exports_with_aliases))
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
