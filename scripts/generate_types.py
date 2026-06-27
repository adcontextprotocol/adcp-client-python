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

import diff_generated_types

# Paths
REPO_ROOT = Path(__file__).parent.parent


# Load ``resolve_bundle_key`` from its source file (importlib) rather than
# via ``from adcp.validation.version import ...``. The package import would
# trigger ``adcp/__init__.py``, which eagerly imports generated Pydantic
# models — but this script runs *during* regeneration, when those models
# may be in a half-regenerated state.
def _load_resolve_bundle_key():
    import importlib.util

    src = REPO_ROOT / "src" / "adcp" / "validation" / "version.py"
    spec = importlib.util.spec_from_file_location("_adcp_bundle_key", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_bundle_key


resolve_bundle_key = _load_resolve_bundle_key()

_VERSION_FILE = REPO_ROOT / "src" / "adcp" / "ADCP_VERSION"
_BUNDLE_KEY = resolve_bundle_key(_VERSION_FILE.read_text().strip())

SCHEMAS_DIR = REPO_ROOT / "schemas" / "cache" / _BUNDLE_KEY
OUTPUT_DIR = REPO_ROOT / "src" / "adcp" / "types" / "generated_poc"
TEMP_DIR = REPO_ROOT / ".schema_temp"
DELTAS_FILE = REPO_ROOT / "SCHEMA_DELTAS.md"

# Bundled schemas are self-contained: each message schema inlines its entire
# ``$ref`` graph, so every bundled module re-emits its own copy of the shared
# provenance/verification/asset sub-schemas. The generator can't merge those
# copies across files, so the bundled tree accounts for the overwhelming
# majority of generated classes. ``consolidate_exports.py`` already excludes
# bundled modules from the public namespace; the only one any source module
# imports is ``get_adcp_capabilities_response`` (via
# ``adcp.types.capabilities``), whose typed sub-models exist *only* in the
# inlined bundled form. Keep that module (and the package ``__init__`` files on
# its import path) and drop the rest after generation.
BUNDLED_DIR_NAME = "bundled"
BUNDLED_KEEP = {
    Path("__init__.py"),
    Path("protocol/__init__.py"),
    Path("protocol/get_adcp_capabilities_response.py"),
}


def rewrite_refs(obj, current_schema_rel_path: Path):
    """
    Recursively rewrite $ref paths:
    1. Convert absolute /schemas/latest/... paths to relative paths
    2. Replace hyphens with underscores for valid Python module names

    Args:
        obj: The JSON schema object to rewrite
        current_schema_rel_path: Relative path of current schema
            (e.g., signals/get-signals-request.json)
    """
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref_path = obj["$ref"]

            # Convert absolute /schemas/<version>/ paths to relative paths
            # Matches /schemas/latest/, /schemas/3.0.0-beta.1/, etc.
            version_match = re.match(r"/schemas/[^/]+/(.+)", ref_path)
            if version_match:
                # Extract the path after /schemas/<version>/
                # e.g., "/schemas/3.0.0-beta.1/core/context.json" -> "core/context.json"
                target_rel_path = version_match.group(1)

                # Compute relative path from current schema to target
                # current_schema_rel_path is like "signals/get-signals-request.json"
                # We need to go up to the root and then to the target
                current_dir = current_schema_rel_path.parent
                if current_dir == Path("."):
                    # Schema is at root level
                    ref_path = target_rel_path
                else:
                    # Need to go up from current directory
                    up_levels = len(current_dir.parts)
                    ref_path = "../" * up_levels + target_rel_path

            # Replace hyphens with underscores in each path segment
            parts = ref_path.split("/")
            parts = [part.replace("-", "_") for part in parts]
            obj["$ref"] = "/".join(parts)

        for value in obj.values():
            rewrite_refs(value, current_schema_rel_path)
    elif isinstance(obj, list):
        for item in obj:
            rewrite_refs(item, current_schema_rel_path)

    return obj


def stabilize_inlined_core_refs(schema: dict, current_schema_rel_path: Path) -> dict:
    """Keep inlined core schemas from rebasing sibling refs.

    datamodel-code-generator resolves refs nested inside an ``allOf``
    target against the caller's directory for some cross-directory refs.
    ``media-buy/build-creative-request.json`` references
    ``../core/signal-targeting.json`` and then the generator looks for
    ``media_buy/signal_ref.json`` instead of ``core/signal_ref.json``;
    ``signals/get-signals-response.json`` has the same shape through
    ``core/signal-listing.json`` and ``core/signal-definition-enrichment``.

    The published schemas stay untouched. In the temp tree only, rewrite
    the affected core-local sibling refs through ``../core/``. That keeps
    direct core generation equivalent and remains correct for the
    same-depth protocol directories that inline these helpers.
    """
    affected_core_schemas = {
        Path("core/signal-targeting.json"),
        Path("core/signal-listing.json"),
        Path("core/signal-definition-enrichment.json"),
    }
    if current_schema_rel_path not in affected_core_schemas:
        return schema

    def visit(value):
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and not ref.startswith(("#", "/")) and "://" not in ref:
                file_part, sep, fragment = ref.partition("#")
                if file_part.endswith(".json") and "/" not in file_part:
                    value["$ref"] = f"../core/{file_part}" + (sep + fragment if sep else "")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


def flatten_validation_oneof(schema: dict) -> dict:
    """Flatten anyOf/oneOf that only express required-field constraints.

    JSON Schema uses anyOf/oneOf with required-only branches to express
    "at least one of these field groups must be set." datamodel-code-generator
    misinterprets this as a type union, generating separate variant classes
    (e.g., FrequencyCap1, FrequencyCap2, FrequencyCap3) plus a RootModel wrapper.

    This function detects the pattern and removes the anyOf/oneOf, keeping
    only the intersection of required fields so a single class is generated.

    Follow-up to #155: enables consumer subclassing without RootModel or
    Union type alias barriers.
    """
    for key, value in list(schema.items()):
        if isinstance(value, dict):
            schema[key] = flatten_validation_oneof(value)
        elif isinstance(value, list):
            schema[key] = [
                flatten_validation_oneof(item) if isinstance(item, dict) else item for item in value
            ]

    if "properties" not in schema:
        return schema

    branch_key = None
    branches = None
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branch_key = key
            branches = schema[key]
            break

    if not branches:
        return schema

    # All branches must contain only 'required' (and optionally 'not')
    if not all(set(b.keys()) <= {"required", "not"} for b in branches):
        return schema

    # All branches are required-only — this is a validation constraint, not a type union
    # Compute the intersection of required fields (fields required in ALL branches)
    branch_required = [set(b.get("required", [])) for b in branches]
    always_required = set.intersection(*branch_required) if branch_required else set()

    # Include any top-level required fields
    top_required = set(schema.get("required", []))
    always_required |= top_required

    title = schema.get("title", "unknown")
    branch_count = len(branches)

    # Remove the anyOf/oneOf
    del schema[branch_key]

    # Set required to the intersection (or remove if empty)
    if always_required:
        schema["required"] = sorted(always_required)
    elif "required" in schema:
        del schema["required"]

    print(f"    flattened {branch_key} ({branch_count} branches) in {title}")
    return schema


def flatten_schemas():
    """
    Copy schemas to temp directory, preserving directory structure.

    We can't truly flatten because there are filename collisions:
    - media-buy/list-creative-formats-request.json
    - creative/list-creative-formats-request.json

    Directory names with hyphens are converted to underscores since Python
    module names cannot contain hyphens (pricing-options -> pricing_options).
    """
    print("Preparing schemas...")

    # Clean temp directory
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir()

    # Recursively find all JSON schemas (including subdirectories)
    schema_files = list(SCHEMAS_DIR.rglob("*.json"))
    # Skip the top-level index.json
    schema_files = [f for f in schema_files if f.name != "index.json"]

    for schema_file in schema_files:
        # Preserve directory structure relative to SCHEMAS_DIR
        rel_path = schema_file.relative_to(SCHEMAS_DIR)

        # Convert hyphens to underscores in directory names for valid Python identifiers
        path_parts = list(rel_path.parts)
        path_parts = [part.replace("-", "_") for part in path_parts]
        output_rel_path = Path(*path_parts)
        output_file = TEMP_DIR / output_rel_path

        # Create parent directories
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Load schema and rewrite refs to use underscores
        with open(schema_file) as f:
            schema = json.load(f)

        if rel_path == Path("adagents.json"):
            inline = schema.get("oneOf", [None, {}])[1]
            if isinstance(inline, dict):
                properties = inline.get("properties")
                if isinstance(properties, dict):
                    # datamodel-code-generator resolves refs nested under
                    # this root-level allOf as if they were relative to
                    # adagents.json, so ProductFormatDeclaration's
                    # ../enums refs escape the temp schema tree. The JSON
                    # schemas still ship and validate formats[]; only the
                    # generated convenience model omits this field.
                    properties.pop("formats", None)

        # Rewrite $ref paths: convert absolute paths to relative, hyphens to underscores
        schema = rewrite_refs(schema, rel_path)
        schema = stabilize_inlined_core_refs(schema, rel_path)

        # Flatten validation-only anyOf/oneOf into single-class schemas
        schema = flatten_validation_oneof(schema)

        with open(output_file, "w") as f:
            json.dump(schema, f, indent=2)

        print(f"  {rel_path}")

    count = len(schema_files)
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
    for py_file in OUTPUT_DIR.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue

        with open(py_file) as f:
            content = f.read()

        # Find imports like: from . import foo as foo_1 or from ..core import foo as foo_1
        # Pattern matches: "from" + dots + optional path + "import" + name + "as" + alias
        import_pattern = r"from \.+(?:[\w.]+\s+)?import (\w+) as (\w+_\d+)"
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


def _run_datamodel_codegen(input_path: Path, output_path: Path) -> subprocess.CompletedProcess[str]:
    """Run datamodel-code-generator for one input path."""
    args = [
        sys.executable,  # Use same Python as running this script
        "-m",
        "datamodel_code_generator",
        "--input",
        str(input_path),
        "--input-file-type",
        "jsonschema",
        "--output",
        str(output_path),
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
        "--reuse-model",
        "--set-default-enum-member",
        "--enum-field-as-literal",
        "one",
    ]

    return subprocess.run(
        args,
        capture_output=True,
        text=True,
    )


def _print_codegen_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout)


