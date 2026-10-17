#!/usr/bin/env python3
"""
Create the consolidated export files derived from the generated domain tree.

``adcp/types/domains/`` is where datamodel-code-generator defines the classes, so
this script reads the public tree and writes the namespaces derived from it:

* ``_generated.py`` — every public generated type in one namespace. A bare type
  name defined by several generated modules resolves to one winner here.
* ``domains/<domain>/__init__.py`` — the domain root, binding the names that
  domain declares exactly once. No class is reachable under zero names.
* ``domains/__init__.py`` — the ``DOMAINS`` roster.
* ``error_details.py`` — the ``error-details/*.json`` model family plus the
  transitive closure of its field types, derived from the generated package.

It writes no per-schema module: the generated module for a schema IS its public
module, so there is nothing to mirror.

Two build guards replace the former checked-in collision allowlist: the consolidate
step fails when a generated public class is reachable under no name, and when two
(name, module) pairs want the same qualified name. Both are properties of the tree,
so a schema addition cannot reopen the gap. See issues #911 and #1080.

Usage:
    python scripts/consolidate_exports.py
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import pkgutil
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import get_args

DOMAINS_DIR = Path(__file__).parent.parent / "src" / "adcp" / "types" / "domains"
OUTPUT_FILE = Path(__file__).parent.parent / "src" / "adcp" / "types" / "_generated.py"
COLLISION_REPORT_FILE = Path(__file__).parent.parent / "docs" / "shared-type-names.md"
ERROR_DETAILS_FILE = Path(__file__).parent.parent / "src" / "adcp" / "types" / "error_details.py"

_GENERATION_DATE_RE = re.compile(r"^Generation date: .+$", re.MULTILINE)

# Bare names this module refuses to bind to one winner in ``_generated``, each
# listed with the module stems that define it. ``aliases.py`` imports the
# ``_<Name>From<Stem>`` private exports these produce and gives them semantic
# public names.
#
# Reachability does NOT depend on this table: the ``domains/`` modules carry
# every variant of every colliding name, derived from the module tree. A name
# belongs here only when the bare slot in ``_generated`` must stay unbound.
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
    # Trusted Match uses TmpxMacro for two different wire shapes:
    # provider_registration defines the registered macro name as a string
    # RootModel, while identity_match_response defines emitted macro/value
    # pairs. Export both and expose semantic aliases from aliases.py.
    "TmpxMacro": {"identity_match_response", "provider_registration"},
    # Beta.3 rendering schemas introduce same-named types for distinct trust
    # domains. Export every variant under a semantic alias instead of letting
    # generated module order choose a public class.
    "Provenance": {"provenance", "reference_renderer"},
    "RenderingOrigin": {"preview_renderer_metadata", "get_adcp_capabilities_response"},
    "Route": {"preview_provider", "get_adcp_capabilities_response"},
    # Beta.4 introduces a request-proposals compatibility result whose
    # product-id wrapper is distinct from the discovery-criteria wrapper.
    # Export both under qualified internal names; aliases.py exposes stable
    # semantic names for adopters.
    "ProductId": {"product_discovery_criteria", "request_proposals_response"},
    # Beta.5 adds the compact proposal budget-guidance shape to the canonical
    # proposal and refine response while retaining the structurally different
    # legacy proposal shape. Export every generated class under an explicit
    # semantic alias instead of allowing module order to select one silently.
    "TotalBudgetGuidance": {
        "canonical_proposal",
        "proposal",
        "refine_proposals_response",
    },
    # Request-signing capability entries and downstream connection
    # requirements use distinct validation constraints despite sharing a
    # generated title. aliases.py exposes both under semantic names.
    "RequiredForItem": {
        "downstream_connection_requirement",
        "get_adcp_capabilities_response",
    },
}


def module_qualifier(module_name: str) -> str:
    """CamelCase the dotted generated-module path: ``core.error`` -> ``CoreError``."""
    return "".join(
        part.replace("_", " ").title().replace(" ", "") for part in module_name.split(".")
    )


def schema_domain(module_name: str) -> str:
    """The schema domain a generated module belongs to: its first path segment.

    ``creative.list_creatives_response`` -> ``creative``. The AdCP bundle is
    organised this way and codegen mirrors it, so the domain is a name the
    schemas already carry rather than one this script invents.
    """
    return module_name.split(".")[0]


def qualified_public_name(type_name: str, module_name: str) -> str:
    """The unambiguous name for a type its own domain declares more than once.

    A domain module binds a name plainly when the domain declares it once. When
    several of the domain's modules declare it — ``creative`` declares
    ``Creative`` four times — the module path has run out of discriminating
    power and the defining file has to appear in the name.

    The suffix is the module STEM, not the whole path: a stem is unique inside
    its domain for every one of the 728 pairs that need this, so the longer form
    buys nothing. ``generate_domain_exports`` fails the build if that stops
    holding.
    """
    stem = module_name.rsplit(".", 1)[-1]
    return f"{type_name}From{module_qualifier(stem)}"


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


def exports_for_public_consolidation(module_path: Path) -> set[str]:
    """Return the intentional public exports for a generated module.

    Some aggregate/reference schemas necessarily define private copies of
    source models. Re-exporting every nested helper makes those copies shadow
    the canonical module with the same wire type.
    """
    rel_path = module_path.relative_to(DOMAINS_DIR)
    if rel_path == Path("brand_discovery.py"):
        return set()
    if rel_path.parts[:2] == ("core", "async_response_refs"):
        return set()

    exports = extract_exports_from_module(module_path)
    if rel_path == Path("core/assets/asset_union.py"):
        return exports & {"AssetVariant"}
    if rel_path == Path("formats/canonical/coordinated_placements.py"):
        # This aggregate schema inlines the component canonical formats. Keep
        # those nested implementation copies private and export only its root.
        return exports & {"CanonicalFormatCoordinatedPlacements"}
    if rel_path == Path("core/assets/card_asset.py"):
        # card-asset.json ``$ref``s core/provenance.json. Depending on which
        # module the generator visits first, it either emits an import or
        # inlines the whole provenance graph here (AiTool, C2pa,
        # EmbeddedProvenanceItem, RenderGuidance, VerificationItem,
        # Watermark). Those inlined classes are copies of the canonical ones
        # in core/provenance.py, so exporting them would put two classes for
        # one wire type in the public namespace and let traversal order decide
        # which an adopter gets. Export only the root.
        return exports & {"CardAsset"}
    if rel_path == Path("core/macro_declaration.py"):
        # Same shape: macro-declaration.json ``$ref``s the macro enums and
        # core/macro-encoding.json / core/macro-translation-target.json, and
        # the generator inlines copies of them here when it reaches this
        # module first. The canonical definitions live in enums/ and their
        # own core/ modules; keep these copies private and export the root.
        return exports & {"MacroDeclaration"}
    return exports


def referenced_classes(annotation: object) -> Iterator[type]:
    """Yield every class reachable through ``annotation``.

    Unwraps the shapes codegen emits — unions (``A | B`` and ``Union[A, B]``),
    ``Optional``, ``Annotated``, and containers (``list[...]``, ``dict[...]``) —
    so a caller asking "what can this field hold" does not have to know which
    shape it got.

    Three separate guards broke by assuming a generated annotation is a class:
    once when composing roots became ``Annotated`` aliases, once when the
    error-details closure collected roots with ``inspect.isclass``, and once
    when a field type became a union and a ``.model_fields`` access raised
    ``AttributeError``. Everything that walks an annotation goes through here.
    """
    pending = [annotation]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(get_args(current))
        if inspect.isclass(current):
            yield current


def is_public_generated_model(cls: type) -> bool:
    """Whether ``cls`` is a generated model or enum that belongs on the surface.

    A codegen'd alias wraps a private body class. The alias is the public handle
    and the body is an implementation detail, so a private name reached through
    an annotation is not an export — the error-details closure and the guard
    that checks it both have to agree on that, which is why the rule is here.
    """
    return (
        cls.__module__.startswith("adcp.types.domains.")
        and not cls.__name__.startswith("_")
        and (hasattr(cls, "model_fields") or issubclass(cls, Enum))
    )


def generated_models_in(annotation: object) -> Iterator[type]:
    """The generated pydantic models and enums reachable through ``annotation``."""
    for cls in referenced_classes(annotation):
        if is_public_generated_model(cls):
            yield cls


def field_annotations(obj: object) -> list[object]:
    """The field annotations of ``obj``, or what it wraps when it is not a model.

    A generated top-level name is a model, or an alias over one. An alias has no
    ``model_fields``; the thing to walk is the alias itself.
    """
    model_fields = getattr(obj, "model_fields", None)
    if model_fields is None:
        return [obj]
    return [field.annotation for field in model_fields.values()]


def colliding_names(name_to_modules: dict[str, set[str]]) -> dict[str, set[str]]:
    """Every public name defined by two or more non-bundled generated modules."""
    return {n: mods for n, mods in name_to_modules.items() if len(mods) > 1}


def domain_bindings(name_to_modules: dict[str, set[str]]) -> dict[str, dict[str, tuple[str, str]]]:
    """Per domain, the public name of every type that domain declares.

    Returns ``{domain: {public_name: (module, type_name)}}``. A name the domain
    declares once binds plainly, so ``adcp.types.domains.creative.QuerySummary``
    is the one ``creative/list-creatives-response.json`` defines and
    ``adcp.types.domains.core.QuerySummary`` is core's. A name the domain
    declares several times takes ``qualified_public_name``.

    The whole mapping is read off the module tree, so a schema addition extends
    it with no edit here.
    """
    by_domain: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for type_name, modules in name_to_modules.items():
        for module_name in modules:
            domain = schema_domain(module_name)
            by_domain.setdefault(domain, {}).setdefault(type_name, []).append(
                (module_name, type_name)
            )

    bindings: dict[str, dict[str, tuple[str, str]]] = {}
    clashes: dict[str, list[str]] = {}
    for domain, declared in sorted(by_domain.items()):
        rows: dict[str, tuple[str, str]] = {}
        for type_name, pairs in sorted(declared.items()):
            for module_name, _ in sorted(pairs):
                public = (
                    type_name if len(pairs) == 1 else qualified_public_name(type_name, module_name)
                )
                if public in rows:
                    clashes.setdefault(f"{domain}.{public}", [rows[public][0]]).append(module_name)
                    continue
                rows[public] = (module_name, type_name)
        bindings[domain] = rows

    if clashes:
        details = "\n".join(f"  {where}: {sorted(mods)}" for where, mods in sorted(clashes.items()))
        raise ValueError(
            f"{len(clashes)} public name(s) are claimed by more than one generated "
            f"class inside one domain:\n{details}\n\n"
            "qualified_public_name() suffixes the module stem, which is unique "
            "inside a domain for every pair that needs it today. A clash means two "
            "modules in one domain now share a stem — widen the suffix to the "
            "domain-relative path, do not drop a binding.\n"
        )
    return bindings


def _enforce_every_class_is_reachable(name_to_modules: dict[str, set[str]]) -> None:
    """Fail the build when a generated public class is reachable under no name.

    Every ``(module, type)`` pair the tree declares is bound by the mirror
    module for its schema, which is a property of the mirror's construction:
    it re-exports what its module declares, verbatim. The guard re-derives it
    so a change that makes the mirror skip a module fails the build instead of
    leaving an adopter with no public path.

    ``_generated`` and the domain roots are not consulted. ``_generated`` binds
    one class per bare name and the losers were the problem; a domain root binds
    only what its domain declares once.
    """
    mirrored = {
        ".".join([*rel.parts[:-1], rel.stem]): extract_exports_from_module(DOMAINS_DIR / rel)
        for rel in _schema_module_paths()
    }
    unreachable = [
        (module_name, type_name)
        for type_name, modules in name_to_modules.items()
        for module_name in modules
        if type_name not in mirrored.get(module_name, set())
    ]
    if not unreachable:
        return
    details = "\n".join(f"  {module}.{name}" for module, name in sorted(unreachable))
    raise ValueError(
        f"{len(unreachable)} generated public class(es) are reachable under no "
        f"public name:\n{details}\n\n"
        "Every public class in a non-bundled generated module must be importable "
        "from adcp.types.domains.<domain>.<schema>, the mirror of the module that "
        "declares it. A class that is reachable under no name is a model an "
        "adopter cannot construct, which is how the error-details family became "
        "unusable (#1080).\n"
    )


def _scan_name_to_modules() -> dict[str, set[str]]:
    """Map every public name to the set of non-bundled modules that define it."""

    def _module_sort_key(p: Path) -> tuple[int, int, str]:
        rel = p.relative_to(DOMAINS_DIR)
        is_enum = rel.parts[0] == "enums" if len(rel.parts) > 1 else False
        is_bundled = rel.parts[0] == "bundled" if len(rel.parts) > 1 else False
        return (0 if is_enum else 1, 1 if is_bundled else 0, str(p))

    modules = sorted(DOMAINS_DIR.rglob("*.py"), key=_module_sort_key)
    modules = [
        m
        for m in modules
        if m.stem != "__init__"
        and not m.stem.startswith(".")
        and m.relative_to(DOMAINS_DIR).parts[0] != "bundled"
    ]

    name_to_modules: dict[str, set[str]] = {}
    for module_path in modules:
        rel_path = module_path.relative_to(DOMAINS_DIR)
        module_name = ".".join(list(rel_path.parts[:-1]) + [rel_path.stem])
        for export_name in exports_for_public_consolidation(module_path):
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
        rel = p.relative_to(DOMAINS_DIR)
        is_enum = rel.parts[0] == "enums" if len(rel.parts) > 1 else False
        is_bundled = rel.parts[0] == "bundled" if len(rel.parts) > 1 else False
        return (0 if is_enum else 1, 1 if is_bundled else 0, str(p))

    modules = sorted(DOMAINS_DIR.rglob("*.py"), key=_module_sort_key)
    modules = [
        m
        for m in modules
        if m.stem != "__init__" and not m.stem.startswith(".")
        # Bundled schemas inline complete task envelopes for validation and
        # SDK-internal use. They duplicate the public non-bundled models and
        # can contain enormous inline unions that Pydantic refuses to build
        # when imported eagerly through _generated. Keep the files on disk,
        # but do not re-export bundled copies as public SDK types.
        and m.relative_to(DOMAINS_DIR).parts[0] != "bundled"
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
        rel_path = module_path.relative_to(DOMAINS_DIR)
        module_parts = list(rel_path.parts[:-1]) + [rel_path.stem]
        module_name = ".".join(module_parts)
        display_name = rel_path.stem

        exports = exports_for_public_consolidation(module_path)
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

    # Second pass: record which exports each module owns. The import lines come
    # later, because a name that a compatibility alias rebinds is imported under
    # a private name instead of its own (see ``compatibility_bindings``).
    owned_by_module: dict[str, set[str]] = {}
    for module_name, exports in module_exports.items():
        owned = {e for e in exports if export_to_module.get(e) == module_name}
        display_name = module_name.rsplit(".", 1)[-1]
        if not owned:
            print(f"  {display_name}: 0 unique exports (all collisions)")
            continue
        print(f"  {display_name}: {len(owned)} exports")
        owned_by_module[module_name] = owned
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
                f"from adcp.types.domains.{module_name}" f" import {type_name} as {qualified_name}"
            )
            special_imports.append(import_str)
            all_exports.add(qualified_name)

    if collisions:
        print("\n⚠️  Name collisions detected (duplicates skipped):")
        for collision in sorted(collisions):
            print(collision)

    # Backward compatibility aliases (only if source exists).
    #
    # A name on the left here is NOT imported under its own name below: the
    # generated class it would have bound is imported privately instead, so every
    # public binding is a first binding. Rebinding an imported name needs
    # ``# type: ignore[assignment]``, and that suppression is what made the public
    # symbol mean one class to mypy and another at runtime (#1141) — mypy keeps
    # the pre-rebind declaration, so a whole shifted window of names typed one
    # variant off what they held.
    aliases: dict[str, str] = {}
    # AdCP renumbered the adagents authorization variants when it added an branch at
    # the front. Each historical public name keeps the shape it has always had,
    # and the root union gets its own name.
    if {"AuthorizedAgents", "AuthorizedAgents6"}.issubset(all_exports):
        aliases["AuthorizedAgentsUnion"] = "AuthorizedAgents"
        for variant in range(6):
            public = "AuthorizedAgents" if variant == 0 else f"AuthorizedAgents{variant}"
            aliases[public] = f"AuthorizedAgents{variant + 1}"
    # Concrete creative asset / manifest classes for subclassing and direct
    # construction; the root unions get their own names.
    for base in ("CreativeAsset", "CreativeManifest"):
        if {base, f"{base}1"}.issubset(all_exports):
            aliases[f"{base}Union"] = base
            aliases[base] = f"{base}1"
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
    # Reporting-delivery capabilities introduced a nested string RootModel named
    # Transport alongside the existing SI endpoint Transport model. Preserve the
    # established public Transport constructor used by decisioning adopters.
    if "Transport" in all_exports and "Transport1" in all_exports:
        aliases["Transport"] = "Transport1"
    # AdCP 3.1 beta 3 collapsed many single-shape response schemas from
    # RootModel union variants (FooResponse1/FooResponse2) to one concrete
    # FooResponse model. Keep the old numbered names as aliases when the
    # upstream generator no longer emits them so legacy imports continue to
    # work while resolving to the beta 3 shape.
    response_branch_aliases = {
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
    for alias, target in response_branch_aliases.items():
        if alias not in all_exports and target in all_exports:
            aliases[alias] = target
    # The beta 3 product schema is a oneOf over two concrete product shapes.
    # Preserve the historical public Product class as the first concrete model
    # so adopters can keep subclassing it for internal-only fields.
    if "Product" in all_exports and "Product1" in all_exports:
        aliases["Product"] = "Product1"

    # A public name a compatibility alias binds keeps its generated class under a
    # private name, so the alias is the name's first and only binding.
    rebound = {name for name in aliases if name in all_exports}
    private_name = {name: f"_Generated{name}" for name in rebound}

    # Emit one import line per module. Rebound names come in privately.
    for module_name, owned in owned_by_module.items():
        imported = []
        for export_name in sorted(owned):
            if export_name in private_name:
                imported.append(f"{export_name} as {private_name[export_name]}")
            else:
                imported.append(export_name)
        import_lines.append(f"from adcp.types.domains.{module_name} import {', '.join(imported)}")

    all_exports_with_aliases = all_exports | set(aliases)

    alias_lines = []
    if aliases:
        alias_lines.extend(
            [
                "",
                "# Backward compatibility aliases for renamed types",
            ]
        )
        for alias, target in aliases.items():
            alias_lines.append(f"{alias} = {private_name.get(target, target)}")

    # Generate file content
    generation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        '"""INTERNAL: Consolidated generated types.',
        "",
        "DO NOT import from this module directly.",
        "Use 'from adcp import Type' or 'from adcp.types import Type' instead.",
        "",
        "This module consolidates all generated types from adcp.types.domains into a",
        "namespace for convenience. The leading underscore signals this is private API.",
        "",
        "A bare type name that several generated modules define resolves to one class",
        "here. Every variant of such a name is exported from the module for the",
        "schema domain that declares it, under adcp.types.domains.",
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
        "# Import all types from the generated domain modules",
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
    lines.extend(_format_all_block(exports_list))

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
        "from adcp.types import domains  # noqa: F401",
        "",
        "# Rebuild models that reference other models via forward refs",
        "# A name here can be a union alias over a split root's branches, which has",
        "# no model_rebuild of its own. classes_behind is the one derivation of",
        "# what a name stands for: the shared base and each branch, or the model.",
        "from adcp.types._split_root import classes_behind",
    ]
    for t in rebuild_types:
        rebuild_lines.append(f"for _rebuilt in classes_behind({t}):")
        rebuild_lines.append("    _rebuilt.model_rebuild()")
    rebuild_lines.append("")
    lines.extend(rebuild_lines)

    return "\n".join(lines)


