#!/usr/bin/env python3
"""
Fix $ref paths in AdCP schemas to be relative file references.

The schemas use absolute URL paths like /schemas/2.4.0/core/error.json
which need to be converted to relative file paths for datamodel-codegen.
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# Load ``resolve_bundle_key`` from its source file rather than via the
# ``adcp`` package — the package's ``__init__`` eagerly imports generated
# Pydantic models, which may be mid-regen when these scripts run.
def _load_resolve_bundle_key():
    src = REPO_ROOT / "src" / "adcp" / "validation" / "version.py"
    spec = importlib.util.spec_from_file_location("_adcp_bundle_key", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_bundle_key


resolve_bundle_key = _load_resolve_bundle_key()

_VERSION_FILE = REPO_ROOT / "src" / "adcp" / "ADCP_VERSION"
_DEFAULT_BUNDLE_KEY = resolve_bundle_key(_VERSION_FILE.read_text().strip())

# Module-level handle the helpers below patch when invoked with --bundle-key.
SCHEMAS_DIR = REPO_ROOT / "schemas" / "cache" / _DEFAULT_BUNDLE_KEY

# Two upstream ref shapes:
# * Versioned (3.x):  /schemas/3.0.7/core/error.json
# * Bare       (2.5): /schemas/core/brand-manifest-ref.json
# The version segment is optional; detect via a semver-ish pattern.
_VERSION_SEGMENT_RE = re.compile(r"^\d+\.\d+")


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

    # Extract the path under /schemas/. Handle both upstream shapes:
    # * ``/schemas/3.0.7/core/error.json`` — versioned (3.x default).
    # * ``/schemas/core/brand-manifest-ref.json`` — bare (2.5 layout).
    parts = ref.split("/")
    if len(parts) >= 3:
        # parts[0] = '', parts[1] = 'schemas', parts[2] = either version
        # or first path segment. Drop the version segment when present.
        if _VERSION_SEGMENT_RE.match(parts[2]) and len(parts) >= 4:
            target_path = "/".join(parts[3:])
        else:
            target_path = "/".join(parts[2:])

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle-key",
        default=_DEFAULT_BUNDLE_KEY,
        help="Schema cache subdir to fix (default: SDK pin)",
    )
    args = parser.parse_args()

    global SCHEMAS_DIR
    SCHEMAS_DIR = REPO_ROOT / "schemas" / "cache" / args.bundle_key

    if not SCHEMAS_DIR.exists():
        print(f"Error: Schemas not found at {SCHEMAS_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"Fixing schema references in {SCHEMAS_DIR}...")

    # Find all JSON files recursively (including subdirectories)
    schema_files = list(SCHEMAS_DIR.rglob("*.json"))

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
