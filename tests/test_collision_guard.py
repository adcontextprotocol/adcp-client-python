"""Tests for the consolidate-step name-collision build guard (issue #911, Step 1).

`consolidate_exports.py` flattens every `generated_poc/` module into a single
namespace. When the same bare type name is defined in more than one module, one
class silently shadows the others for adopters importing from `adcp.types`.

The build guard fails the consolidate step for any collision that is neither
handled via qualified imports (`KNOWN_COLLISIONS`) nor recorded in the
checked-in allowlist snapshot. These tests assert the guard passes on the
current tree and raises for a synthetic new collision.
"""

from __future__ import annotations

import pytest

from scripts.consolidate_exports import (
    KNOWN_COLLISIONS,
    _enforce_collision_allowlist,
    _scan_name_to_modules,
    extract_exports_from_module,
    exports_for_public_consolidation,
    load_collision_allowlist,
)


def test_allowlist_snapshot_is_present_and_nonempty():
    """The checked-in allowlist seeds the guard with today's collision set."""
    allowlist = load_collision_allowlist()
    assert allowlist, "collision_allowlist.json is missing or empty"
    # Sanity: a few names called out in issue #911 must be snapshotted.
    for name in ("Creative", "Account", "Authentication", "Sort", "Unit"):
        assert name in allowlist, f"{name} should be in the seeded allowlist"


def test_known_collisions_are_not_in_allowlist():
    """Qualified-import collisions are handled separately, not via the allowlist."""
    allowlist = load_collision_allowlist()
    overlap = set(KNOWN_COLLISIONS) & allowlist
    assert (
        overlap == set()
    ), f"KNOWN_COLLISIONS names must not also be in the allowlist: {sorted(overlap)}"


def test_current_tree_consolidates_cleanly():
    """Guard passes on the current generated tree against the seeded allowlist."""
    name_to_modules = _scan_name_to_modules()
    # Must not raise.
    _enforce_collision_allowlist(name_to_modules, set(KNOWN_COLLISIONS))


def test_allowlist_matches_current_collisions_exactly():
    """The snapshot is neither stale nor padded with non-colliding names.

    Every allowlisted name must still collide in the tree; every collision not
    handled via qualified imports must be in the allowlist.
    """
    name_to_modules = _scan_name_to_modules()
    collisions = {name for name, mods in name_to_modules.items() if len(mods) > 1} - set(
        KNOWN_COLLISIONS
    )
    allowlist = load_collision_allowlist()
    assert allowlist == collisions, (
        "Allowlist drifted from the real collision set. Regenerate with "
        "`python scripts/consolidate_exports.py --update-allowlist`.\n"
        f"  In allowlist but no longer colliding: {sorted(allowlist - collisions)}\n"
        f"  Colliding but missing from allowlist: {sorted(collisions - allowlist)}"
    )


def test_new_collision_not_in_allowlist_raises():
    """A new bare name in two modules, absent from the allowlist, fails the build."""
    name_to_modules = _scan_name_to_modules()
    # Synthetic collision: a name defined in two modules that is not in the
    # allowlist and not a known collision.
    synthetic = "WidgetCollisionGuardSentinel"
    assert synthetic not in load_collision_allowlist()
    name_to_modules[synthetic] = {"core.widget_a", "core.widget_b"}

    with pytest.raises(ValueError) as excinfo:
        _enforce_collision_allowlist(name_to_modules, set(KNOWN_COLLISIONS))

    message = str(excinfo.value)
    assert synthetic in message
    assert "not in the checked-in allowlist" in message
    # The remediation guidance must tell a contributor what to do.
    assert "aliases.py" in message
    assert "--update-allowlist" in message
    assert "KNOWN_COLLISIONS" in message


def test_single_definition_name_does_not_trip_guard():
    """A name defined in exactly one module is not a collision."""
    name_to_modules = {"SoloUniqueGuardSentinel": {"core.solo"}}
    # Must not raise.
    _enforce_collision_allowlist(name_to_modules, set(KNOWN_COLLISIONS))


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
