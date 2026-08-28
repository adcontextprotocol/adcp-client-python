#!/usr/bin/env python3
"""
Generate Python types from AdCP JSON schemas using datamodel-code-generator.

This script processes schemas from the organized subdirectory structure and
generates Pydantic v2 models with discriminated union support.
"""

from __future__ import annotations

import json
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import diff_generated_types
except ModuleNotFoundError:  # Imported as ``scripts.generate_types`` in tests.
    from scripts import diff_generated_types

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

# Compiled MCP schemas are transport artifacts that inline the same source
# protocol models for each profile and task. Generating Python models from
# them creates tens of thousands of duplicate classes (the 3.2 beta bundle
# expands 416 MCP files into more than 28,000 modules). They remain packaged
# under ``schemas/`` for wire validation; the public Python types come from
# the source schemas instead.
GENERATED_SCHEMA_EXCLUDE_DIRS = {"mcp"}

# Root discovery documents can share names with protocol domains (brand.json
# vs brand/*.json). datamodel-code-generator turns that collision into a
# synthetic package ``__init__`` full of duplicate domain models. These
# discovery files still ship in the schema bundle but are not SDK task types.
GENERATED_SCHEMA_EXCLUDE_FILES = {Path("brand.json")}

# ``brand.json`` is both a discovery document and the basename of the
# ``brand/`` task-schema directory. Generate it as a standalone compatibility
# module after directory-mode generation so its public models remain
# available without turning ``generated_poc.brand`` into a synthetic package
# full of colliding models.
ROOT_DISCOVERY_SCHEMAS = {Path("brand.json"): Path("brand_discovery.py")}

# datamodel-code-generator resolves refs in these schemas from two different
# bases depending on whether it sees the schema as a directory entrypoint or
# an inlined child. Preserve their immutable canonical URLs so both modes
# resolve the same target. Keeping this allowlist narrow avoids duplicating
# the whole protocol graph through remote refs.
PRESERVE_CANONICAL_URL_REFS = {
    Path("core/assets/asset-union.json"),
    Path("core/assets/card-asset.json"),
    # MacroDeclaration is referenced from schemas under core/assets/. The
    # generator otherwise rebases its ../enums/macro-dialect.json child ref
    # against the asset directory and looks for core/enums/macro_dialect.json.
    Path("core/macro-declaration.json"),
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
            file_part, separator, fragment = ref_path.partition("#")

            # Convert root-relative and canonical absolute schema refs to
            # local files. This keeps generation deterministic and lets the
            # generator reuse source models instead of inlining a duplicate
            # model graph for every remote reference.
            canonical_url = file_part.startswith("https://adcontextprotocol.org/schemas/")
            preserve_canonical_url = (
                canonical_url and current_schema_rel_path in PRESERVE_CANONICAL_URL_REFS
            )
            version_match = None
            if not preserve_canonical_url:
                version_match = re.match(
                    r"^(?:https://adcontextprotocol\.org)?/schemas/[^/]+/(.+)$",
                    file_part,
                )
            if version_match:
                # Extract the path after /schemas/<version>/
                # e.g., "/schemas/3.0.0-beta.1/core/context.json" -> "core/context.json"
                target_rel_path = version_match.group(1)

                # Compute the shortest relative path from the current schema
                # to the target. Avoid logically equivalent root round-trips
                # such as ``../../core/assets/image.json`` from
                # ``core/assets/asset-union.json``: datamodel-code-generator
                # can incorrectly rebase those in directory mode.
                current_dir = current_schema_rel_path.parent
                file_part = posixpath.relpath(target_rel_path, start=current_dir.as_posix())

            # Only local filesystem paths are rewritten. External URLs and
            # JSON Pointer fragments are wire identifiers, not module names.
            if "://" not in file_part and not file_part.startswith("//"):
                parts = file_part.split("/")
                parts = [part.replace("-", "_") for part in parts]
                file_part = "/".join(parts)

            obj["$ref"] = file_part + (separator + fragment if separator else "")

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
    if current_schema_rel_path.parent == Path("core"):
        stable_prefix = "../core/"
    else:
        stable_prefix = None
    if stable_prefix is None:
        return schema

    def visit(value):
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and not ref.startswith(("#", "/")) and "://" not in ref:
                file_part, sep, fragment = ref.partition("#")
                if file_part.endswith(".json") and "/" not in file_part:
                    value["$ref"] = stable_prefix + file_part + (sep + fragment if sep else "")
                elif file_part.startswith(("assets/", "requirements/", "async_response_refs/")):
                    value["$ref"] = "../core/" + file_part + (sep + fragment if sep else "")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


def stabilize_nested_discriminators(schema: dict, current_schema_rel_path: Path) -> dict:
    """Avoid codegen flattening a valid nested discriminator into duplicates.

    ``core/format.json`` has an outer ``item_type`` discriminator whose
    ``individual`` branch has its own ``asset_type`` discriminator. The JSON
    Schema is valid, but datamodel-code-generator flattens the inner branches
    into the outer union and leaves ``Field(discriminator='item_type')`` on
    every individual variant. Pydantic then rejects the duplicate
    ``item_type='individual'`` choices at import time. Dropping only the outer
    generation hint preserves all const validation and lets Pydantic evaluate
    the resulting union normally.
    """
    if current_schema_rel_path != Path("core/format.json"):
        return schema
    assets = schema.get("properties", {}).get("assets", {})
    items = assets.get("items") if isinstance(assets, dict) else None
    if isinstance(items, dict):
        items.pop("discriminator", None)
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
    schema_files = [
        f
        for f in schema_files
        if f.relative_to(SCHEMAS_DIR) not in GENERATED_SCHEMA_EXCLUDE_FILES
        and f.relative_to(SCHEMAS_DIR).parts[0] not in GENERATED_SCHEMA_EXCLUDE_DIRS
    ]

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
        schema = stabilize_nested_discriminators(schema, rel_path)

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
        "--allow-remote-refs",
    ]

    return subprocess.run(
        args,
        capture_output=True,
        text=True,
    )