def _format_all_block(names: list[str]) -> list[str]:
    """Render ``__all__`` for ``names``, wrapped at 100 columns.

    Each name once: ``__all__`` is a list, and a consumer that counts it rather
    than setting it would count a repeated name twice (#1380).
    """
    names = list(dict.fromkeys(names))
    lines = ["", "# Explicit exports", "__all__ = ["]
    current = "    "
    for i, name in enumerate(names):
        entry = f'"{name}"' + ("," if i < len(names) - 1 else "")
        if len(current + entry + " ") > 100 and current.strip():
            lines.append(current.rstrip())
            current = "    " + entry + " "
        else:
            current += entry + " "
    if current.strip():
        lines.append(current.rstrip())
    lines.append("]")
    lines.append("")
    return lines


def scan_declared_names() -> dict[str, set[str]]:
    """Map every public name to the non-bundled modules that DECLARE it.

    Unlike :func:`_scan_name_to_modules` this applies no public-export filter.
    That filter exists so an aggregate schema's inlined private copies cannot
    shadow the canonical class they were copied from in the flat namespace, and
    the domains layer does not need it twice over: a schema's own module names
    the defining schema and shadows nothing, and a domain root binds only the
    names its domain declares exactly once — so a copy that duplicates a
    canonical name is omitted from the root by that rule alone.

    Using the filtered scan here is what left ``brand_discovery`` with no public
    path at all, since that module is suppressed in full.
    """
    declared: dict[str, set[str]] = {}
    for rel in _schema_module_paths():
        module_name = ".".join([*rel.parts[:-1], rel.stem])
        for name in extract_exports_from_module(DOMAINS_DIR / rel):
            declared.setdefault(name, set()).add(module_name)
    return declared


