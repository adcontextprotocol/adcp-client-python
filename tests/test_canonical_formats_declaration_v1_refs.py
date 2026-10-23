"""Every read of a declaration's v1 refs accepts both declaration types.

Two types in this package are called a product format declaration and they name
this field differently:

* the sixteen generated ``ProductFormatDeclaration`` arms declare
  ``v1_format_ref`` as a real field — this is what
  ``adcp.types.domains.core.product.Product.format_options`` holds;
* ``adcp.types.canonical_creative.Format`` exposes ``legacy_format_refs``, a
  property over private state populated from ``v1_format_ref`` at construction.

Every function that reads the refs was written against the second spelling, so
each one raises ``AttributeError`` when handed the first. These tests grade the
entry points against BOTH types, which is the only way the asymmetry shows up.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.canonical_formats import find_declaration_by_v1_format_id
from adcp.canonical_formats.projection import resolve_legacy_format_refs
from adcp.canonical_formats.v1_to_v2 import group_declarations_by_product
from adcp.canonical_formats.v2_to_v1 import project_declaration_to_v1
from adcp.types.canonical_creative import ProductFormatDeclaration as HandWrittenDeclaration
from adcp.types.domains.core.product_format_declaration import (
    ProductFormatDeclaration1 as GeneratedDeclaration,
)
from adcp.types.legacy import LegacyFormatId

AGENT_URL = "https://creative.adcontextprotocol.org"
V1_ID = "display_300x250_image"
DECLARATION_DOCUMENT: dict[str, Any] = {
    "format_kind": "image",
    "format_option_id": "opt1",
    "params": {"width": 300, "height": 250},
    "v1_format_ref": [{"agent_url": AGENT_URL, "id": V1_ID}],
}


def _generated() -> Any:
    return GeneratedDeclaration.model_validate(DECLARATION_DOCUMENT)


def _hand_written() -> Any:
    return HandWrittenDeclaration.model_validate(DECLARATION_DOCUMENT)


#: Both spellings of the same declaration. The ids name the field each one
#: declares, so a failure says which spelling the reader could not handle.
DECLARATION_BUILDERS = [
    pytest.param(_generated, id="generated-v1_format_ref"),
    pytest.param(_hand_written, id="handwritten-legacy_format_refs"),
]


def test_the_two_declaration_types_spell_the_field_differently() -> None:
    """The premise of this module, asserted rather than assumed.

    If a later change unifies the two types, this test fails and the rest of
    the module becomes redundant rather than silently grading one shape twice.
    """
    generated, hand_written = _generated(), _hand_written()

    assert "v1_format_ref" in type(generated).model_fields
    assert not hasattr(generated, "legacy_format_refs")

    assert "v1_format_ref" not in type(hand_written).model_fields
    assert hasattr(hand_written, "legacy_format_refs")


@pytest.mark.parametrize("build", DECLARATION_BUILDERS)
def test_resolve_legacy_format_refs_reads_either_spelling(build: Any) -> None:
    assert [ref.id for ref in resolve_legacy_format_refs(build())] == [V1_ID]


@pytest.mark.parametrize("build", DECLARATION_BUILDERS)
def test_find_declaration_by_v1_format_id_reads_either_spelling(build: Any) -> None:
    declaration = build()
    target = LegacyFormatId.model_validate({"agent_url": AGENT_URL, "id": V1_ID})

    assert find_declaration_by_v1_format_id(target, [declaration]) is declaration


@pytest.mark.parametrize("build", DECLARATION_BUILDERS)
def test_project_declaration_to_v1_reads_either_spelling(build: Any) -> None:
    projected = project_declaration_to_v1(build())

    assert [ref.id for ref in projected.format_ids] == [V1_ID]


@pytest.mark.parametrize("build", DECLARATION_BUILDERS)
def test_group_declarations_by_product_reads_either_spelling(build: Any) -> None:
    declaration = build()

    grouped = group_declarations_by_product([declaration], {"prod_1": [V1_ID]})

    assert grouped == {"prod_1": [declaration]}
