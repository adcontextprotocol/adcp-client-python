"""Runtime tests for :data:`adcp.types.SchemaVariant` (#710).

Static-typing behavior is exercised separately via
``tests/type_checks/cross_class_override_with_schema_variant.py``,
which CI mypy-strict-checks. These tests pin the runtime contract:

1. ``SchemaVariant[T]`` evaluates to ``T``.
2. Pydantic validates fields annotated with ``SchemaVariant[T]`` against
   the wrapped ``T`` exactly as if ``T`` had been the annotation.
3. ``model_dump`` round-trips work unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from adcp.types import SchemaVariant


def test_subscription_returns_wrapped_type() -> None:
    """``SchemaVariant[X]`` must collapse to ``X`` at runtime. Pydantic
    reads the annotation via ``get_type_hints`` which evaluates the
    subscription — if the metaclass didn't return ``X``, Pydantic
    would see a ``SchemaVariant`` instance and refuse to validate."""
    assert SchemaVariant[int] is int
    assert SchemaVariant[list[str]] == list[str]
    assert SchemaVariant[dict[str, int]] == dict[str, int]


def test_pydantic_validates_against_wrapped_type() -> None:
    """A Pydantic field annotated ``SchemaVariant[list[Sub]]`` validates
    every input list element as ``Sub``. This is the load-bearing
    behavior — Pydantic must not see ``SchemaVariant``; it must see
    the inner type."""

    class Sub(BaseModel):
        value: int

    class Container(BaseModel):
        items: SchemaVariant[list[Sub]]

    ok = Container(items=[Sub(value=1), Sub(value=2)])
    assert ok.items[0].value == 1
    assert ok.items[1].value == 2

    # Wrong type for an item → Pydantic rejects via the wrapped Sub.
    with pytest.raises(ValidationError):
        Container(items=[{"wrong_field": "x"}])  # type: ignore[list-item]


def test_model_dump_roundtrip() -> None:
    """SchemaVariant doesn't interfere with serialization — model_dump
    produces the same dict shape as if the wrapped type had been the
    annotation directly."""

    class Item(BaseModel):
        name: str

    class WithVariant(BaseModel):
        items: SchemaVariant[list[Item]]

    class WithoutVariant(BaseModel):
        items: list[Item]

    variant = WithVariant(items=[Item(name="a"), Item(name="b")])
    plain = WithoutVariant(items=[Item(name="a"), Item(name="b")])

    assert variant.model_dump() == plain.model_dump()


def test_cross_class_override_validates() -> None:
    """The whole point of the marker: an adopter substitutes a sibling
    class for the parent's declared element type. The override
    validates against the sibling, not the parent — proves Pydantic
    sees ``list[AdopterEntity]`` even though the parent declared
    ``Sequence[LibraryEntity]``.

    Mypy's override-compat error on this pattern is what the bundled
    plugin suppresses — see the static-test fixture for that side."""

    class LibraryCreative(BaseModel):
        creative_id: str

    class AdopterCreative(BaseModel):
        # Same shape, different class — common pattern for adopters
        # that carry extra internal fields and a different name.
        creative_id: str
        internal_state: str = "active"

    class LibraryResponse(BaseModel):
        creatives: list[LibraryCreative]

    class AdopterResponse(LibraryResponse):
        creatives: SchemaVariant[list[AdopterCreative]]

    resp = AdopterResponse(
        creatives=[
            AdopterCreative(creative_id="c1"),
            AdopterCreative(creative_id="c2", internal_state="paused"),
        ]
    )
    # Each item is the adopter's class, not the library's — proves
    # validation went through the wrapped (adopter) type, not the
    # parent's declared type.
    assert isinstance(resp.creatives[0], AdopterCreative)
    assert resp.creatives[1].internal_state == "paused"


def test_does_not_accept_unbound_use() -> None:
    """``SchemaVariant`` is a marker — it should not be used as a bare
    type annotation. Constructing a Pydantic model with a bare
    ``SchemaVariant`` annotation degenerates to Pydantic's
    arbitrary-types handling; the result is undefined and adopters
    shouldn't rely on it. Document the contract here so anyone tempted
    to do this hits a test telling them not to.
    """

    # We don't enforce a hard error — just document that the result is
    # implementation-defined. The test asserts the marker exists and is
    # subscriptable; bare use is out of scope.
    assert callable(getattr(SchemaVariant, "__class_getitem__", None)) or hasattr(
        type(SchemaVariant), "__getitem__"
    )


def test_nested_subscription_resolves() -> None:
    """``SchemaVariant[dict[str, SchemaVariant[int]]]`` should fully
    resolve to ``dict[str, int]`` — the metaclass evaluates each level
    as Python evaluates the subscription left-to-right."""

    inner = SchemaVariant[int]
    outer = SchemaVariant[dict[str, inner]]  # type: ignore[valid-type]
    assert outer == dict[str, int]


def test_works_with_arbitrary_t() -> None:
    """Regression: ``SchemaVariant[X]`` should accept any subscriptable
    type, not just simple generics. Tuples, unions, callable types,
    etc., all pass through unchanged."""

    assert SchemaVariant[tuple[int, str]] == tuple[int, str]
    assert SchemaVariant[int | None] == int | None

    callable_t: Any = type("Callable", (), {"__class_getitem__": staticmethod(lambda args: args)})
    # Use ``==`` not ``is`` — Callable[[int], str] constructs a fresh
    # tuple on each invocation; identity won't hold but equality does.
    assert SchemaVariant[callable_t[[int], str]] == callable_t[[int], str]