def _generate_split_bundled_media_buy(input_dir: Path) -> bool:
    """Generate bundled/media_buy schemas separately.

    The fully bundled media-buy directory is large enough that
    datamodel-code-generator can spend minutes trying to deduplicate the
    combined inline graph. Generating those bundled message schemas one file
    at a time preserves the import surface while keeping regeneration bounded.
    """
    split_dir = input_dir / "bundled" / "media_buy"
    if not split_dir.exists():
        return True

    output_dir = OUTPUT_DIR / "bundled" / "media_buy"
    output_dir.mkdir(parents=True, exist_ok=True)
    for package_dir in (OUTPUT_DIR / "bundled", output_dir):
        init_file = package_dir / "__init__.py"
        init_file.touch(exist_ok=True)

    print(f"Generating split bundled media-buy types from {split_dir}...")
    for schema_file in sorted(split_dir.glob("*.json")):
        output_file = output_dir / f"{schema_file.stem}.py"
        print(f"  {schema_file.name}")
        result = _run_datamodel_codegen(schema_file, output_file)
        _print_codegen_output(result)
        if result.returncode != 0:
            print("\n✗ Split bundled media-buy generation failed:", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return False

    return True


def generate_types(input_dir: Path):
    """Generate types using datamodel-code-generator."""
    print(f"Generating types from {input_dir}...")

    split_dir = input_dir / "bundled" / "media_buy"
    held_split_dir = input_dir.parent / f"{input_dir.name}_bundled_media_buy_split"

    if held_split_dir.exists():
        shutil.rmtree(held_split_dir)

    if split_dir.exists():
        shutil.move(str(split_dir), str(held_split_dir))

    try:
        result = _run_datamodel_codegen(input_dir, OUTPUT_DIR)
    finally:
        if held_split_dir.exists():
            split_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(held_split_dir), str(split_dir))

    _print_codegen_output(result)

    if result.returncode != 0:
        print("\n✗ Generation failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return False

    if not _generate_split_bundled_media_buy(input_dir):
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


def prune_unused_bundled_modules():
    """Drop generated bundled modules no source module imports.

    See ``BUNDLED_KEEP`` for why the bundled tree is almost entirely dead
    weight. Removing it here keeps the committed tree small without changing
    the content of any retained module — generation runs unchanged and this
    only deletes the unreferenced output afterwards.
    """
    bundled_dir = OUTPUT_DIR / BUNDLED_DIR_NAME
    if not bundled_dir.exists():
        return

    print("Pruning unused bundled modules...")
    removed = 0
    for py_file in bundled_dir.rglob("*.py"):
        if py_file.relative_to(bundled_dir) in BUNDLED_KEEP:
            continue
        py_file.unlink()
        removed += 1

    # Drop now-empty package directories (deepest first).
    for directory in sorted(
        (d for d in bundled_dir.rglob("*") if d.is_dir()),
        key=lambda d: len(d.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()

    print(f"  ✓ Removed {removed} unused bundled module(s)\n")


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
    before_snapshot: dict = {}
    try:
        # Snapshot the current generated tree before wiping it. The wipe-and-
        # regen pattern means we lose the only record of "what fields existed
        # last release" unless we capture it now. The diff produced after
        # generation lands in SCHEMA_DELTAS.md so consumers can shrink their
        # known-mismatch allowlists without grepping the raw diff.
        if OUTPUT_DIR.exists():
            before_snapshot = diff_generated_types.snapshot(OUTPUT_DIR)
            print(f"Captured pre-regen snapshot: {len(before_snapshot)} files\n")

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

        # Drop unreferenced bundled modules before consolidation
        prune_unused_bundled_modules()

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

        # Generate ergonomic coercion module (type coercion for better API ergonomics)
        # Reset _ergonomic.py first — the old version may import variant classes
        # that no longer exist after schema flattening (e.g., PackageUpdate1).
        ergonomic_file = REPO_ROOT / "src" / "adcp" / "types" / "_ergonomic.py"
        if ergonomic_file.exists():
            ergonomic_file.write_text(
                '"""Auto-generated ergonomic coercion — regenerating..."""\n'
                "\ndef apply_ergonomic_coercion() -> None:\n"
                "    pass\n"
            )

        ergonomic_script = REPO_ROOT / "scripts" / "generate_ergonomic_coercion.py"
        if ergonomic_script.exists():
            print("\nGenerating ergonomic coercion module...")
            result = subprocess.run(
                [sys.executable, str(ergonomic_script)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print("\n✗ Ergonomic coercion generation failed:", file=sys.stderr)
                print(result.stderr, file=sys.stderr)
                return 1
            if result.stdout:
                print(result.stdout, end="")

        # Count generated files
        py_files = list(OUTPUT_DIR.glob("*.py"))
        print("\n✓ Successfully generated types")
        print(f"  Output: {OUTPUT_DIR}")
        print(f"  Files: {len(py_files)}")

        after_snapshot = diff_generated_types.snapshot(OUTPUT_DIR)
        report = diff_generated_types.format_diff(before_snapshot, after_snapshot)
        DELTAS_FILE.write_text(report, encoding="utf-8")
        print(f"  Delta report: {DELTAS_FILE.relative_to(REPO_ROOT)}")

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
