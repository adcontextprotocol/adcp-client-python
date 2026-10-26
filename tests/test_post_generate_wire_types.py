"""``preserve_wire_text_on_normalizing_fields`` renames references, not prose (#1610).

The pass points every generated ``AwareDatetime`` / ``AnyUrl`` / ``UUID`` /
``EmailStr`` field at its wire-preserving type. A field description that
says "Use a fresh UUID v4" must keep its wording, and ``uuid.UUID`` as an
attribute is not a reference to the imported name.
"""

from __future__ import annotations

import textwrap

from scripts import post_generate_fixes as fixes

_SOURCE = textwrap.dedent('''
    from uuid import UUID

    from pydantic import AnyUrl, AwareDatetime, Field


    class Model(AdCPBaseModel):
        """A UUID identifies it; an AnyUrl locates it."""

        key: Annotated[UUID, Field(description="Use a fresh UUID v4 for each request.")]
        url: AnyUrl | None = None
        when: AwareDatetime
        other = uuid.UUID
    ''')


def test_identifier_tokens_are_renamed_and_strings_are_not() -> None:
    renamed = fixes._rename_code_names(
        _SOURCE, {"UUID": "WireUuid", "AnyUrl": "WireAnyUrl", "AwareDatetime": "WireDatetime"}
    )
    assert "key: Annotated[WireUuid, Field(" in renamed
    assert "url: WireAnyUrl | None = None" in renamed
    assert "when: WireDatetime" in renamed
    assert "from uuid import WireUuid" in renamed  # the import line is a reference too
    assert 'description="Use a fresh UUID v4 for each request."' in renamed
    assert '"""A UUID identifies it; an AnyUrl locates it."""' in renamed
    assert "other = uuid.UUID" in renamed


def test_every_format_type_the_generator_renders_has_a_replacement() -> None:
    assert dict(fixes._WIRE_PRESERVING_TYPES) == {
        ("pydantic", "AwareDatetime"): "WireDatetime",
        ("pydantic", "AnyUrl"): "WireAnyUrl",
        ("pydantic", "EmailStr"): "WireEmail",
        ("uuid", "UUID"): "WireUuid",
    }
