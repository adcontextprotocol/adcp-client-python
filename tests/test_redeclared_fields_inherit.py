"""A hand-written redeclaration keeps its generated field's metadata (#1620).

A canonical model narrows an inherited field's annotation — ``list[Format]``
for the generated ``list[FormatOption]`` — and that is all it means to change.
A bare ``Field(...)`` on the redeclaration silently dropped the generated
description, the constraints (``min_length``, ``max_length``) and the
never-null marker that leaves an unset ``None`` off the wire from any parent
(#1591), so a plain-pydantic parent nesting a canonical ``Product`` wrote
``"placements": null`` where the schema admits no null. #1548 and #1579 fixed
single fields the same way; this guard walks every hand-written subclass of a
generated model and holds every redeclared field to its parent, so the next
redeclaration cannot regress it.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import warnings
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

import adcp
from adcp.types import (
    CreateMediaBuyRequest,
    GetProductsRequest,
    GetProductsResponse,
    ListCreativesRequest,
    Placement,
    Product,
    UpdateMediaBuyRequest,
)
from adcp.types.base import is_no_explicit_null

#: Redeclarations that deliberately do not mirror the generated field, each
#: with the reason. Nothing else is exempt.
ALLOWED: dict[tuple[str, str], str] = {
    ("adcp.types.projections.BusinessEntityResponse", "bank"): (
        "write-only projection: the field is excluded from every dump and a validator "
        "refuses any value, so the marker and description have nothing to apply to"
    ),
    ("adcp.types.projections._NotificationAuthenticationResponse", "credentials"): (
        "write-only projection: excluded from every dump, any value refused; the generated "
        "min_length would raise on the only value it ever holds (None)"
    ),
    ("adcp.types.experimental_identity.AgentOnboardingRequirement", "kind"): (
        "the schema enum is x-extensible and readers keep unknown advisory kinds, so the "
        "required field widens to `enum | str`; its only added metadata is the "
        "left_to_right union mode that still parses a known kind as the enum member"
    ),
}


def _is_generated(cls: type) -> bool:
    return cls.__module__.startswith("adcp.types.domains.")


def _generated_parent_field(cls: type[BaseModel], name: str) -> tuple[type, FieldInfo] | None:
    for base in cls.__mro__[1:]:
        if (
            isinstance(base, type)
            and issubclass(base, BaseModel)
            and _is_generated(base)
            and name in base.model_fields
        ):
            return base, base.model_fields[name]
    return None


def _constraints(field: FieldInfo) -> list[str]:
    """The field's constraint metadata, as text, without the never-null marker."""
    return sorted(
        repr(item)
        for item in field.metadata
        if type(item).__name__ != "_RefuseJsonNull" and item is not None
    )


def _hand_written_subclasses() -> list[type[BaseModel]]:
    """Every class under ``adcp`` that subclasses a generated model and is not one."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        importlib.import_module("adcp.types._eager")
        for info in pkgutil.walk_packages(adcp.__path__, "adcp."):
            if ".domains." in info.name or info.name.endswith("._generated"):
                continue
            try:
                importlib.import_module(info.name)
            except ImportError:  # an optional dependency this environment lacks
                continue
    found: list[type[BaseModel]] = []
    seen: set[type] = set()
    for module_name in sorted(sys.modules):
        if not module_name.startswith("adcp.") or ".domains." in module_name:
            continue
        for value in list(vars(sys.modules[module_name]).values()):
            if not (isinstance(value, type) and issubclass(value, BaseModel)):
                continue
            if value in seen or value.__module__ != module_name or _is_generated(value):
                continue
            if any(_is_generated(base) for base in value.__mro__[1:]):
                seen.add(value)
                found.append(value)
    return found


def _redeclarations() -> list[tuple[str, str, FieldInfo, FieldInfo]]:
    out = []
    for cls in _hand_written_subclasses():
        for name in cls.__dict__.get("__annotations__", {}):
            if name.startswith("_") or name not in cls.model_fields:
                continue
            parent = _generated_parent_field(cls, name)
            if parent is None:
                continue
            out.append(
                (f"{cls.__module__}.{cls.__qualname__}", name, parent[1], cls.model_fields[name])
            )
    return out


REDECLARED = sorted(_redeclarations(), key=lambda item: (item[0], item[1]))


def test_the_walk_finds_the_canonical_layer() -> None:
    names = {(owner, name) for owner, name, _, _ in REDECLARED}
    assert ("adcp.types.canonical_creative.Product", "placements") in names
    assert ("adcp.types.capabilities.MediaBuy", "reporting_delivery") in names
    assert ("adcp.types.projections.AccountResponse", "notification_configs") in names


@pytest.mark.parametrize(
    "owner, name, parent, child",
    REDECLARED,
    ids=[f"{owner.rsplit('.', 1)[1]}.{name}" for owner, name, _, _ in REDECLARED],
)
def test_redeclared_field_keeps_its_generated_metadata(
    owner: str, name: str, parent: FieldInfo, child: FieldInfo
) -> None:
    if (owner, name) in ALLOWED:
        pytest.skip(ALLOWED[(owner, name)])
    problems = []
    narrowed_to_required = child.is_required() and not parent.is_required()
    # A field made required drops the marker (``inherit_field(required=True)``):
    # every marked field in the tree is optional, and a required one has no
    # "omitted" for it to spell.
    if not narrowed_to_required and is_no_explicit_null(parent) != is_no_explicit_null(child):
        problems.append("never-null marker")
    if (parent.description or None) != (child.description or None):
        problems.append("description")
    if _constraints(parent) != _constraints(child):
        problems.append(f"constraints {_constraints(parent)} -> {_constraints(child)}")
    if parent.alias != child.alias:
        problems.append("alias")
    assert not problems, (
        f"{owner}.{name} redeclares the generated field without its {', '.join(problems)}; "
        "assign adcp.types.base.inherit_field(<generated class>, <name>) on the redeclaration"
    )


def test_every_allowed_exemption_is_still_a_redeclaration() -> None:
    names = {(owner, name) for owner, name, _, _ in REDECLARED}
    stale = sorted(set(ALLOWED) - names)
    assert not stale, f"ALLOWED names redeclarations that no longer exist: {stale}"


# ---------------------------------------------------------------------------
# The issue's reproduction: no null for an unset optional field from a parent.
# ---------------------------------------------------------------------------


class _Envelope(BaseModel):
    products: list[Product]


def test_a_nested_canonical_product_leaves_unset_placements_off() -> None:
    product = Product.model_construct(product_id="p", name="n", description="d")
    assert [k for k, v in product.model_dump(mode="json").items() if v is None] == []
    body = _Envelope.model_construct(products=[product]).model_dump(mode="json")
    assert [k for k, v in body["products"][0].items() if v is None] == []


@pytest.mark.parametrize(
    "model, field",
    [
        (Product, "placements"),
        (Placement, "format_options"),
        (CreateMediaBuyRequest, "packages"),
        (UpdateMediaBuyRequest, "packages"),
        (UpdateMediaBuyRequest, "new_packages"),
        (GetProductsResponse, "products"),
        (GetProductsRequest, "filters"),
        (ListCreativesRequest, "filters"),
    ],
)
def test_listed_fields_carry_the_never_null_marker(model: type[BaseModel], field: str) -> None:
    info = model.model_fields[field]
    assert is_no_explicit_null(info), f"{model.__name__}.{field}"

    class Parent(BaseModel):
        item: Any

    instance = model.model_construct(**{field: None})
    assert field not in Parent(item=instance).model_dump(mode="json")["item"]
