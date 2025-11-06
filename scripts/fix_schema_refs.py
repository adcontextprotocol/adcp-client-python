#!/usr/bin/env python3
"""
Fix $ref paths in AdCP schemas to be relative file references.

The schemas use absolute URL paths like /schemas/v1/core/error.json
which need to be converted to relative file paths for datamodel-codegen.
"""

import json
import sys
from pathlib import Path

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas" / "cache" / "latest"


def extract_filename_from_ref(ref: str) -> str:
    """Extract just the filename from a ref path."""
    # /schemas/v1/core/error.json -> error.json
    # /schemas/v1/media-buy/get-products-request.json -> get-products-request.json
    return ref.split("/")[-1]


def fix_refs(obj):
    """Recursively fix $ref paths in schema."""
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            if ref.startswith("/schemas/v1/"):
                # Convert to just filename since all schemas are in one directory
                obj["$ref"] = extract_filename_from_ref(ref)
        for value in obj.values():
            fix_refs(value)
    elif isinstance(obj, list):
        for item in obj:
            fix_refs(item)


def main():
    """Fix all schema references."""
    if not SCHEMAS_DIR.exists():
        print("Error: Schemas not found", file=sys.stderr)
        sys.exit(1)

    print(f"Fixing schema references in {SCHEMAS_DIR}...")

    schema_files = list(SCHEMAS_DIR.glob("*.json"))
    print(f"Found {len(schema_files)} schemas\n")

    for schema_file in schema_files:
        with open(schema_file) as f:
            schema = json.load(f)

        fix_refs(schema)

        with open(schema_file, "w") as f:
            json.dump(schema, f, indent=2)

        print(f"  ✓ {schema_file.name}")

    print(f"\n✓ Fixed {len(schema_files)} schemas")


if __name__ == "__main__":
    main()
