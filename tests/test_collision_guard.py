"""Tests for the consolidate-step reachability guard (issues #911, #1080).

`consolidate_exports.py` flattens every `generated_poc/` module into a single
namespace. When the same bare type name is declared in more than one module,
one class wins that name in `_generated` and the others would be unreachable.
The public mirror under `adcp.types.domains` answers that: one module per
schema, re-exporting what that schema declares, so every variant has a path and
nothing is renamed.

These tests assert the mirror covers every generated class, that the domain
roots bind exactly the unambiguous names, and that the guard raises when a
class would be left unreachable.
"""

from __future__ import annotations

import collections

import pytest

from scripts.consolidate_exports import (
    _enforce_every_class_is_reachable,
    _mirror_relative_paths,
    colliding_names,
    exports_for_public_consolidation,
    extract_exports_from_module,
    scan_declared_names,
    schema_domain,
    unambiguous_domain_bindings,
)


def test_a_domain_namespace_cannot_split_a_name_its_own_domain_declares_twice():
    """The measurement that chose the depth: a domain is still one namespace."""
    declared = scan_declared_names()
    collisions = colliding_names(declared)
    assert collisions, "the generated tree has no colliding names — guard is vacuous"

    solvable, unsolvable = 0, 0
    for modules in collisions.values():
        per_domain = [schema_domain(m) for m in modules]
        if len(set(per_domain)) == len(per_domain):
            solvable += 1
        else:
            unsolvable += 1
    # A domain root handles the names no single domain declares twice; the rest
    # need the schema's own module, which is why the mirror exists.
    assert solvable > 0 and unsolvable > 0, (solvable, unsolvable)
    assert solvable + unsolvable == len(collisions)


def test_core_declares_nine_units_and_no_domain_namespace_could_hold_them():
    """The concrete case, pinned so the mirror's depth is not mistaken for noise."""
    declared = scan_declared_names()
    units = {m for m in declared["Unit"] if schema_domain(m) == "core"}
    assert len(units) == 9, sorted(units)
    assert "Unit" not in unambiguous_domain_bindings(declared)["core"]


def test_the_mirror_covers_every_declared_pair_exactly_once():
    """One public module per schema, re-exporting what that schema declares."""
    declared = scan_declared_names()
    mirrored = collections.Counter()
    for rel in _mirror_relative_paths():
        module_name = ".".join([*rel.parts[:-1], rel.stem])
        for name in extract_exports_from_module(_mirror_path(rel)):
            mirrored[(module_name, name)] += 1

    expected = {
        (module_name, type_name)
        for type_name, modules in declared.items()
        for module_name in modules
    }
    assert set(mirrored) == expected
    assert all(count == 1 for count in mirrored.values())


def _mirror_path(rel):
    from scripts.consolidate_exports import GENERATED_POC_DIR

    return GENERATED_POC_DIR / rel


def test_a_domain_root_binds_the_unambiguous_names_and_renames_nothing():
    """No invented name anywhere: a root key is the name codegen gave the class."""
    declared = scan_declared_names()
    bindings = unambiguous_domain_bindings(declared)
    for domain, rows in bindings.items():
        for type_name, module_name in rows.items():
            assert schema_domain(module_name) == domain
            assert declared[type_name] >= {module_name}
            # One declaring module inside this domain — that is what makes the
            # bare name unambiguous here.
            assert len([m for m in declared[type_name] if schema_domain(m) == domain]) == 1


def test_the_unfiltered_scan_is_what_reaches_brand_discovery():
    """The filtered scan suppresses that module whole, so it had no public path.

    ``exports_for_public_consolidation`` keeps an aggregate schema's inlined
    copies out of the flat namespace. Three of the four aggregates keep their
    own root; ``brand_discovery`` keeps nothing, so reading the domains layer
    off the filtered scan left its own types unreachable.
    """
    from scripts.consolidate_exports import GENERATED_POC_DIR

    path = GENERATED_POC_DIR / "brand_discovery.py"
    assert exports_for_public_consolidation(path) == set()
    assert "Brand" in extract_exports_from_module(path)
    assert "brand_discovery" in scan_declared_names()["Brand"]


def test_current_tree_leaves_no_class_unreachable():
    """Guard passes on the generated tree as consolidated today."""
    # Must not raise.
    _enforce_every_class_is_reachable(scan_declared_names())


