#!/usr/bin/env python3
"""
Create a consolidated export file that re-exports all types from generated_poc modules.

This script analyzes all modules in generated_poc/ and creates a single generated.py
that imports and re-exports all public types, handling naming conflicts appropriately.

A build guard fails the consolidate step for any name collision that is neither
handled via qualified imports (KNOWN_COLLISIONS) nor recorded in the checked-in
allowlist (collision_allowlist.json). See issue #911.

Usage:
    python scripts/consolidate_exports.py                  # consolidate + guard
    python scripts/consolidate_exports.py --update-allowlist  # refresh the snapshot
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

GENERATED_POC_DIR = Path(__file__).parent.parent / "src" / "adcp" / "types" / "generated_poc"
OUTPUT_FILE = Path(__file__).parent.parent / "src" / "adcp" / "types" / "_generated.py"

# Checked-in snapshot of every bare type name that is defined in more than one
# non-bundled generated module today and is knowingly resolved by first-seen /
# stem-preference order (see the build guard in generate_consolidated_exports).
# Regenerate with: python scripts/consolidate_exports.py --update-allowlist
COLLISION_ALLOWLIST_FILE = Path(__file__).parent / "collision_allowlist.json"

# Names handled explicitly via qualified imports (see KNOWN_COLLISIONS below).
# These are NOT in the allowlist file — the qualified-import machinery already
# exports every variant, so neither version silently wins.
#
# We need BOTH versions of these types available, so import them with qualified
# names.
KNOWN_COLLISIONS: dict[str, set[str]] = {
    "Package": {"package", "create_media_buy_response", "get_media_buys_response"},
    # DeliveryStatus appears in get_media_buy_delivery_response (5 values) and
    # get_media_buys_response (6 values, adds not_delivering). Export both with
    # qualified names so aliases.py can re-export the superset as the canonical one.
    "DeliveryStatus": {"get_media_buy_delivery_response", "get_media_buys_response"},
    # Note: "Catalog" also collides between core.catalog and media_buy.sync_catalogs_response.
    # We intentionally let core.catalog win (first-seen, since core/ sorts before media_buy/).
    # The response-level Catalog is imported directly in aliases.py as SyncCatalogResult.
    # Audience collides between get_media_buy_delivery_request (breakdown config) and
    # sync_audiences_request (audience payload). aliases.py imports the request one directly.
    "Audience": {
        "get_media_buy_delivery_request",
        "sync_audiences_request",
        "sync_audiences_response",
    },
    # Error collides between core.error (Pydantic model used everywhere) and
    # compliance.comply_test_controller_response (test-only enum). Export both
    # with qualified names; aliases/init re-export core Error as the canonical one.
    "Error": {"error", "comply_test_controller_response"},
    # FormatId: AdCP 3.0.1 renamed core/format-id.json title from "Format ID"
    # to "Format Reference (Structured Object)". The canonical class in
    # core/format_id.py is now FormatReferenceStructuredObject, but every
    # bundled-message file inlines a per-message duplicate still named
    # FormatId. Without this entry, the bundled stale duplicate would win
    # the bare-name slot in _generated.py and shadow the canonical class.
    # aliases.py re-exports the canonical FormatReferenceStructuredObject as
    # the public FormatId.
    "FormatId": {
        "build_creative_request",
        "build_creative_response",
        "calibrate_content_request",
        "create_content_standards_request",
        "create_media_buy_request",
        "create_media_buy_response",
        "get_content_standards_response",
        "get_creative_delivery_response",
        "get_creative_features_request",
        "get_media_buy_artifacts_response",
        "get_products_request",
        "get_products_response",
        "list_content_standards_response",
        "list_creative_formats_request",
        "list_creative_formats_response",
        "list_creatives_request",
        "list_creatives_response",
        "package_request",
        "preview_creative_request",
        "preview_creative_response",
        "sync_creatives_request",
        "update_content_standards_request",
        "update_media_buy_request",
        "update_media_buy_response",
        "validate_content_delivery_request",
    },
    # DeclaredBy appears in core provenance and SI sponsored-context schemas
    # with different Role enums. Export both qualified variants and expose
    # semantic aliases from aliases.py.
    "DeclaredBy": {"provenance", "si_sponsored_context"},
}


def load_collision_allowlist() -> set[str]:
    """Load the checked-in snapshot of knowingly-resolved collision names."""
    if not COLLISION_ALLOWLIST_FILE.exists():
        return set()
    data = json.loads(COLLISION_ALLOWLIST_FILE.read_text())
    return set(data["allowlist"])


def extract_exports_from_module(module_path: Path) -> set[str]:
    """Extract all public class and type alias names from a Python module."""
    with open(module_path) as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return set()

    exports = set()

    def _add_public_type_name(name: str) -> None:
        if name and not name.startswith("_") and name[0].isupper():
            exports.add(name)

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
                    _add_public_type_name(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # ``Foo: TypeAlias = ...`` is common in post-generated response
            # unions; keep those public aliases in the consolidated namespace.
            _add_public_type_name(node.target.id)

    return exports


def _collisions_from_accounting(
    name_to_modules: dict[str, set[str]], known_names: set[str]
) -> set[str]:
    """Names defined in >1 module, excluding the qualified-import known set."""
    return {n for n, mods in name_to_modules.items() if len(mods) > 1} - known_names


def _enforce_collision_allowlist(
    name_to_modules: dict[str, set[str]], known_names: set[str]
) -> None:
    """Fail the build for any collision not in the checked-in allowlist.

    A collision is a bare type name defined in more than one non-bundled
    generated module. ``KNOWN_COLLISIONS`` handles its set via qualified
    imports; every other collision is silently resolved by first-seen /
    stem-preference order, which means one class shadows the others for
    adopters importing from ``adcp.types``. The allowlist is a snapshot of the
    collisions we knowingly tolerate today. Anything not in it is a NEW or
    CHANGED collision and must be triaged before it ships.
    """
    collisions = _collisions_from_accounting(name_to_modules, known_names)
    allowlist = load_collision_allowlist()

    unexpected = sorted(collisions - allowlist)
    if not unexpected:
        return

    lines = []
    for name in unexpected:
        mods = sorted(name_to_modules[name])
        lines.append(f"  {name}: defined in {mods}")
    details = "\n".join(lines)
    raise ValueError(
        f"{len(unexpected)} new name collision(s) in generated_poc are not in "
        f"the checked-in allowlist:\n{details}\n\n"
        "A collision means the same bare type name is defined in more than one "
        "generated module, so 'from adcp.types import <Name>' silently resolves "
        "to whichever module wins the sort order — adopters get an unpredictable "
        "class shape.\n\n"
        "To fix, pick ONE:\n"
        "  1. Add a disambiguated alias in src/adcp/types/aliases.py so adopters "
        "can import each variant by an unambiguous name (preferred).\n"
        f"  2. If this name genuinely belongs in {COLLISION_ALLOWLIST_FILE.name} "
        "(first-seen resolution is acceptable), regenerate the allowlist with a "
        "justification:\n"
        "       python scripts/consolidate_exports.py --update-allowlist\n"
        "     and review the diff — every added name is a class an adopter can no "
        "longer import unambiguously from adcp.types.\n"
        "  3. If the name needs BOTH variants exported under qualified names, add "
        "it to KNOWN_COLLISIONS in scripts/consolidate_exports.py.\n"
    )


def update_collision_allowlist() -> int:
    """Regenerate the checked-in collision allowlist from the current tree."""
    name_to_modules = _scan_name_to_modules()
    collisions = _collisions_from_accounting(name_to_modules, set(KNOWN_COLLISIONS))
    payload = {
        "_comment": (
            "Snapshot of bare type names defined in more than one non-bundled "
            "generated_poc module that are knowingly resolved by first-seen / "
            "stem-preference order in scripts/consolidate_exports.py. Each name "
            "is a class an adopter cannot import unambiguously from adcp.types. "
            "Regenerate with: python scripts/consolidate_exports.py "
            "--update-allowlist. A growing list is a signal to add disambiguated "
            "aliases in src/adcp/types/aliases.py (issue #911)."
        ),
        "allowlist": sorted(collisions),
    }
    COLLISION_ALLOWLIST_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {len(collisions)} collision names to {COLLISION_ALLOWLIST_FILE}")
    return 0


def _scan_name_to_modules() -> dict[str, set[str]]:
    """Map every public name to the set of non-bundled modules that define it."""

    def _module_sort_key(p: Path) -> tuple[int, int, str]:
        rel = p.relative_to(GENERATED_POC_DIR)
        is_enum = rel.parts[0] == "enums" if len(rel.parts) > 1 else False
        is_bundled = rel.parts[0] == "bundled" if len(rel.parts) > 1 else False
        return (0 if is_enum else 1, 1 if is_bundled else 0, str(p))

    modules = sorted(GENERATED_POC_DIR.rglob("*.py"), key=_module_sort_key)
    modules = [
        m
        for m in modules
        if m.stem != "__init__"
        and not m.stem.startswith(".")
        and m.relative_to(GENERATED_POC_DIR).parts[0] != "bundled"
    ]

    name_to_modules: dict[str, set[str]] = {}
    for module_path in modules:
        rel_path = module_path.relative_to(GENERATED_POC_DIR)
        module_name = ".".join(list(rel_path.parts[:-1]) + [rel_path.stem])
        for export_name in extract_exports_from_module(module_path):
            name_to_modules.setdefault(export_name, set()).add(module_name)
    return name_to_modules


def generate_consolidated_exports() -> str:
    """Generate the consolidated exports file content."""

    # Discover all modules recursively (including subdirectories)
    # Sort order: enums first (canonical enum definitions), then non-bundled,
    # then bundled. Bundled schemas inline the same types as non-bundled, but
    # as renumbered/enum duplicates — we want the canonical class definitions
    # from non-bundled to win the first-seen dedup.
    def _module_sort_key(p: Path) -> tuple[int, int, str]:
        rel = p.relative_to(GENERATED_POC_DIR)
        is_enum = rel.parts[0] == "enums" if len(rel.parts) > 1 else False
        is_bundled = rel.parts[0] == "bundled" if len(rel.parts) > 1 else False
        return (0 if is_enum else 1, 1 if is_bundled else 0, str(p))

    modules = sorted(GENERATED_POC_DIR.rglob("*.py"), key=_module_sort_key)
    modules = [
        m
        for m in modules
        if m.stem != "__init__" and not m.stem.startswith(".")
        # Bundled schemas inline complete task envelopes for validation and
        # SDK-internal use. They duplicate the public non-bundled models and
        # can contain enormous inline unions that Pydantic refuses to build
        # when imported eagerly through _generated. Keep the files on disk,
        # but do not re-export bundled copies as public SDK types.
        and m.relative_to(GENERATED_POC_DIR).parts[0] != "bundled"
    ]

    print(f"Found {len(modules)} modules to consolidate")

    # Build import statements and collect all exports
    # Track which module first defined each export name
    export_to_module: dict[str, str] = {}
    import_lines = []
    all_exports = set()
    collisions = []

    # Special handling for known collisions
    # We need BOTH versions of these types available, so import them with qualified names
    known_collisions = KNOWN_COLLISIONS

    # Record every module that defines each name so the build guard can detect
    # name collisions independently of which module wins the bare-name slot.
    # A name in >1 module is a collision regardless of whether it resolves via
    # first-seen or stem-preference order.
    name_to_modules: dict[str, set[str]] = {}

    special_imports = []
    collision_modules_seen: dict[str, set[str]] = {name: set() for name in known_collisions}

    def _stem_matches_export(module_stem: str, export_name: str) -> bool:
        """True if the module filename matches the export (snake_case ↔ PascalCase)."""
        return module_stem.replace("_", "").lower() == export_name.lower()

    # First pass: decide which module owns each export name.
    # Canonical class definitions live in files named after the class
    # (e.g. core/format.py defines Format). Prefer those over duplicates
    # elsewhere (bundled copies, enum aliases in unrelated files).
    module_exports: dict[str, set[str]] = {}
    for module_path in modules:
        rel_path = module_path.relative_to(GENERATED_POC_DIR)
        module_parts = list(rel_path.parts[:-1]) + [rel_path.stem]
        module_name = ".".join(module_parts)
        display_name = rel_path.stem

        exports = extract_exports_from_module(module_path)
        if not exports:
            continue
        module_exports[module_name] = exports

        for export_name in exports:
            name_to_modules.setdefault(export_name, set()).add(module_name)

            if export_name in known_collisions and display_name in known_collisions[export_name]:
                collision_modules_seen[export_name].add(module_name)
                # Sentinel: known collisions are only imported via qualified
                # names later, never as a primary export.
                export_to_module[export_name] = "<collision>"
                continue

            if export_name in export_to_module:
                first_module = export_to_module[export_name]
                first_stem = first_module.rsplit(".", 1)[-1]
                if _stem_matches_export(display_name, export_name) and not _stem_matches_export(
                    first_stem, export_name
                ):
                    export_to_module[export_name] = module_name
                    collisions.append(
                        f"  {export_name}: defined in ['{first_module}', '{module_name}'] "
                        f"(preferring {module_name} — stem matches export name)"
                    )
                else:
                    collisions.append(
                        f"  {export_name}: defined in both "
                        f"{first_module} and {module_name} (using {first_module})"
                    )
            else:
                export_to_module[export_name] = module_name

    # Build guard: every name defined in more than one non-bundled module is a
    # collision. KNOWN_COLLISIONS handles its set via qualified imports; the
    # rest are knowingly resolved by sort order and must be snapshotted in the
    # checked-in allowlist. A NEW or CHANGED collision that is in neither set
    # would silently shadow a class for adopters importing from adcp.types — so
    # we fail the build instead of logging a warning (issue #911, Step 1).
    _enforce_collision_allowlist(name_to_modules, set(known_collisions))

    # Second pass: emit one import line per module with only the exports it owns.
    for module_name, exports in module_exports.items():
        owned = {e for e in exports if export_to_module.get(e) == module_name}
        display_name = module_name.rsplit(".", 1)[-1]
        if not owned:
            print(f"  {display_name}: 0 unique exports (all collisions)")
            continue
        print(f"  {display_name}: {len(owned)} exports")
        exports_str = ", ".join(sorted(owned))
        import_line = f"from adcp.types.generated_poc.{module_name} import {exports_str}"
        import_lines.append(import_line)
        all_exports.update(owned)

    # Generate special imports for all known collisions
    for type_name, modules_seen in collision_modules_seen.items():
        if not modules_seen:
            continue
        collisions.append(
            f"  {type_name}: defined in {sorted(modules_seen)} (all exported with qualified names)"
        )
        for module_name in sorted(modules_seen):
            # Non-bundled versions use the stem as the alias suffix
            # (_PackageFromGetMediaBuysResponse). Bundled versions prepend
            # "Bundled<Subdir>" so the same filename existing under both
            # bundled/creative/ and bundled/media_buy/ produces distinct
            # qualified names (otherwise the duplicate triggers a mypy
            # incompatible-import error at import time).
            parts = module_name.split(".")
            stem = parts[-1].replace("_", " ").title().replace(" ", "")
            if parts[0] == "bundled" and len(parts) >= 3:
                subdir = parts[1].replace("_", " ").title().replace(" ", "")
                prefix = f"Bundled{subdir}"
            elif parts[0] == "bundled":
                prefix = "Bundled"
            else:
                prefix = ""
            qualified_name = f"_{type_name}From{prefix}{stem}"
            import_str = (
                f"from adcp.types.generated_poc.{module_name}"
                f" import {type_name} as {qualified_name}"
            )
            special_imports.append(import_str)
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
        "Use 'from adcp import Type' or 'from adcp.types import Type' instead.",
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
                "# Special imports for name collisions"
                " (qualified names for types defined in multiple modules)",
            ]
        )
        lines.extend(special_imports)

    if {"AuthorizedAgents", "AuthorizedAgents6"}.issubset(all_exports):
        lines.extend(
            [
                "",
                "# Backward-compatible adagents authorization variant numbering",
                "AuthorizedAgentsUnion = AuthorizedAgents",
                "AuthorizedAgents = AuthorizedAgents1  # type: ignore[misc,assignment]",
                "AuthorizedAgents1 = AuthorizedAgents2  # type: ignore[misc,assignment]",
                "AuthorizedAgents2 = AuthorizedAgents3  # type: ignore[misc,assignment]",
                "AuthorizedAgents3 = AuthorizedAgents4  # type: ignore[misc,assignment]",
                "AuthorizedAgents4 = AuthorizedAgents5  # type: ignore[misc,assignment]",
                "AuthorizedAgents5 = AuthorizedAgents6  # type: ignore[misc,assignment]",
            ]
        )
        all_exports.add("AuthorizedAgentsUnion")
    if {"CreativeAsset", "CreativeAsset1"}.issubset(all_exports):
        lines.extend(
            [
                "",
                "# Backward-compatible concrete creative asset class for subclassing",
                "CreativeAssetUnion = CreativeAsset",
                "CreativeAsset = CreativeAsset1  # type: ignore[misc,assignment]",
            ]
        )
        all_exports.add("CreativeAssetUnion")
    if {"CreativeManifest", "CreativeManifest1"}.issubset(all_exports):
        lines.extend(
            [
                "",
                "# Backward-compatible concrete creative manifest class for direct construction",
                "CreativeManifestUnion = CreativeManifest",
                "CreativeManifest = CreativeManifest1  # type: ignore[misc,assignment]",
            ]
        )
        all_exports.add("CreativeManifestUnion")

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
    # AdCP 3.1 RC renamed the signals-domain enum to SignalAvailabilityType.
    # Keep the historical public SignalCatalogType name as a compatibility alias.
    if "SignalCatalogType" not in all_exports and "SignalAvailabilityType" in all_exports:
        aliases["SignalCatalogType"] = "SignalAvailabilityType"
    # AdCP 3.1 beta 3 collapsed many single-shape response schemas from
    # RootModel union variants (FooResponse1/FooResponse2) to one concrete
    # FooResponse model. Keep the old numbered names as aliases when the
    # upstream generator no longer emits them so legacy imports continue to
    # work while resolving to the beta 3 shape.
    response_arm_aliases = {
        "AcquireRightsResponse1": "AcquireRightsResponse",
        "AcquireRightsResponse2": "AcquireRightsResponse",
        "AcquireRightsResponse3": "AcquireRightsResponse",
        "AcquireRightsResponse4": "AcquireRightsResponse",
        "ActivateSignalResponse1": "ActivateSignalResponse",
        "ActivateSignalResponse2": "ActivateSignalResponse",
        "BuildCreativeResponse1": "BuildCreativeResponse",
        "BuildCreativeResponse2": "BuildCreativeResponse",
        "CalibrateContentResponse1": "CalibrateContentResponse",
        "CalibrateContentResponse2": "CalibrateContentResponse",
        "ComplyTestControllerResponse1": "ComplyTestControllerResponse",
        "ComplyTestControllerResponse2": "ComplyTestControllerResponse",
        "ComplyTestControllerResponse3": "ComplyTestControllerResponse",
        "ComplyTestControllerResponse4": "ComplyTestControllerResponse",
        "CreateContentStandardsResponse1": "CreateContentStandardsResponse",
        "CreateContentStandardsResponse2": "CreateContentStandardsResponse",
        "CreateMediaBuyResponse1": "CreateMediaBuyResponse",
        "CreateMediaBuyResponse2": "CreateMediaBuyResponse",
        "CreateMediaBuyResponse3": "CreateMediaBuyResponse",
        "GetAccountFinancialsResponse1": "GetAccountFinancialsResponse",
        "GetAccountFinancialsResponse2": "GetAccountFinancialsResponse",
        "GetBrandIdentityResponse1": "GetBrandIdentityResponse",
        "GetBrandIdentityResponse2": "GetBrandIdentityResponse",
        "GetContentStandardsResponse1": "GetContentStandardsResponse",
        "GetContentStandardsResponse2": "GetContentStandardsResponse",
        "GetCreativeFeaturesResponse1": "GetCreativeFeaturesResponse",
        "GetCreativeFeaturesResponse2": "GetCreativeFeaturesResponse",
        "GetMediaBuyArtifactsResponse1": "GetMediaBuyArtifactsResponse",
        "GetMediaBuyArtifactsResponse2": "GetMediaBuyArtifactsResponse",
        "GetRightsResponse1": "GetRightsResponse",
        "GetRightsResponse2": "GetRightsResponse",
        "ListContentStandardsResponse1": "ListContentStandardsResponse",
        "ListContentStandardsResponse2": "ListContentStandardsResponse",
        "LogEventResponse1": "LogEventResponse",
        "LogEventResponse2": "LogEventResponse",
        "PreviewCreativeResponse1": "PreviewCreativeResponse",
        "PreviewCreativeResponse2": "PreviewCreativeResponse",
        "PreviewCreativeResponse3": "PreviewCreativeResponse",
        "ProvidePerformanceFeedbackResponse1": "ProvidePerformanceFeedbackResponse",
        "ProvidePerformanceFeedbackResponse2": "ProvidePerformanceFeedbackResponse",
        "SyncAccountsResponse1": "SyncAccountsResponse",
        "SyncAccountsResponse2": "SyncAccountsResponse",
        "SyncAudiencesResponse1": "SyncAudiencesResponse",
        "SyncAudiencesResponse2": "SyncAudiencesResponse",
        "SyncCatalogsResponse1": "SyncCatalogsResponse",
        "SyncCatalogsResponse2": "SyncCatalogsResponse",
        "SyncCreativesResponse1": "SyncCreativesResponse",
        "SyncCreativesResponse2": "SyncCreativesResponse",
        "SyncEventSourcesResponse1": "SyncEventSourcesResponse",
        "SyncEventSourcesResponse2": "SyncEventSourcesResponse",
        "UpdateContentStandardsResponse1": "UpdateContentStandardsResponse",
        "UpdateContentStandardsResponse2": "UpdateContentStandardsResponse",
        "UpdateMediaBuyResponse1": "UpdateMediaBuyResponse",
        "UpdateMediaBuyResponse2": "UpdateMediaBuyResponse",
        "UpdateMediaBuyResponse3": "UpdateMediaBuyResponse",
        "ValidateContentDeliveryResponse1": "ValidateContentDeliveryResponse",
        "ValidateContentDeliveryResponse2": "ValidateContentDeliveryResponse",
    }
    for alias, target in response_arm_aliases.items():
        if alias not in all_exports and target in all_exports:
            aliases[alias] = target
    # The beta 3 product schema is a oneOf over two concrete product shapes.
    # Preserve the historical public Product class as the first concrete model
    # so adopters can keep subclassing it for internal-only fields.
    if "Product" in all_exports and "Product1" in all_exports:
        aliases["Product"] = "Product1"

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
    # No backward-compat stubs. The SDK surface matches the spec directly.
    # Removed types (BrandManifest, PromotedOfferings, DeliverTo, Pricing,
    # FormatCategory, PackageStatus, etc.) are documented in
    # MIGRATION_v3_to_v4.md.

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
    rebuild_candidates = [
        "CreativeManifest",
        "PreviewCreativeRequest1",
        "PreviewCreativeRequest2",
    ]
    rebuild_types = [t for t in rebuild_candidates if t in all_exports]

    rebuild_lines = [
        "",
        "# Rebuild models with forward references",
        "# This must happen AFTER all imports to resolve forward reference chains",
        "",
        "# Import individual modules needed for rebuilding",
        "from adcp.types import generated_poc  # noqa: F401",
        "",
        "# Rebuild models that reference other models via forward refs",
        "# Note: only call model_rebuild() on actual classes, not Union type aliases",
    ]
    for t in rebuild_types:
        rebuild_lines.append(f"{t}.model_rebuild()")
    rebuild_lines.append("")
    lines.extend(rebuild_lines)

    content = "\n".join(lines)
    # Product is generated as a RootModel union, but the SDK's public Product
    # export intentionally points at the concrete first arm for subclassing.
    # Avoid importing the RootModel so mypy sees the later compatibility
    # assignment as the first public Product binding, not a class redefinition.
    content = content.replace(
        "    Product,\n    Product1,\n",
        "    Product1,\n",
    )
    return content


def main():
    """Generate consolidated exports file."""
    if "--update-allowlist" in sys.argv:
        if not GENERATED_POC_DIR.exists():
            print(f"Error: {GENERATED_POC_DIR} does not exist")
            return 1
        return update_collision_allowlist()

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