def unambiguous_domain_bindings(
    name_to_modules: dict[str, set[str]],
) -> dict[str, dict[str, str]]:
    """Per domain, the names that domain declares exactly once.

    Returns ``{domain: {type_name: module}}``. A name a domain declares twice
    has no unambiguous spelling at domain level and is reached through the
    module that declares it — which is the module the generator defined it in.
    Nothing is renamed: a type either keeps the name codegen gave it or is
    imported from its own schema's module.
    """
    per_domain: dict[str, dict[str, list[str]]] = {}
    for type_name, modules in name_to_modules.items():
        for module_name in modules:
            per_domain.setdefault(schema_domain(module_name), {}).setdefault(type_name, []).append(
                module_name
            )
    return {
        domain: {
            type_name: modules[0]
            for type_name, modules in sorted(declared.items())
            if len(modules) == 1
        }
        for domain, declared in sorted(per_domain.items())
    }


def _schema_module_paths() -> list[Path]:
    """Every non-bundled generated module, as a path relative to the tree root.

    These are the modules that DEFINE the public classes. ``core`` declares nine
    different ``Unit`` classes, in ``audience_evidence``, ``canvas_constraint``
    and seven more; the schema layout distinguishes them one level below the
    domain and the generated tree mirrors that layout, so all nine have a public
    path with nothing renamed, invented or mangled::

        from adcp.types.domains.core.audience_evidence import Unit
        from adcp.types.domains.core.canvas_constraint import Unit
    """
    paths = []
    for path in sorted(DOMAINS_DIR.rglob("*.py")):
        rel = path.relative_to(DOMAINS_DIR)
        if path.stem == "__init__" or rel.parts[0] == "bundled":
            continue
        paths.append(rel)
    return paths


