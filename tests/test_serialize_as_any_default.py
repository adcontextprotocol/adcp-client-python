"""AdCPBaseModel defaults ``serialize_as_any=True`` so that subclass
``@model_serializer`` overrides fire when nested under a base-typed parent
field, and ``Field(exclude=True)`` continues to suppress internal fields at
every nesting depth.

Together these two guarantees mean adopters never need to write parent-side
``model_dump`` overrides that manually walk children — Pydantic does the
walking, ``Field(exclude=True)`` is the wire-isolation contract, and
``@model_serializer`` is the custom-logic seam. The previous default
(``serialize_as_any=False``) silently dropped subclass-only fields and
skipped subclass serializers under nesting; that footgun is what these
tests pin closed.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, SerializationInfo, model_serializer

from adcp.types.base import AdCPBaseModel


class _SpecChild(BaseModel):
    spec_field: str


class _ExtendedChildWithExtraField(_SpecChild):
    """Subclass that adds a non-excluded field — appears in serialized output
    when the parent dispatches via ``serialize_as_any=True``."""

    seller_extension: str = "exposed"


class _ExtendedChildWithExcludedField(_SpecChild):
    """Subclass that adds an internal field marked ``exclude=True`` — must
    never appear on the wire, regardless of serialize_as_any state."""

    internal_id: str = Field(default="internal-42", exclude=True)


class _ExtendedChildWithSerializer(_SpecChild):
    """Subclass with a wrap-mode model serializer — fires under nesting
    once serialize_as_any is set."""

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any, info: SerializationInfo) -> dict[str, Any]:
        result: dict[str, Any] = handler(self, info)
        result["normalized_by_subclass"] = True
        return result


class _Parent(AdCPBaseModel):
    """Parent declares the field as the spec base type."""

    child: _SpecChild
    children: list[_SpecChild] = Field(default_factory=list)


def test_subclass_serializer_fires_on_singular_field() -> None:
    parent = _Parent(child=_ExtendedChildWithSerializer(spec_field="ok"))
    dumped = parent.model_dump()
    assert dumped["child"] == {"spec_field": "ok", "normalized_by_subclass": True}


def test_subclass_serializer_fires_on_list_field() -> None:
    parent = _Parent(
        child=_SpecChild(spec_field="root"),
        children=[
            _ExtendedChildWithSerializer(spec_field="a"),
            _ExtendedChildWithSerializer(spec_field="b"),
        ],
    )
    dumped = parent.model_dump()
    for entry in dumped["children"]:
        assert entry["normalized_by_subclass"] is True


def test_subclass_serializer_fires_in_json_dump() -> None:
    """``model_dump_json`` carries the same default."""
    parent = _Parent(child=_ExtendedChildWithSerializer(spec_field="ok"))
    dumped = json.loads(parent.model_dump_json())
    assert dumped["child"]["normalized_by_subclass"] is True


def test_field_exclude_true_still_suppresses_internal_field() -> None:
    """The wire-isolation contract: ``Field(exclude=True)`` keeps internal
    state off the wire even when serialize_as_any honors subclass schemas."""
    parent = _Parent(child=_ExtendedChildWithExcludedField(spec_field="ok"))
    dumped = parent.model_dump()
    assert dumped["child"] == {"spec_field": "ok"}
    assert "internal_id" not in dumped["child"]


def test_field_exclude_true_works_in_list_field() -> None:
    parent = _Parent(
        child=_SpecChild(spec_field="root"),
        children=[
            _ExtendedChildWithExcludedField(spec_field="a"),
            _ExtendedChildWithExcludedField(spec_field="b"),
        ],
    )
    dumped = parent.model_dump()
    for entry in dumped["children"]:
        assert "internal_id" not in entry


def test_subclass_only_field_appears_under_default() -> None:
    """Subclasses that add fields without ``Field(exclude=True)`` will see
    those fields appear on the wire under the new default. This pins the
    behavior change so adopters who relied on the previous accidental
    firewall surface a failing test rather than discovering it in
    production."""
    parent = _Parent(child=_ExtendedChildWithExtraField(spec_field="ok"))
    dumped = parent.model_dump()
    assert dumped["child"] == {"spec_field": "ok", "seller_extension": "exposed"}


def test_caller_can_opt_out_with_explicit_kwarg() -> None:
    """Adopters who want the prior firewall back can pass
    ``serialize_as_any=False`` explicitly — the default only kicks in when
    the kwarg is unset."""
    parent = _Parent(child=_ExtendedChildWithExtraField(spec_field="ok"))
    dumped = parent.model_dump(serialize_as_any=False)
    assert dumped["child"] == {"spec_field": "ok"}
    assert "seller_extension" not in dumped["child"]


def test_caller_can_still_pass_exclude_none_false() -> None:
    """The two defaults are independent — overriding one doesn't disturb
    the other."""

    class _ParentWithOptional(AdCPBaseModel):
        child: _SpecChild
        optional: str | None = None

    parent = _ParentWithOptional(child=_SpecChild(spec_field="ok"))
    dumped = parent.model_dump(exclude_none=False)
    assert dumped["optional"] is None
