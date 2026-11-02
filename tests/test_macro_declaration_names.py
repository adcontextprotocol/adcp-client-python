"""Macro declaration variants have names keyed on their asset arm (#1783).

AdCP 3.3 gives each asset delivery its own ``macro_declarations`` item shape,
and the generator names the variants by position (``MacroDeclaration``,
``MacroDeclaration15``, ``asset_union.MacroDeclaration6``). The numbers move
when the bundle changes, so the only stable spelling an adopter had was a
number that might point at a different arm after an upgrade. ``aliases.py``
names each variant after the owning arm's ``asset_type`` / ``delivery_type``
literals and ``macro_declaration_model`` resolves one from those literals at
runtime. These tests derive the mapping from the ``AssetVariant`` arms
themselves, so a renumber that silently repoints an alias fails here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import adcp.types as types
from adcp.types import aliases
from adcp.types.aliases import macro_declaration_model, macro_declaration_variants

EXPECTED = {
    ("display_tag", "tag_url"): "TagUrlMacroDeclaration",
    ("display_tag", "inline_markup"): "InlineMarkupMacroDeclaration",
    ("display_tag", "paired_redirect"): "PairedRedirectMacroDeclaration",
    ("vast", "url"): "UrlVastMacroDeclaration",
    ("vast", "inline"): "InlineVastMacroDeclaration",
    ("daast", "url"): "UrlDaastMacroDeclaration",
    ("daast", "inline"): "InlineDaastMacroDeclaration",
    ("url", None): "UrlAssetMacroDeclaration",
    ("pixel_tracker", None): "PixelTrackerMacroDeclaration",
    ("vast_tracker", None): "VastTrackerMacroDeclaration",
    ("daast_tracker", None): "DaastTrackerMacroDeclaration",
}


def test_every_arm_with_macro_declarations_has_a_named_alias() -> None:
    """The arm set and the alias set are the same set; a new arm must be named."""
    assert set(macro_declaration_variants()) == set(EXPECTED)


@pytest.mark.parametrize(("arm", "name"), sorted(EXPECTED.items(), key=str))
def test_the_alias_is_the_arms_macro_declaration_class(
    arm: tuple[str, str | None], name: str
) -> None:
    from_arm = macro_declaration_variants()[arm]
    assert getattr(aliases, name) is from_arm, (name, from_arm.__name__)
    assert getattr(types, name) is from_arm, "the public namespace exports the alias"
    assert macro_declaration_model(*arm) is from_arm


def test_the_variants_are_distinct_classes() -> None:
    classes = list(macro_declaration_variants().values())
    assert len(set(classes)) == len(classes)


def test_an_alias_validates_the_arms_declaration_shape() -> None:
    """The arms differ in ``location.field``; the alias is the arm, not a lookalike."""
    declaration = {
        "declaration_id": "cb",
        "token": "${CACHEBUSTER}",
        "dialect": "adcp",
        "dialect_semantic": "CACHEBUSTING",
        "mapping_status": "verified_universal",
        "operation": "resolve_value",
        "location": {"field": "url", "occurrence": 1, "context": "url_query_value"},
        "encoding": {"kind": "none", "depth": 0},
        "required": True,
        "unavailable_behavior": "preserve",
    }
    parsed = types.TagUrlMacroDeclaration.model_validate(declaration)
    assert parsed.model_dump(mode="json") == declaration

    paired = {**declaration, "location": {**declaration["location"], "field": "clickthrough_url"}}
    assert types.PairedRedirectMacroDeclaration.model_validate(paired).location.field.value == (
        "clickthrough_url"
    )
    with pytest.raises(ValidationError):
        types.TagUrlMacroDeclaration.model_validate(paired)


def test_resolving_a_multi_delivery_asset_type_without_a_delivery_names_the_deliveries() -> None:
    with pytest.raises(KeyError, match=r"'display_tag' has one macro declaration shape per") as e:
        macro_declaration_model("display_tag")
    assert "inline_markup" in str(e.value) and "tag_url" in str(e.value)


def test_resolving_an_unknown_arm_lists_the_arms_that_exist() -> None:
    with pytest.raises(KeyError, match=r"no AssetVariant arm has asset_type='html'"):
        macro_declaration_model("html")
    with pytest.raises(KeyError, match=r"delivery_type='inline'"):
        macro_declaration_model("url", "inline")


def test_the_names_are_in_the_public_surface() -> None:
    for name in EXPECTED.values():
        assert name in types.__all__
        assert name in aliases.__all__
    assert "macro_declaration_model" in aliases.__all__
