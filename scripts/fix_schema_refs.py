#!/usr/bin/env python3
"""
Fix $ref paths in AdCP schemas to be relative file references.

The schemas use absolute URL paths like /schemas/2.4.0/core/error.json
which need to be converted to relative file paths for datamodel-codegen.
"""

import json
import sys
from pathlib import Path

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas" / "cache"


def convert_ref_to_relative(ref: str, current_file: Path) -> str:
    """
    Convert absolute $ref to relative path from current file.

    Examples:
        From: /schemas/2.4.0/core/error.json
        Current: schemas/cache/media-buy/get-products-request.json
        To: ../core/error.json
    """
    if not ref.startswith("/schemas/"):
        return ref  # Already relative or not a schema ref

    # Extract path after /schemas/VERSION/
    # e.g., /schemas/2.4.0/core/error.json -> core/error.json
    parts = ref.split("/")
    if len(parts) >= 4:
        # Skip first 3 parts: '', 'schemas', 'VERSION'
        target_path = "/".join(parts[3:])

        # Calculate relative path from current file to target
        current_dir = current_file.parent
        target_file = SCHEMAS_DIR / target_path

        try:
            rel_path = target_file.relative_to(current_dir)
            return str(rel_path)
        except ValueError:
            # If relative_to fails, calculate using common parent
            common = SCHEMAS_DIR
            current_depth = len(current_dir.relative_to(common).parts)
            up_dirs = "../" * current_depth
            return up_dirs + target_path

    return ref


def fix_refs(obj, current_file: Path):
    """Recursively fix $ref paths in schema."""
    if isinstance(obj, dict):
        # Remove $id field as it causes issues with relative path resolution
        # datamodel-code-generator tries to resolve relative $refs from the $id path
        if "$id" in obj:
            del obj["$id"]

        if "$ref" in obj:
            ref = obj["$ref"]
            obj["$ref"] = convert_ref_to_relative(ref, current_file)
        for value in obj.values():
            fix_refs(value, current_file)
    elif isinstance(obj, list):
        for item in obj:
            fix_refs(item, current_file)


def main():
    """Fix all schema references."""
    if not SCHEMAS_DIR.exists():
        print("Error: Schemas not found", file=sys.stderr)
        sys.exit(1)

    print(f"Fixing schema references in {SCHEMAS_DIR}...")

    # Find all JSON files recursively (including subdirectories)
    schema_files = list(SCHEMAS_DIR.rglob("*.json"))
    # Filter out .hashes.json
    schema_files = [f for f in schema_files if f.name != ".hashes.json"]

    print(f"Found {len(schema_files)} schemas\n")

    for schema_file in schema_files:
        with open(schema_file) as f:
            schema = json.load(f)

        fix_refs(schema, schema_file)

        with open(schema_file, "w") as f:
            json.dump(schema, f, indent=2)

        rel_path = schema_file.relative_to(SCHEMAS_DIR)
        print(f"  ✓ {rel_path}")

    print(f"\n✓ Fixed {len(schema_files)} schemas")


if __name__ == "__main__":
    main()
