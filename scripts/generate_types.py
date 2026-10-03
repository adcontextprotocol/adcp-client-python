#!/usr/bin/env python3
"""
Generate Python types from AdCP JSON schemas using datamodel-code-generator.

This script processes schemas from the organized subdirectory structure and
generates Pydantic v2 models with discriminated union support.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
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
_PINNED_VERSION = _VERSION_FILE.read_text().strip()
_BUNDLE_KEY = resolve_bundle_key(_PINNED_VERSION)

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


def _normalize_schema_ref_target(
    file_part: str, current_schema_rel_path: Path
) -> tuple[str, Path] | None:
    """Return a schema-root-relative target and reject escaping references."""
    canonical_match = re.match(r"^https://adcontextprotocol\.org/schemas/[^/]+/(.+)$", file_part)
    if canonical_match:
        source_kind = "canonical"
        raw_target = canonical_match.group(1)
    elif file_part.startswith("/schemas/"):
        source_kind = "root"
        raw_target = file_part.removeprefix("/schemas/")
    elif not file_part or "://" in file_part or file_part.startswith("//"):
        return None
    else:
        source_kind = "local"
        raw_target = (current_schema_rel_path.parent / file_part).as_posix()

    normalized = posixpath.normpath(raw_target)
    if normalized in {"", ".", ".."} or normalized.startswith("../") or posixpath.isabs(normalized):
        raise ValueError(
            f"schema reference escapes its root: {file_part!r} from "
            f"{current_schema_rel_path.as_posix()!r}"
        )
    return source_kind, Path(normalized)


def _underscored_schema_path(path: Path) -> Path:
    return Path(*(part.replace("-", "_") for part in path.parts))


def _is_macro_schema(path: Path) -> bool:
    parts = path.parts
    return bool(
        len(parts) == 2
        and (
            (
                parts[0] == "enums"
                and (parts[1].startswith("macro-") or parts[1] == "universal-macro.json")
            )
            or (parts[0] == "core" and parts[1].startswith("macro-"))
        )
    )


def normalize_enum_descriptions(obj):
    """Normalize OpenAPI-style enum description maps for code generation.

    AdCP schemas key ``x-enum-descriptions`` by enum value. Newer
    datamodel-code-generator releases validate that extension as a positional
    list, so translate the map in enum order in the temporary schema tree.
    The published schema cache remains byte-for-byte unchanged.
    """
    if isinstance(obj, dict):
        descriptions = obj.get("x-enum-descriptions")
        enum_values = obj.get("enum")
        if isinstance(descriptions, dict) and isinstance(enum_values, list):
            obj["x-enum-descriptions"] = [
                str(descriptions.get(str(enum_value), "")) for enum_value in enum_values
            ]
        for value in obj.values():
            normalize_enum_descriptions(value)
    elif isinstance(obj, list):
        for item in obj:
            normalize_enum_descriptions(item)
    return obj


# A JSON pointer whose first token is a definition container names a reusable
# subschema, and datamodel-code-generator emits one class for it and shares it.
# Every other pointer addresses a structural position — ``/properties/<name>``,
# ``/items``, ``/oneOf/<n>`` — which has no name to emit, so the generator mints
# a single-arm ``RootModel`` wrapper per referencing site instead of the type the
# pointer selects.
_DEFINITION_POINTER_ROOTS = frozenset({"$defs", "definitions"})


def _is_structural_pointer(fragment: str) -> bool:
    """Report whether a ``$ref`` fragment addresses a structural position."""
    if not fragment.startswith("/"):
        return False
    first_token = fragment[1:].split("/", 1)[0]
    return first_token not in _DEFINITION_POINTER_ROOTS


def _resolve_json_pointer(document, fragment: str, ref: str):
    """Return the subschema a JSON pointer selects, or raise ``ValueError``."""
    node = document
    for raw_token in fragment[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"unresolvable pointer {ref!r}") from exc
        elif isinstance(node, dict) and token in node:
            node = node[token]
        else:
            raise ValueError(f"unresolvable pointer {ref!r}")
    if not isinstance(node, dict):
        raise ValueError(f"pointer {ref!r} selects a {type(node).__name__}, not a subschema")
    return node


def _rebase_refs_to_schema_root(obj, target: Path):
    """Rewrite a ``$ref`` inside an inlined subschema against the schema root.

    An inlined subschema travels from its own file to the referencing one, so
    its file-relative targets no longer resolve and its same-document fragments
    address a document that is no longer in scope. Root-relative ``/schemas/``
    paths are directory-independent and ``_normalize_schema_ref_target`` already
    accepts them.
    """
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str):
            file_part, separator, fragment = ref.partition("#")
            suffix = separator + fragment if separator else ""
            if not file_part:
                obj["$ref"] = "/schemas/" + target.as_posix() + suffix
            elif "://" not in file_part and not file_part.startswith("/"):
                resolved = posixpath.normpath(posixpath.join(target.parent.as_posix(), file_part))
                obj["$ref"] = "/schemas/" + resolved + suffix
        for value in obj.values():
            _rebase_refs_to_schema_root(value, target)
    elif isinstance(obj, list):
        for item in obj:
            _rebase_refs_to_schema_root(item, target)
    return obj


def _select_structural_pointer(ref: str, current_schema_rel_path: Path, seen: frozenset):
    """Resolve a cross-file structural pointer to a self-contained subschema."""
    file_part, _, fragment = ref.partition("#")
    if not file_part or not _is_structural_pointer(fragment):
        return None
    normalized = _normalize_schema_ref_target(file_part, current_schema_rel_path)
    if normalized is None:
        return None
    _, target = normalized
    key = (target.as_posix(), fragment)
    if key in seen or not (SCHEMAS_DIR / target).is_file():
        return None
    with open(SCHEMAS_DIR / target) as handle:
        document = json.load(handle)
    selected = copy.deepcopy(_resolve_json_pointer(document, fragment, ref))
    selected = _rebase_refs_to_schema_root(selected, target)
    # Resolve pointers nested inside the selection against the file that owns
    # it, and record this pointer so a cyclic selection terminates.
    return inline_structural_pointer_refs(selected, target, seen | {key})


def _merge_selection(obj: dict, selected: dict, dropped: str) -> dict:
    """Replace ``dropped`` in ``obj`` with ``selected``, local keywords winning.

    Annotation keywords at the referencing site (``description``, ``title``) and
    the constraints it adds describe this field, so they override the selection.
    """
    local = {k: v for k, v in obj.items() if k != dropped}
    obj.clear()
    obj.update(selected)
    obj.update(local)
    return obj


def inline_structural_pointer_refs(obj, current_schema_rel_path: Path, _seen=frozenset()):
    """Replace cross-file structural pointer ``$ref``s with the type they select.

    ``core/targeting-input.json`` declares 27 of its 37 fields as "the type
    ``core/targeting.json`` gives this property, or null"::

        {"anyOf": [{"$ref": ".../core/targeting.json#/properties/audience_exclude"},
                   {"type": "null"}]}

    The pointer selects ``{"type": "array", "items": {"type": "string"}, ...}``.
    datamodel-code-generator has no name for that position, so it emits
    ``targeting.AudienceExclude = RootModel[list[str]]`` and types the field with
    the wrapper. Resolving the pointer here gives the generator the subschema
    itself, which with ``collapse_nullable_unions`` becomes ``list[str] | None`` —
    the type the pointed-to property gets in its own module.

    A pointer that is the only arm of an ``allOf`` merges into the enclosing
    schema. ``core/catalog-selection.json`` adds ``uniqueItems`` that way and
    ``compliance/comply-test-controller-request.json`` adds a ``description``;
    leaving the selection inside the ``allOf`` loses the ``additionalProperties``
    the selection carries.
    """
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str):
            selected = _select_structural_pointer(ref, current_schema_rel_path, _seen)
            if selected is not None:
                # The selection is already resolved; recursing into it with a
                # fresh ``_seen`` would re-enter a cyclic pointer.
                return _merge_selection(obj, selected, "$ref")

        arms = obj.get("allOf")
        if isinstance(arms, list) and len(arms) == 1 and isinstance(arms[0], dict):
            only_arm_ref = arms[0].get("$ref")
            if isinstance(only_arm_ref, str) and len(arms[0]) == 1:
                selected = _select_structural_pointer(only_arm_ref, current_schema_rel_path, _seen)
                if selected is not None:
                    return _merge_selection(obj, selected, "allOf")

        for value in obj.values():
            inline_structural_pointer_refs(value, current_schema_rel_path, _seen)
    elif isinstance(obj, list):
        for item in obj:
            inline_structural_pointer_refs(item, current_schema_rel_path, _seen)

    return obj


_NULL_ARM = {"type": "null"}


def _as_nullable_arm(arms: list) -> dict | None:
    """Return the non-null arm of a two-arm "S or null" union, when typed.

    Only an arm that carries a ``type`` keyword qualifies. An arm that is a
    ``$ref`` already produces ``T | None``, and an arm with no ``type`` has
    nothing to make nullable.
    """
    if len(arms) != 2 or arms.count(_NULL_ARM) != 1:
        return None
    other = arms[0] if arms[1] == _NULL_ARM else arms[1]
    if not isinstance(other, dict) or "$ref" in other:
        return None
    declared = other.get("type")
    if isinstance(declared, str):
        return {**other, "type": [declared, "null"]}
    if isinstance(declared, list) and "null" not in declared:
        return {**other, "type": [*declared, "null"]}
    return None


def collapse_nullable_unions(obj):
    """Spell "S or null" as a nullable ``S`` rather than a two-arm union.

    datamodel-code-generator emits a named ``RootModel`` for an ``anyOf`` arm
    that declares a container or scalar ``type``, then types the field with the
    wrapper::

        {"anyOf": [{"type": "array", "items": {"type": "string"}},
                   {"type": "null"}]}

        class AudienceExclude(RootModel[list[str]]): ...
        audience_exclude: AudienceExclude | None

    The same nullability written as a type list produces the type itself::

        {"type": ["array", "null"], "items": {"type": "string"}}

        audience_exclude: list[str] | None

    ``core/targeting-input.json`` carries both spellings — ``keyword_targets``
    uses the type list and gets ``list[KeywordTarget] | None``, while the 27
    fields declared as a union get wrappers. The wrapper is not a usable
    container: it rejects indexing and ``len()``, and iterating it yields the
    ``('root', [...])`` field pair instead of the elements. Normalize the union
    spelling to the type-list spelling so one wire shape produces one type.

    Only a named property is normalized. A nullable type list in an ``items``
    position makes the generator emit ``class XItem5(Optional[XItem])``, which
    does not import and does not run; ``formats/canonical/audio_hosted.json``
    declares ``duration_ms_range.items`` that way.
    """
    if isinstance(obj, dict):
        properties = obj.get("properties")
        if isinstance(properties, dict):
            for schema in properties.values():
                if not isinstance(schema, dict):
                    continue
                for keyword in ("anyOf", "oneOf"):
                    collapsed = _as_nullable_arm(schema.get(keyword) or [])
                    if collapsed is not None:
                        siblings = {k: v for k, v in schema.items() if k != keyword}
                        schema.clear()
                        schema.update(collapsed)
                        schema.update(siblings)
                        break
        for value in obj.values():
            collapse_nullable_unions(value)
    elif isinstance(obj, list):
        for item in obj:
            collapse_nullable_unions(item)

    return obj


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
            normalized = _normalize_schema_ref_target(file_part, current_schema_rel_path)
            suffix = separator + fragment if separator else ""

            if normalized is not None:
                source_kind, target = normalized
                preserve_canonical = (
                    source_kind == "canonical"
                    and current_schema_rel_path in PRESERVE_CANONICAL_URL_REFS
                )
                preserve_local = (
                    source_kind == "local"
                    and current_schema_rel_path in PRESERVE_CANONICAL_URL_REFS
                )

                # datamodel-code-generator rebases transitive macro refs
                # against their caller. Absolute paths make the one canonical
                # temp-tree model unambiguous in directory and inlined modes.
                if preserve_local or (
                    not preserve_canonical and not fragment and _is_macro_schema(target)
                ):
                    obj["$ref"] = (TEMP_DIR / _underscored_schema_path(target)).as_posix() + suffix
                elif not preserve_canonical:
                    relative = posixpath.relpath(
                        target.as_posix(), start=current_schema_rel_path.parent.as_posix()
                    )
                    obj["$ref"] = relative.replace("-", "_") + suffix
            elif file_part and "://" not in file_part and not file_part.startswith("//"):
                obj["$ref"] = file_part.replace("-", "_") + suffix

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

    # Alternative annotations do not turn a required-field constraint into a
    # distinct object shape.
    validation_branch_keys = {
        "$comment",
        "deprecated",
        "description",
        "examples",
        "not",
        "required",
        "title",
    }
    if not all(
        isinstance(branch, dict) and set(branch) <= validation_branch_keys for branch in branches
    ):
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


_ROOT_OBJECT_VALIDATION_UNIONS = {
    Path("account/list-account-changes-response.json"),
    Path("brand/search-brands-response.json"),
    Path("brand/verify-brand-claim-request.json"),
    Path("core/product.json"),
    Path("media-buy/buy-products-request.json"),
    Path("media-buy/create-media-buy-request.json"),
    Path("media-buy/get-reporting-status-response.json"),
    Path("media-buy/request-proposals-request.json"),
}


def flatten_root_object_validation_union(schema: dict, schema_path: Path) -> dict:
    """Keep known root object overlays as concrete, subclassable models.

    These schemas put their shared object envelope at the root and use a
    root-level ``oneOf``/``anyOf`` only for cross-field validation. codegen
    0.63+ emits a ``RootModel`` union for that pattern, which breaks the SDK's
    public subclassability contract. Merge branch-only fields conservatively;
    nested unions remain untouched and runtime validators continue to enforce
    the cross-field rules that the older generator also could not express.
    """
    if schema_path not in _ROOT_OBJECT_VALIDATION_UNIONS or schema.get("type") != "object":
        return schema

    branch_key = next((key for key in ("anyOf", "oneOf") if key in schema), None)
    if branch_key is None:
        return schema
    branches = schema[branch_key]
    if (
        not isinstance(branches, list)
        or not branches
        or not all(isinstance(branch, dict) for branch in branches)
    ):
        return schema

    properties = dict(schema.get("properties", {}))
    branch_properties: dict[str, list[dict]] = {}
    for branch in branches:
        for name, definition in branch.get("properties", {}).items():
            if name not in properties and isinstance(definition, dict):
                branch_properties.setdefault(name, []).append(definition)

    for name, definitions in branch_properties.items():
        unique = {json.dumps(definition, sort_keys=True): definition for definition in definitions}
        variants = list(unique.values())
        if len(variants) == 1:
            properties[name] = variants[0]
        elif all("const" in variant for variant in variants):
            properties[name] = {"enum": [variant["const"] for variant in variants]}
        elif all(variant.get("type") == "object" for variant in variants):
            properties[name] = {"type": "object", "additionalProperties": True}
        else:
            properties[name] = {"anyOf": variants}

    schema["properties"] = properties
    top_required = set(schema.get("required", []))
    branch_required = [set(branch.get("required", [])) for branch in branches]
    common_required = set.intersection(*branch_required) if branch_required else set()
    required = sorted(top_required | common_required)
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    del schema[branch_key]
    print(f"    flattened root {branch_key} object overlay in {schema_path}")
    return schema


def flatten_schemas(temp_dir: Path):
    """
    Copy schemas to temp directory, preserving directory structure.

    We can't truly flatten because there are filename collisions:
    - media-buy/list-creative-formats-request.json
    - creative/list-creative-formats-request.json

    Directory names with hyphens are converted to underscores since Python
    module names cannot contain hyphens (pricing-options -> pricing_options).
    """
    print("Preparing schemas...")

    global TEMP_DIR
    TEMP_DIR = temp_dir

    # Clean temp directory
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir()

    # Recursively find all JSON schemas (including subdirectories)
    # The generator assigns numeric suffixes while traversing this aggregate
    # input tree.  Path.rglob() follows filesystem insertion order, which
    # differs between developer machines and fresh CI checkouts.  Create the
    # temporary tree in a stable order so generated names are reproducible.
    schema_files = sorted(SCHEMAS_DIR.rglob("*.json"))
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
        output_file = temp_dir / output_rel_path

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

        # Normalize generator-specific extensions, resolve structural pointer
        # refs into the type they select, then rewrite $ref paths.
        schema = normalize_enum_descriptions(schema)
        schema = inline_structural_pointer_refs(schema, rel_path)
        schema = collapse_nullable_unions(schema)
        schema = rewrite_refs(schema, rel_path)
        schema = stabilize_inlined_core_refs(schema, rel_path)
        schema = stabilize_nested_discriminators(schema, rel_path)

        # Flatten validation-only anyOf/oneOf into single-class schemas.
        schema = flatten_root_object_validation_union(schema, rel_path)
        schema = flatten_validation_oneof(schema)

        with open(output_file, "w") as f:
            json.dump(schema, f, indent=2)

        print(f"  {rel_path}")

    count = len(schema_files)
    print(f"\n  Prepared {count} schema files\n")
    return temp_dir


def fix_forward_references(output_dir: Path = OUTPUT_DIR):
    """Fix broken forward references in generated files.

    datamodel-code-generator sometimes generates incorrect forward references like:
        from . import brand_manifest as brand_manifest_1
        field: brand_manifest.BrandManifest  # Should be brand_manifest_1.BrandManifest

    This function fixes those references.
    """
    print("Fixing forward references...")

    fixes_made = 0
    for py_file in output_dir.rglob("*.py"):
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
    """Generate from the pinned cache, including canonical HTTP references.

    A few nested asset references retain canonical URLs to prevent the
    generator from rebasing them against their caller. Resolve those URLs
    from the same cache rather than downloading a second copy from the CDN.
    Missing references fail locally; there is no network fallback.
    """
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

    with tempfile.TemporaryDirectory(prefix="adcp-codegen-refs-") as mirror:
        cached = Path(mirror) / "adcontextprotocol.org" / "schemas" / _BUNDLE_KEY
        shutil.copytree(SCHEMAS_DIR, cached)
        if _PINNED_VERSION != _BUNDLE_KEY:
            # A stable bundle is cached as MAJOR.MINOR, but its absolute
            # references name the exact release, e.g. /schemas/3.2.1/.
            (cached.parent / _PINNED_VERSION).symlink_to(cached, target_is_directory=True)
        args.extend(["--http-local-ref-path", mirror])
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


def generate_types(input_dir: Path, output_dir: Path = OUTPUT_DIR):
    """Generate types using datamodel-code-generator."""
    print(f"Generating types from {input_dir}...")

    held_bundled_dir = _hold_unused_bundled_dirs(input_dir)

    try:
        result = _run_datamodel_codegen(input_dir, output_dir)
    finally:
        _restore_unused_bundled_dirs(input_dir, held_bundled_dir)

    _print_codegen_output(result)

    if result.returncode != 0:
        print("\n✗ Generation failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return False

    return True


def generate_root_discovery_types(input_dir: Path, output_dir: Path = OUTPUT_DIR) -> bool:
    """Generate root discovery documents outside colliding domain packages."""
    print("Generating root discovery compatibility types...")
    for schema_rel_path, output_rel_path in ROOT_DISCOVERY_SCHEMAS.items():
        source = SCHEMAS_DIR / schema_rel_path
        prepared = input_dir / f"_{schema_rel_path.stem}_discovery.json"
        schema = json.loads(source.read_text())
        schema = normalize_enum_descriptions(schema)
        schema = rewrite_refs(schema, schema_rel_path)
        schema = flatten_validation_oneof(schema)
        prepared.write_text(json.dumps(schema, indent=2))

        result = _run_datamodel_codegen(prepared, output_dir / output_rel_path)
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
    content = re.sub(r"#\s+timestamp:.*\n", "", content)
    return re.sub(r"^Generation date:.*\n", "", content, flags=re.MULTILINE)


def restore_unchanged_file(candidate: Path, baseline: Path) -> bool:
    """Preserve prior bytes when a generated file changed only by timestamp."""
    if not baseline.is_file():
        return False
    if normalize_timestamp(candidate.read_text()) != normalize_timestamp(baseline.read_text()):
        return False
    candidate.write_bytes(baseline.read_bytes())
    return True


def restore_unchanged_files(candidate_dir: Path, baseline_dir: Path = OUTPUT_DIR) -> None:
    """Restore files where only the timestamp changed.

    This prevents noisy commits where the only change is the generation timestamp.
    We compare file contents ignoring timestamp lines.
    """
    print("Checking for timestamp-only changes...")

    restored_count = 0
    for candidate in candidate_dir.rglob("*.py"):
        relative = candidate.relative_to(candidate_dir)
        baseline = baseline_dir / relative
        if not baseline.is_file():
            continue
        if restore_unchanged_file(candidate, baseline):
            restored_count += 1

    if restored_count > 0:
        print(f"  ✓ Restored {restored_count} file(s) with only timestamp changes")
    else:
        print("  No timestamp-only changes found")


def prune_unused_bundled_modules(output_dir: Path = OUTPUT_DIR):
    """Drop generated bundled modules no source module imports.

    See ``BUNDLED_KEEP`` for why the bundled tree is almost entirely dead
    weight. Removing it here keeps the committed tree small without changing
    the content of any retained module — generation runs unchanged and this
    only deletes the unreferenced output afterwards.
    """
    bundled_dir = output_dir / BUNDLED_DIR_NAME
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


def apply_post_generation_fixes(output_dir: Path = OUTPUT_DIR):
    """Apply post-generation fixes using the dedicated script."""
    print("Running post-generation fixes...")

    post_fix_script = REPO_ROOT / "scripts" / "post_generate_fixes.py"
    result = subprocess.run(
        [sys.executable, str(post_fix_script), "--output-dir", str(output_dir)],
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report generated drift without modifying the checkout",
    )
    return parser.parse_args(argv)


def _copy_package_for_introspection(staging_root: Path, generated_dir: Path) -> Path:
    """Build an isolated source tree whose imports use the staged models."""
    staged_source = staging_root / "source"
    staged_package = staged_source / "adcp"
    shutil.copytree(
        REPO_ROOT / "src" / "adcp",
        staged_package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_schemas"),
    )
    staged_generated = staged_package / "types" / "generated_poc"
    if staged_generated.exists():
        shutil.rmtree(staged_generated)
    shutil.copytree(generated_dir, staged_generated)

    # The committed ergonomic module can import generated names that no longer
    # exist. The isolated import graph uses this inert stub until a fresh module
    # is generated from the staged models.
    (staged_package / "types" / "_ergonomic.py").write_text(
        '"""Temporary ergonomic stub used during type generation."""\n\n'
        "def apply_ergonomic_coercion() -> None:\n"
        "    pass\n"
    )
    return staged_source


def _artifact_files(root: Path) -> set[Path]:
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _paths_equal(candidate: Path, current: Path) -> bool:
    if candidate.is_file() and current.is_file():
        return candidate.read_bytes() == current.read_bytes()
    if not candidate.is_dir() or not current.is_dir():
        return False
    candidate_files = _artifact_files(candidate)
    current_files = _artifact_files(current)
    return candidate_files == current_files and all(
        (candidate / relative).read_bytes() == (current / relative).read_bytes()
        for relative in candidate_files
    )


def _changed_paths(candidate: Path, current: Path) -> list[Path]:
    """Return file-level drift paths relative to an artifact root."""
    if candidate.is_file() or current.is_file():
        return [] if _paths_equal(candidate, current) else [Path(candidate.name)]
    candidate_files = _artifact_files(candidate)
    current_files = _artifact_files(current)
    return sorted(
        relative
        for relative in candidate_files | current_files
        if relative not in candidate_files
        or relative not in current_files
        or (candidate / relative).read_bytes() != (current / relative).read_bytes()
    )


def _install_generated_artifacts(staging_root: Path, artifacts: list[tuple[Path, Path]]) -> None:
    """Install staged artifacts with rollback if any replacement fails."""
    backup_root = staging_root / "backups"
    backup_root.mkdir()
    backups: list[tuple[Path, Path]] = []
    installed: list[tuple[Path, Path]] = []
    try:
        for index, (candidate, target) in enumerate(artifacts):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = backup_root / f"artifact-{index}"
                os.replace(target, backup)
                backups.append((backup, target))
            os.replace(candidate, target)
            installed.append((target, candidate))
    except BaseException:
        for target, candidate in reversed(installed):
            if target.exists():
                os.replace(target, candidate)
        for backup, target in reversed(backups):
            if backup.exists():
                os.replace(backup, target)
        raise


def main(argv: list[str] | None = None):
    """Generate types from schemas."""
    args = _parse_args(argv)

    print("=" * 70)
    print("AdCP Python Type Generation")
    print("=" * 70)
    print(f"\nInput: {SCHEMAS_DIR}")
    print(f"Output: {OUTPUT_DIR}\n")

    before_snapshot: dict = {}
    try:
        if OUTPUT_DIR.exists():
            before_snapshot = diff_generated_types.snapshot(OUTPUT_DIR)
            print(f"Captured pre-regen snapshot: {len(before_snapshot)} files\n")

        with tempfile.TemporaryDirectory(dir=REPO_ROOT, prefix=".typegen-") as temp_root:
            staging_root = Path(temp_root)
            staged_output = staging_root / "generated_poc"
            # Keep the historical basename stable: datamodel-code-generator
            # embeds it in every package ``__init__.py`` header.
            temp_schemas = flatten_schemas(staging_root / ".schema_temp")

            if not generate_types(temp_schemas, staged_output):
                return 1
            if not generate_root_discovery_types(temp_schemas, staged_output):
                return 1

            fix_forward_references(staged_output)
            if not apply_post_generation_fixes(staged_output):
                return 1
            prune_unused_bundled_modules(staged_output)
            restore_unchanged_files(staged_output)

            staged_source = _copy_package_for_introspection(staging_root, staged_output)
            staged_types = staged_source / "adcp" / "types"
            staged_consolidated = staged_types / "_generated.py"
            staged_ergonomic = staged_types / "_ergonomic.py"

            consolidate_script = REPO_ROOT / "scripts" / "consolidate_exports.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(consolidate_script),
                    "--input-dir",
                    str(staged_output),
                    "--output-file",
                    str(staged_consolidated),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print("\n✗ Export consolidation failed:", file=sys.stderr)
                print(result.stderr, file=sys.stderr)
                return 1
            if result.stdout:
                print(result.stdout, end="")

            ergonomic_script = REPO_ROOT / "scripts" / "generate_ergonomic_coercion.py"
            if ergonomic_script.exists():
                print("\nGenerating ergonomic coercion module...")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ergonomic_script),
                        "--source-root",
                        str(staged_source),
                        "--output-file",
                        str(staged_ergonomic),
                    ],
                    capture_output=True,
                    text=True,
                    cwd=staging_root,
                )
                if result.returncode != 0:
                    print("\n✗ Ergonomic coercion generation failed:", file=sys.stderr)
                    print(result.stderr, file=sys.stderr)
                    return 1
                if result.stdout:
                    print(result.stdout, end="")

            current_types = REPO_ROOT / "src" / "adcp" / "types"
            restore_unchanged_file(staged_consolidated, current_types / "_generated.py")
            restore_unchanged_file(staged_ergonomic, current_types / "_ergonomic.py")

            artifacts = [
                (staged_output, OUTPUT_DIR),
                (staged_consolidated, current_types / "_generated.py"),
                (staged_ergonomic, current_types / "_ergonomic.py"),
            ]
            changed = [
                target for candidate, target in artifacts if not _paths_equal(candidate, target)
            ]

            if args.check:
                if changed:
                    print("\n✗ Generated types are out of date:")
                    for candidate, target in artifacts:
                        if target not in changed:
                            continue
                        relative_target = target.relative_to(REPO_ROOT)
                        for relative in _changed_paths(candidate, target):
                            detail = (
                                relative_target / relative if target.is_dir() else relative_target
                            )
                            print(f"  {detail}")
                    return 1
                print("\n✓ Generated types are up to date")
                return 0

            after_snapshot = diff_generated_types.snapshot(staged_output)
            _install_generated_artifacts(staging_root, artifacts)

            report = diff_generated_types.format_diff(before_snapshot, after_snapshot)
            if before_snapshot == after_snapshot and DELTAS_FILE.exists():
                print("  No new field-shape delta; retained the existing delta report")
            else:
                DELTAS_FILE.write_text(report, encoding="utf-8")
            print(f"  Delta report: {DELTAS_FILE.relative_to(REPO_ROOT)}")

            py_files = list(OUTPUT_DIR.rglob("*.py"))
            print("\n✓ Successfully generated types")
            print(f"  Output: {OUTPUT_DIR}")
            print(f"  Files: {len(py_files)}")

        return 0

    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