#: The runtime half of a lazy domain facade, emitted verbatim into each one.
#:
#: Under ``not TYPE_CHECKING`` so a type checker never sees a module-level
#: ``__getattr__``: one would type every unknown name as ``object`` and silence
#: the missing-attribute error the ``TYPE_CHECKING`` block above exists to keep.
_LAZY_FACADE_RESOLVER = '''
if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {
        name: module for module, names in _DECLARED_BY.items() for name in names
    }

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))
'''


def generate_domain_exports(
    bindings: dict[str, dict[str, str]],
    name_to_modules: dict[str, set[str]],
) -> dict[str, str]:
    """Generate each domain package root, plus the ``domains`` package root.

    Returns ``{"<domain>": content, "__init__": content}``. A domain root binds
    the names that domain declares exactly once — nothing is renamed, and a name
    the domain declares twice is reached through its own schema's module.

    A domain whose only schema sits at the tree root (``adagents``,
    ``brand_discovery``, ``manifest``, ``manifest_schema``) gets no entry: the
    generator defines that schema's classes in ``domains/<domain>.py``, so the
    domain root already IS the definition module and writing one would replace
    it with a re-export of itself.
    """
    generation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    domain_modules: dict[str, set[str]] = {}
    for module_name in {m for mods in name_to_modules.values() for m in mods}:
        domain_modules.setdefault(schema_domain(module_name), set()).add(module_name)

    modules: dict[str, str] = {}
    for domain, rows in sorted(bindings.items()):
        # A schema at the tree root has no submodule of its own — its module
        # path is the domain name — so the generated module at that path is
        # already the domain root and carries everything it declares.
        if domain in {m for mods in name_to_modules.values() for m in mods}:
            continue
        by_module: dict[str, list[str]] = {}
        for type_name, module_name in sorted(rows.items()):
            by_module.setdefault(module_name, []).append(type_name)

        lines = [
            f'"""Types the AdCP ``{domain}`` schemas declare.',
            "",
            "Importing from the domain says which variant you mean, where the flat",
            "``adcp.types`` namespace can only bind one class per name:",
            "",
            f"    from adcp.types.domains.{domain} import <Type>",
            "",
            "A type this domain declares in more than one schema is not here: import",
            "    it from its own schema's module," f" ``adcp.types.domains.{domain}.<schema>``.",
            "Nothing here is renamed.",
            "",
            "This facade is LAZY (PEP 562). It binds no name at import time, because an",
            "eager one makes every leaf of this domain depend on every other leaf: a",
            "submodule import runs this ``__init__``, so one leaf's dependencies become",
            "the whole domain's. That turned a benign submodule-level cycle between",
            "``core`` and ``formats.canonical`` -- two domains whose schemas reference",
            "each other, which Python handles -- into a package-level one that made all",
            "16 modules under ``formats/canonical/`` unimportable on their own.",
        ]
        lines.extend(
            [
                "",
                "Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.",
                f"Generation date: {generation_date}",
                '"""',
                "# ruff: noqa: E501, I001",
                "from __future__ import annotations",
                "",
                "from typing import TYPE_CHECKING",
                "",
                "if TYPE_CHECKING:",
                "    # The whole importable surface, so type checkers and IDEs see it. Runtime",
                "    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd",
                "    # import is still an error rather than ``object``.",
            ]
        )
        for module_name in sorted(by_module):
            lines.append(
                f"    from adcp.types.domains.{module_name} import "
                f"{', '.join(sorted(by_module[module_name]))}"
            )
        lines.extend(
            [
                "",
                "#: Which schema module declares each re-exported name.",
                "#:",
                "#: Resolution is PER NAME, a deliberate divergence from",
                "#: ``adcp.types.__init__``, which routes every name through one ``_eager``",
                "#: module. Importing a whole domain graph on first access can re-enter the",
                "#: cycle this laziness exists to break, when that first access happens while",
                "#: a canonical format module is still initializing. Importing only the one",
                "#: declaring module cannot.",
                "_DECLARED_BY = {",
            ]
        )
        for module_name in sorted(by_module):
            declared = ", ".join(f'"{name}"' for name in sorted(by_module[module_name]))
            lines.append(f'    "{module_name}": ({declared},),')
        lines.append("}")
        lines.extend(_LAZY_FACADE_RESOLVER.splitlines())
        lines.extend(_format_all_block(sorted(rows)))
        modules[domain] = "\n".join(lines)

    root = [
        '"""Public types grouped by the AdCP schema that declares them.',
        "",
        "The AdCP bundle is organised by domain — ``core/``, ``creative/``,",
        "``media_buy/`` and the rest — and by schema within each. Codegen writes this",
        "package in that layout and defines every class here, so a type name more",
        "than one schema declares is unambiguous by path rather than by a mangled name:",
        "",
        "    from adcp.types.domains.creative import QuerySummary",
        "    from adcp.types.domains.core.audience_evidence import Unit",
        "    from adcp.types.domains.core.canvas_constraint import Unit",
        "",
        "A domain root carries the names that domain declares exactly once. For the",
        "rest, import from the schema's own module, as the last two lines do.",
        "",
        "Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.",
        f"Generation date: {generation_date}",
        '"""',
        "from __future__ import annotations",
        "",
        "#: Every domain in this package.",
        "DOMAINS = (",
        *(f'    "{domain}",' for domain in sorted(bindings)),
        ")",
        "",
        '__all__ = ["DOMAINS"]',
        "",
    ]
    modules["__init__"] = "\n".join(root)

    total = sum(len(rows) for rows in bindings.values())
    print(f"  domain roots: {len(bindings)} packages, {total} unambiguous exports")
    return modules