def test_a_declared_class_the_mirror_does_not_carry_fails_the_build():
    """A pair with no mirror export fails the consolidate step."""
    declared = scan_declared_names()
    declared["WidgetGuardSentinel"] = {"core.widget_a"}

    with pytest.raises(ValueError) as excinfo:
        _enforce_every_class_is_reachable(declared)

    message = str(excinfo.value)
    assert "core.widget_a.WidgetGuardSentinel" in message
    assert "reachable under no public name" in message
    assert "adcp.types.domains.<domain>.<schema>" in message


# ---------------------------------------------------------------------------
# Aggregate schemas that inline private copies of a $ref'd schema
# ---------------------------------------------------------------------------
#
# ``card-asset.json`` and ``macro-declaration.json`` ``$ref`` other schemas.
# Whether datamodel-code-generator emits an import or inlines a private copy
# of the referenced graph depends on which module it reaches first, and that
# traversal order shifts whenever the bundle gains or loses a schema — see the
# codegen-instability note in CLAUDE.md.
#
# AdCP 3.2.0-rc.2 added new schemas (sync_reporting_status, consumer status,
# forecast rate range) with no change at all to card-asset.json or
# macro-declaration.json, and the reshuffled traversal made both modules start
# inlining. That produced 12 duplicate public type names for 12 wire types that
# already had canonical homes.
#
# ``exports_for_public_consolidation`` keeps those private copies out of the
# public namespace, exactly as it already does for ``asset_union`` and
# ``coordinated_placements``. These tests pin that behavior against a synthetic
# inlined module, so the guard holds whether or not the pinned bundle currently
# triggers the inlining.

_INLINED_CARD_ASSET = """
from adcp.types.base import AdCPBaseModel


class AiTool(AdCPBaseModel):
    name: str


class C2pa(AdCPBaseModel):
    manifest: str


class EmbeddedProvenanceItem(AdCPBaseModel):
    kind: str


class RenderGuidance(AdCPBaseModel):
    hint: str


class VerificationItem(AdCPBaseModel):
    result: str


class Watermark(AdCPBaseModel):
    media: str


class Provenance(AdCPBaseModel):
    declared_by: str


class CardAsset(AdCPBaseModel):
    asset_type: str
"""

_INLINED_MACRO_DECLARATION = """
from enum import StrEnum

from adcp.types.base import AdCPBaseModel


class MacroMappingStatus(StrEnum):
    mapped = "mapped"


class UniversalMacro(StrEnum):
    device_id = "DEVICE_ID"


class MacroProcessingOperation(StrEnum):
    resolve_value = "resolve_value"


class MacroTranslationTarget(AdCPBaseModel):
    token: str


class MacroValueContext(StrEnum):
    url = "url"


class MacroEncoding(AdCPBaseModel):
    kind: str


class MacroDeclaration(AdCPBaseModel):
    token: str
"""


@pytest.mark.parametrize(
    ("relative_path", "source", "root", "inlined"),
    [
        (
            "core/assets/card_asset.py",
            _INLINED_CARD_ASSET,
            "CardAsset",
            {
                "AiTool",
                "C2pa",
                "EmbeddedProvenanceItem",
                "RenderGuidance",
                "VerificationItem",
                "Watermark",
                "Provenance",
            },
        ),
        (
            "core/macro_declaration.py",
            _INLINED_MACRO_DECLARATION,
            "MacroDeclaration",
            {
                "MacroEncoding",
                "MacroMappingStatus",
                "MacroProcessingOperation",
                "MacroTranslationTarget",
                "MacroValueContext",
                "UniversalMacro",
            },
        ),
    ],
    ids=["card_asset", "macro_declaration"],
)
def test_aggregate_modules_export_only_their_root(
    tmp_path, monkeypatch, relative_path, source, root, inlined
):
    """An inlined private copy must not reach the public namespace.

    Each inlined class is a copy of a wire type whose canonical definition
    lives in its own module (``core/provenance.py``, ``enums/universal_macro.py``
    and friends). Exporting the copy too would put two classes for one wire
    type in ``adcp.types`` and let traversal order pick which one an adopter
    gets.
    """
    module_path = tmp_path / relative_path
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(source)
    monkeypatch.setattr("scripts.consolidate_exports.GENERATED_POC_DIR", tmp_path)

    exports = exports_for_public_consolidation(module_path)

    assert exports == {root}
    assert not (exports & inlined), "inlined private copies must stay out of the namespace"
    # Sanity: without the suppression the raw extractor does see them, so this
    # test would fail loudly if the special case were dropped.
    assert inlined <= extract_exports_from_module(module_path)
