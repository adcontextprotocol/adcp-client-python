"""The ``NoExplicitNull`` post-generation rewrite reproduces the generator's layout.

``point_omittable_fields_at_no_explicit_null`` removes the
``json_schema_extra={'x-adcp-no-explicit-null': True}`` keyword the generator
emitted for a marked property and appends ``NoExplicitNull`` to the field's
``Annotated`` metadata, re-rendering the statement the way black laid it out.
Every later fix reads that text, so the layout has to be the generator's own:
with the marker suppressed the rewrite must give back exactly what the
generator writes for the same schema without the mark.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from scripts import post_generate_fixes as fixes

MARK = fixes._NO_EXPLICIT_NULL_EXTRA


def _rewrite(source: str, marker: str | None = fixes._NO_EXPLICIT_NULL_MARKER) -> tuple[str, int]:
    rewritten, count = fixes._rewrite_no_explicit_null_marks(textwrap.dedent(source), marker)
    ast.parse(rewritten)
    return rewritten, count


def test_width_follows_the_repository_black_configuration() -> None:
    assert fixes._GENERATED_LINE_WIDTH == 100


def test_mark_alone_becomes_the_marker_on_one_line() -> None:
    source = f"""\
    class X(AdCPBaseModel):
        ref: Annotated[R | None, Field({MARK})] = None
    """
    rewritten, count = _rewrite(source)
    assert count == 1
    assert "    ref: Annotated[R | None, NoExplicitNull] = None\n" in rewritten


def test_mark_beside_other_arguments_is_removed_and_the_marker_appended() -> None:
    source = f"""\
    class X(AdCPBaseModel):
        creatives: Annotated[
            list[Creative] | None,
            Field(
                description='Array of creative assets to sync (create or update)',
                {MARK},
                max_length=100,
                min_length=1,
            ),
        ] = None
    """
    rewritten, count = _rewrite(source)
    assert count == 1
    assert rewritten == textwrap.dedent("""\
    class X(AdCPBaseModel):
        creatives: Annotated[
            list[Creative] | None,
            Field(
                description='Array of creative assets to sync (create or update)',
                max_length=100,
                min_length=1,
            ),
            NoExplicitNull,
        ] = None
    """)


def test_an_annotation_that_already_refuses_none_only_loses_the_keyword() -> None:
    description = "Set to true to cancel the media buy. " * 3
    source = f"""\
    class X(AdCPBaseModel):
        status: Annotated[Literal['removed'], Field({MARK})] = 'removed'
        canceled: Annotated[
            Literal[True],
            Field(
                description='{description}',
                {MARK},
            ),
        ] = True
    """
    rewritten, count = _rewrite(source)
    assert count == 0
    assert "NoExplicitNull" not in rewritten
    assert "    status: Literal['removed'] = 'removed'\n" in rewritten
    # A single long argument is kept on one inner line with no trailing comma,
    # which is the form ``_CANCELED_FIELD_RE`` matches.
    assert fixes._CANCELED_FIELD_RE.search(rewritten) is not None


def test_a_parenthesised_default_is_read_back_and_re_split_only_when_needed() -> None:
    source = f"""\
    class X(AdCPBaseModel):
        layout: Annotated[Layout | None, Field({MARK})] = (
            Layout.vertical
        )
    """
    rewritten, _count = _rewrite(source, marker=None)
    assert rewritten == textwrap.dedent("""\
    class X(AdCPBaseModel):
        layout: Layout | None = Layout.vertical
    """)


def test_black_forms_are_chosen_by_width() -> None:
    long_description = "x" * 30
    source = f"""\
    class X(AdCPBaseModel):
        short: Annotated[StrictBool | None, Field(description='Short one.', {MARK})] = False
        longer_name_here: Annotated[
            StrictBool | None, Field(description='{long_description}', {MARK})
        ] = False
    """
    rewritten, count = _rewrite(source)
    assert count == 2
    lines = rewritten.splitlines()
    short_field = "Annotated[StrictBool | None, Field(description='Short one.'), NoExplicitNull]"
    assert f"    short: {short_field} = False" in lines
    # Too wide for one line and the head does not fit either: the subscript is
    # split, with its content hugged on one inner line.
    hugged = f"StrictBool | None, Field(description='{long_description}'), NoExplicitNull"
    assert "    longer_name_here: Annotated[" in lines
    assert f"        {hugged}" in lines
    assert "    ] = False" in lines
    assert all(len(line) <= fixes._GENERATED_LINE_WIDTH for line in lines)


def test_marker_suppressed_gives_back_the_unmarked_rendering() -> None:
    source = f"""\
    class X(AdCPBaseModel):
        a: Annotated[str | None, Field({MARK})] = None
        b: Annotated[str | None, Field(description='kept', {MARK})] = None
        c: Annotated[str | None, Field({MARK}, max_length=3)] = None
    """
    rewritten, count = _rewrite(source, marker=None)
    assert count == 0
    assert rewritten == textwrap.dedent("""\
    class X(AdCPBaseModel):
        a: str | None = None
        b: Annotated[str | None, Field(description='kept')] = None
        c: Annotated[str | None, Field(max_length=3)] = None
    """)


@pytest.mark.parametrize(
    "form", ["Field(json_schema_extra={'x-adcp-no-explicit-null': True, 'other': 1})"]
)
def test_a_json_schema_extra_that_is_not_only_the_mark_is_left_alone(form: str) -> None:
    source = f"""\
    class X(AdCPBaseModel):
        a: Annotated[str | None, {form}] = None
    """
    rewritten, count = _rewrite(source)
    assert count == 0
    assert form in rewritten


def test_non_ascii_text_before_the_mark_does_not_shift_the_edit() -> None:
    """``ast`` columns are UTF-8 byte offsets; an em dash earlier on the line must not
    corrupt the rewrite (a whole-tree run caught this once)."""
    source = f"""\
    class X(AdCPBaseModel):
        a: Annotated[str | None, Field(description='Explicit choice — True', {MARK})] = None
        b: Annotated[
            str | None,
            Field(description='Irreversible — canceled buys stop. Ünïcode.', {MARK}),
        ] = None
    """
    rewritten, count = _rewrite(source)
    assert count == 2
    assert "Field(description='Explicit choice — True'), NoExplicitNull" in rewritten
    assert (
        "Ünïcode.'), NoExplicitNull" in rewritten
        or "Ünïcode.'),\n        NoExplicitNull" in rewritten
    )