def generate_collision_report(name_to_modules: dict[str, set[str]]) -> str:
    """Generate the scannable list of type names more than one schema declares.

    A flat module of qualified names would be greppable, which was its one real
    convenience. A generated document keeps that without committing a public
    identifier per variant.
    """
    shared = {
        type_name: [f"`adcp.types.domains.{module}.{type_name}`" for module in sorted(modules)]
        for type_name, modules in sorted(name_to_modules.items())
        if len(modules) > 1
    }

    generation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Type names declared by more than one AdCP schema",
        "",
        "AdCP names an inline object after the property that holds it, so several",
        "schemas legitimately declare a class called `QuerySummary` or `Creative`.",
        "`adcp.types` binds one of them per name. Import from the domain module to",
        "name the one you mean.",
        "",
        f"{len(shared)} names, {sum(len(p) for p in shared.values())} variants.",
        "",
        "Auto-generated by `scripts/consolidate_exports.py`. DO NOT EDIT MANUALLY.",
        f"Generation date: {generation_date}",
        "",
        "| Type name | Import one of |",
        "| --- | --- |",
    ]
    for name, paths in sorted(shared.items()):
        lines.append(f"| `{name}` | {' <br> '.join(paths)} |")
    lines.append("")
    print(f"  collision report: {len(shared)} shared names")
    return "\n".join(lines)