def _print_codegen_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout)


def _restore_optional_temp_dir(source: Path, target: Path) -> None:
    if source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def _hold_unused_bundled_dirs(input_dir: Path) -> Path:
    """Move bundled subtrees that are pruned later out of the generator input."""
    bundled_dir = input_dir / BUNDLED_DIR_NAME
    held_bundled_dir = input_dir.parent / f"{input_dir.name}_bundled_held"

    if held_bundled_dir.exists():
        shutil.rmtree(held_bundled_dir)
    if not bundled_dir.exists():
        return held_bundled_dir

    shutil.move(str(bundled_dir), str(held_bundled_dir))

    protocol_dir = held_bundled_dir / "protocol"
    if protocol_dir.exists():
        bundled_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(protocol_dir), str(bundled_dir / "protocol"))

    return held_bundled_dir


def _restore_unused_bundled_dirs(input_dir: Path, held_bundled_dir: Path) -> None:
    bundled_dir = input_dir / BUNDLED_DIR_NAME
    protocol_dir = bundled_dir / "protocol"

    if protocol_dir.exists():
        held_bundled_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(protocol_dir), str(held_bundled_dir / "protocol"))

    if bundled_dir.exists() and not any(bundled_dir.iterdir()):
        bundled_dir.rmdir()

    _restore_optional_temp_dir(held_bundled_dir, bundled_dir)


def generate_types(input_dir: Path):
    """Generate types using datamodel-code-generator."""
    print(f"Generating types from {input_dir}...")

    held_bundled_dir = _hold_unused_bundled_dirs(input_dir)

    try:
        result = _run_datamodel_codegen(input_dir, OUTPUT_DIR)
    finally:
        _restore_unused_bundled_dirs(input_dir, held_bundled_dir)

    _print_codegen_output(result)

    if result.returncode != 0:
        print("\n✗ Generation failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return False

    return True


def generate_root_discovery_types(input_dir: Path) -> bool:
    """Generate root discovery documents outside colliding domain packages."""
    print("Generating root discovery compatibility types...")
    for schema_rel_path, output_rel_path in ROOT_DISCOVERY_SCHEMAS.items():
        source = SCHEMAS_DIR / schema_rel_path
        prepared = input_dir / f"_{schema_rel_path.stem}_discovery.json"
        schema = json.loads(source.read_text())
        schema = rewrite_refs(schema, schema_rel_path)
        schema = flatten_validation_oneof(schema)
        prepared.write_text(json.dumps(schema, indent=2))

        result = _run_datamodel_codegen(prepared, OUTPUT_DIR / output_rel_path)
        _print_codegen_output(result)
        if result.returncode != 0:
            print(
                f"\n✗ Discovery type generation failed for {schema_rel_path}:",
                file=sys.stderr,
            )
            print(result.stderr, file=sys.stderr)
            return False
        print(f"  ✓ {schema_rel_path} -> {output_rel_path}")
    print()
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

        # Compare against the staged candidate when present so a schema bump can
        # prove regeneration is clean before it is committed. Fall back to HEAD
        # for ordinary unstaged development runs.
        git_result = subprocess.run(
            ["git", "show", f":{rel_path}"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

        if git_result.returncode != 0:
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
            # Only timestamp changed, restore the prior bytes without mutating
            # the index or invoking a destructive worktree command.
            file_path.write_text(old_content)
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

        if not generate_root_discovery_types(temp_schemas):
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
        if before_snapshot == after_snapshot and DELTAS_FILE.exists():
            print("  No new field-shape delta; retained the existing delta report")
        else:
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
