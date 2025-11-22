#!/usr/bin/env python3
"""
Generate Python types from AdCP JSON schemas using datamodel-code-generator.

This script processes schemas from the organized subdirectory structure and
generates Pydantic v2 models with discriminated union support.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Paths
REPO_ROOT = Path(__file__).parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas" / "cache"
OUTPUT_DIR = REPO_ROOT / "src" / "adcp" / "types" / "generated_poc"
TEMP_DIR = REPO_ROOT / ".schema_temp"


def rewrite_refs(obj: dict | list | str) -> dict | list | str:
    """
    Recursively rewrite absolute $ref paths to relative paths.

    Converts paths like "/schemas/v1/core/error.json" to "./error.json"
    """
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str):
                # Convert absolute path to relative
                if value.startswith("/schemas/v1/"):
                    # Extract just the filename
                    filename = value.split("/")[-1]
                    result[key] = f"./{filename}"
                else:
                    result[key] = value
            else:
                result[key] = rewrite_refs(value)
        return result
    elif isinstance(obj, list):
        return [rewrite_refs(item) for item in obj]
    else:
        return obj


def flatten_schemas():
    """
    Flatten schema directory structure and rewrite $ref paths.

    The tool has issues with nested $ref paths, so we:
    1. Copy all schemas from subdirectories into flat structure
    2. Rewrite absolute $ref paths to relative paths
    """
    print("Preparing schemas...")

    # Clean temp directory
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir()

    # Recursively find all JSON schemas (including subdirectories)
    schema_files = list(SCHEMAS_DIR.rglob("*.json"))
    # Filter out .hashes.json and index.json
    schema_files = [
        f
        for f in schema_files
        if f.name not in (".hashes.json", "index.json")
    ]

    for schema_file in schema_files:
        # Load schema
        with open(schema_file) as f:
            schema = json.load(f)

        # Rewrite $ref paths
        schema = rewrite_refs(schema)

        # Write to temp directory (flattened)
        output_file = TEMP_DIR / schema_file.name
        with open(output_file, "w") as f:
            json.dump(schema, f, indent=2)

        rel_path = schema_file.relative_to(SCHEMAS_DIR)
        print(f"  {rel_path}")

    count = len(list(TEMP_DIR.glob("*.json")))
    print(f"\n  Prepared {count} schema files\n")
    return TEMP_DIR


def fix_forward_references():
    """Fix broken forward references in generated files.

    datamodel-code-generator sometimes generates incorrect forward references like:
        from . import brand_manifest as brand_manifest_1
        field: brand_manifest.BrandManifest  # Should be brand_manifest_1.BrandManifest

    This function fixes those references.
    """
    print("Fixing forward references...")

    fixes_made = 0
    for py_file in OUTPUT_DIR.glob("*.py"):
        if py_file.name == "__init__.py":
            continue

        with open(py_file) as f:
            content = f.read()

        # Find imports like: from . import foo as foo_1
        import_pattern = r"from \. import (\w+) as (\w+_\d+)"
        imports = re.findall(import_pattern, content)

        # For each aliased import, fix references
        modified = False
        for original, alias in imports:
            # Replace module_name.ClassName with alias.ClassName
            pattern = rf"\b{original}\.(\w+)"
            replacement = rf"{alias}.\1"
            new_content = re.sub(pattern, replacement, content)
            if new_content != content:
                content = new_content
                modified = True
                fixes_made += 1

        if modified:
            with open(py_file, "w") as f:
                f.write(content)
            print(f"  Fixed: {py_file.name}")

    if fixes_made > 0:
        print(f"\n  Fixed {fixes_made} forward reference issue(s)\n")
    else:
        print("  No fixes needed\n")


def generate_types(input_dir: Path):
    """Generate types using datamodel-code-generator."""
    print(f"Generating types from {input_dir}...")

    args = [
        sys.executable,  # Use same Python as running this script
        "-m",
        "datamodel_code_generator",
        "--input",
        str(input_dir),
        "--input-file-type",
        "jsonschema",
        "--output",
        str(OUTPUT_DIR),
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--base-class",
        "adcp.types.base.AdCPBaseModel",
        "--field-constraints",
        "--use-standard-collections",
        "--use-union-operator",
        "--target-python-version",
        "3.10",
        "--use-annotated",
        "--collapse-root-models",
        "--reuse-model",
        "--set-default-enum-member",
        "--enum-field-as-literal",
        "one",
    ]

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
    )

    if result.stdout:
        print(result.stdout)

    if result.returncode != 0:
        print("\n✗ Generation failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return False

    return True


def normalize_timestamp(content: str) -> str:
    """Remove timestamp from generated file for comparison.

    Timestamps look like:
    #   timestamp: 2025-11-18T03:32:03+00:00
    """
    return re.sub(r"#\s+timestamp:.*\n", "", content)


def restore_unchanged_files():
    """Restore files where only the timestamp changed.

    This prevents noisy commits where the only change is the generation timestamp.
    We compare file contents ignoring timestamp lines.
    """
    print("Checking for timestamp-only changes...")

    # Get git status to see modified files
    result = subprocess.run(
        ["git", "diff", "--name-only", str(OUTPUT_DIR)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    if result.returncode != 0:
        print("  Could not check git status (skipping restoration)")
        return

    modified_files = [f for f in result.stdout.strip().split("\n") if f]
    restored_count = 0

    for rel_path in modified_files:
        file_path = REPO_ROOT / rel_path
        if not file_path.exists():
            continue

        # Get current (new) content
        with open(file_path) as f:
            new_content = f.read()

        # Get old content from git
        git_result = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

        if git_result.returncode != 0:
            continue

        old_content = git_result.stdout

        # Compare without timestamps
        if normalize_timestamp(old_content) == normalize_timestamp(new_content):
            # Only timestamp changed, restore old version
            subprocess.run(
                ["git", "checkout", "HEAD", "--", rel_path],
                cwd=REPO_ROOT,
                capture_output=True,
            )
            restored_count += 1

    if restored_count > 0:
        print(f"  ✓ Restored {restored_count} file(s) with only timestamp changes")
    else:
        print("  No timestamp-only changes found")


def apply_post_generation_fixes():
    """Apply post-generation fixes using the dedicated script."""
    print("Running post-generation fixes...")

    post_fix_script = REPO_ROOT / "scripts" / "post_generate_fixes.py"
    result = subprocess.run(
        [sys.executable, str(post_fix_script)],
        capture_output=True,
        text=True,
    )

    if result.stdout:
        print(result.stdout, end="")

    if result.returncode != 0:
        print("\n✗ Post-generation fixes failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return False

    return True


def main():
    """Generate types from schemas."""
    print("=" * 70)
    print("AdCP Python Type Generation")
    print("=" * 70)
    print(f"\nInput: {SCHEMAS_DIR}")
    print(f"Output: {OUTPUT_DIR}\n")

    temp_schemas = None
    try:
        # Clean output directory to prevent stale files
        # This ensures old/renamed schema files don't persist
        if OUTPUT_DIR.exists():
            print("Cleaning output directory...")
            shutil.rmtree(OUTPUT_DIR)
            print("  ✓ Removed stale generated files\n")

        # Ensure output directory exists
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Flatten schemas
        temp_schemas = flatten_schemas()

        # Generate types
        if not generate_types(temp_schemas):
            return 1

        # Fix forward references
        fix_forward_references()

        # Apply post-generation fixes
        if not apply_post_generation_fixes():
            return 1

        # Consolidate exports into generated.py
        consolidate_script = REPO_ROOT / "scripts" / "consolidate_exports.py"
        result = subprocess.run(
            [sys.executable, str(consolidate_script)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("\n✗ Export consolidation failed:", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return 1
        if result.stdout:
            print(result.stdout, end="")

        # Restore files where only timestamp changed
        restore_unchanged_files()

        # Count generated files
        py_files = list(OUTPUT_DIR.glob("*.py"))
        print("\n✓ Successfully generated types")
        print(f"  Output: {OUTPUT_DIR}")
        print(f"  Files: {len(py_files)}")

        return 0

    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1
    finally:
        # Clean up temp directory
        if temp_schemas and temp_schemas.exists():
            shutil.rmtree(temp_schemas)


if __name__ == "__main__":
    sys.exit(main())