def _error_details_closure() -> dict[str, list[str]]:
    """Map each generated module to the error-details names it must export.

    The set is every public top-level name in ``error-details/*`` plus the
    transitive closure of the types those names reference. Issue #1080 asked for
    the family to be importable and was closed by listing 16 names; a model whose
    field types are unreachable still cannot be constructed with typed values,
    and the list went stale on the next schema addition. Walking the generated
    package covers both.

    Roots come from the AST, not from ``inspect.isclass``: a schema whose root
    composes other schemas generates a ``typing.Annotated[...]`` alias rather
    than a class, and an alias is a top-level name an adopter has to be able to
    import like any other. Filtering on ``isclass`` dropped two error-details
    models the moment codegen started emitting them that way.
    """
    package = importlib.import_module("adcp.types.domains.error_details")
    package_dir = Path(package.__path__[0])

    roots: list[object] = []
    declared: dict[str, list[str]] = {}
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        rel = f"error_details.{module_info.name}"
        for name in sorted(extract_exports_from_module(package_dir / f"{module_info.name}.py")):
            obj = getattr(module, name, None)
            if obj is None:
                continue
            declared.setdefault(rel, []).append(name)
            roots.append(obj)

    # ``seen`` holds classes only: a class is what a nested field type is, and
    # what has a module to be exported from. An alias contributes what it wraps.
    seen: set[type] = set()
    queue: list[object] = list(roots)
    while queue:
        obj = queue.pop()
        if inspect.isclass(obj):
            if obj in seen:
                continue
            seen.add(obj)
        for annotation in field_annotations(obj):
            queue.extend(cls for cls in generated_models_in(annotation) if cls not in seen)

    by_module: dict[str, list[str]] = {
        module: list(names) for module, names in sorted(declared.items())
    }
    for cls in seen:
        # A codegen'd alias wraps a private body class; the alias is the public
        # handle and the body is not an export. Reaching one through a field
        # annotation does not make its name public.
        if cls.__name__.startswith("_"):
            continue
        module_name = cls.__module__.removeprefix("adcp.types.domains.")
        if cls.__name__ not in by_module.setdefault(module_name, []):
            by_module[module_name].append(cls.__name__)
    return {module: sorted(names) for module, names in sorted(by_module.items())}


def generate_error_details_exports() -> str:
    """Generate the error-details surface: the models and their field types."""
    closure = _error_details_closure()
    occurrences: dict[str, int] = {}
    for names in closure.values():
        for name in names:
            occurrences[name] = occurrences.get(name, 0) + 1

    exported: list[str] = []
    import_lines: list[str] = []
    for module_name, names in closure.items():
        imported = []
        for name in names:
            # A nested name two error-details schemas both define (``Scope`` is in
            # billing-not-supported and rate-limited) has no unambiguous bare
            # spelling, so it gets only its qualified one. Binding one of them to
            # the bare name is how the wrong class reaches a call site.
            public = name if occurrences[name] == 1 else qualified_public_name(name, module_name)
            imported.append(name if public == name else f"{name} as {public}")
            exported.append(public)
        import_lines.append(f"from adcp.types.domains.{module_name} import {', '.join(imported)}")

    generation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        '"""The AdCP structured error-details models, with their field types.',
        "",
        "``Error.details`` is typed ``dict``, so nothing constrains what a raise site",
        "puts in it. One model per ``error-details/*.json`` schema encodes the required",
        "keys, and this module exports every one of them together with the transitive",
        "closure of their field types — the enums and nested models their annotations",
        "reference — so a seller constructs the payload with typed values:",
        "",
        "    from adcp.types.error_details import SupportedVersion, VersionUnsupportedDetails",
        "",
        "    VersionUnsupportedDetails(",
        '        adcp_version="3.2",',
        '        supported_versions=[SupportedVersion("3.1"), SupportedVersion("3.2")],',
        "    )",
        "",
        "A nested name that two error-details schemas both define carries the",
        "defining file in its name (``ScopeFromRateLimited``) instead of a bare one.",
        "",
        "Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.",
        f"Generation date: {generation_date}",
        '"""',
        "# ruff: noqa: E501, I001",
        "from __future__ import annotations",
        "",
        *import_lines,
    ]
    lines.extend(_format_all_block(sorted(exported)))
    print(f"  error_details: {len(exported)} exports over {len(closure)} modules")
    return "\n".join(lines)


def preserve_generation_date_if_unchanged(previous: str, generated: str) -> str:
    """Keep the prior timestamp when regeneration changed no exported content."""
    previous_without_date = _GENERATION_DATE_RE.sub("Generation date:", previous)
    generated_without_date = _GENERATION_DATE_RE.sub("Generation date:", generated)
    if previous_without_date != generated_without_date:
        return generated

    previous_date = _GENERATION_DATE_RE.search(previous)
    if previous_date is None:
        return generated
    return _GENERATION_DATE_RE.sub(previous_date.group(0), generated, count=1)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DOMAINS_DIR,
        help="generated domain tree to consolidate",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=OUTPUT_FILE,
        help="destination for the consolidated Python module; the derived "
        "domains/ package and error_details.py are written beside it",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="source tree whose ``adcp`` package the error-details derivation imports "
        "(defaults to the installed package)",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=COLLISION_REPORT_FILE,
        help="destination for the shared-type-names markdown report",
    )
    return parser.parse_args(argv)


def _format_with_black(*targets: Path) -> None:
    """Run black over ``targets`` once, not once per file."""
    print("Formatting with black...")
    args = [str(t) for t in targets]
    for command in (
        ["uv", "run", "black", *args, "--quiet"],
        [sys.executable, "-m", "black", *args, "--quiet"],
    ):
        try:
            if subprocess.run(command, capture_output=True, text=True, check=False).returncode == 0:
                print("✓ Formatted with black")
                return
        except FileNotFoundError:
            continue
    print("⚠ Could not format with black (not installed)")


def write_generated_module(path: Path, content: str) -> str:
    """Write ``content`` to ``path``, black-format it, and keep a stable date.

    Returns the file's previous text. The date can only be preserved by
    comparing formatted text against formatted text, so the format runs here.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_content = path.read_text() if path.exists() else ""
    path.write_text(content)
    _format_with_black(path)
    formatted_content = path.read_text()
    stable_content = preserve_generation_date_if_unchanged(previous_content, formatted_content)
    if stable_content != formatted_content:
        path.write_text(stable_content)
    return previous_content


def main(argv: list[str] | None = None):
    """Generate the consolidated namespace, the domain modules and error-details."""
    global DOMAINS_DIR, OUTPUT_FILE, ERROR_DETAILS_FILE, COLLISION_REPORT_FILE

    args = _parse_args(argv)
    # One tree: the input is the generated domain package, and the domain roots
    # are written back into it. ``generate_types.py`` generates into its staging
    # copy and passes that path, so every derived artifact lands beside the
    # models and is installed (or, under ``--check``, compared) as one unit.
    DOMAINS_DIR = args.input_dir.resolve()
    OUTPUT_FILE = args.output_file.resolve()
    ERROR_DETAILS_FILE = OUTPUT_FILE.parent / "error_details.py"
    COLLISION_REPORT_FILE = args.report_file.resolve()
    if args.source_root is not None:
        # ``_error_details_closure`` imports the generated package to walk its
        # annotations. It has to see the tree being consolidated, not whichever
        # ``adcp`` is installed in the environment — otherwise the error-details
        # surface is derived from the previous generation and lags one run behind.
        sys.path.insert(0, str(args.source_root.resolve()))

    print("Generating consolidated exports from the generated domain tree...")

    if not DOMAINS_DIR.exists():
        print(f"Error: {DOMAINS_DIR} does not exist")
        return 1

    content = generate_consolidated_exports()

    # Build guard: ``_generated`` binds one class per bare name, so every variant
    # has to be reachable from its domain module. Both sides are derived from the
    # module tree, and the guard fails when a class is left reachable under no
    # name (issues #911, #1080).
    declared = scan_declared_names()
    _enforce_every_class_is_reachable(declared)
    bindings = unambiguous_domain_bindings(declared)

    write_generated_module(OUTPUT_FILE, content)
    for domain, module_content in generate_domain_exports(bindings, declared).items():
        target = DOMAINS_DIR / ("__init__.py" if domain == "__init__" else f"{domain}/__init__.py")
        write_generated_module(target, module_content)
    write_generated_module(ERROR_DETAILS_FILE, generate_error_details_exports())
    # Not through ``write_generated_module``: black cannot format markdown. The
    # date still has to be preserved or the report churns on every regeneration.
    previous_report = COLLISION_REPORT_FILE.read_text() if COLLISION_REPORT_FILE.exists() else ""
    COLLISION_REPORT_FILE.write_text(
        preserve_generation_date_if_unchanged(previous_report, generate_collision_report(declared))
    )

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
