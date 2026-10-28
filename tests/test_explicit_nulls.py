"""A received ``null`` round-trips; a Python ``None`` means "not supplied" (#1615).

On the wire an explicit ``null`` on a property whose schema admits it is a
value: a digest or echo over the document includes it. On the Python side
``None`` is the SDK's spelling of "not supplied". ``AdCPBaseModel`` keeps both
through ``model_fields_set``: validation leaves a received ``null`` set, the
constructor makes a ``None`` keyword argument unset, and the dump re-emits
``null`` for exactly the set, null-admitting fields that are ``None``.
``tests/conformance/test_wire_round_trip.py`` holds every generated model to
this with no ``null_dropped`` allowance.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from typing import Annotated

import pytest
from pydantic import Field, ValidationError

from adcp.types.base import AdCPBaseModel, NoExplicitNull, null_admitting_fields


class Inner(AdCPBaseModel):
    y: int | None = None


class Model(AdCPBaseModel):
    x: int | None = None
    never: Annotated[int | None, Field(), NoExplicitNull] = None
    field_schema: Annotated[str | None, Field(alias="$schema")] = None
    inner: Inner | None = None
    required: int


def test_null_admitting_fields_exclude_the_never_null_marker() -> None:
    assert null_admitting_fields(Model) == {"x", "field_schema", "inner"}
    assert null_admitting_fields(Inner) == {"y"}


@pytest.mark.parametrize(
    "parse",
    [
        lambda doc: Model.model_validate_json(json.dumps(doc)),
        lambda doc: Model.model_validate(doc),
    ],
    ids=["json", "dict"],
)
def test_a_received_null_is_re_emitted(parse) -> None:
    doc = {"required": 1, "x": None, "$schema": None, "inner": {"y": None}}
    model = parse(doc)
    assert model.x is None
    assert model.model_fields_set == {"required", "x", "field_schema", "inner"}
    assert model.model_dump(mode="json") == doc
    assert json.loads(model.model_dump_json()) == doc
    assert model.model_dump_as_received() == doc


def test_a_received_null_is_written_under_the_wire_name() -> None:
    model = Model.model_validate({"required": 1, "$schema": None})
    assert model.model_dump(mode="json") == {"required": 1, "$schema": None}
    assert model.model_dump(mode="json", by_alias=False) == {"required": 1, "field_schema": None}


def test_a_none_keyword_argument_is_not_supplied() -> None:
    model = Model(required=1, x=None, never=None, field_schema=None, inner=Inner(y=None))
    assert model.model_fields_set == {"required", "inner"}
    assert model.model_dump(mode="json") == {"required": 1, "inner": {}}
    assert model.model_dump(mode="json", exclude_unset=True) == {"required": 1, "inner": {}}
    assert Model(required=1) == Model(required=1, x=None)


def test_a_none_keyword_argument_is_absent_to_validators() -> None:
    """A validator that asks ``model_fields_set`` sees what the wire would."""
    from pydantic import model_validator

    class Grouped(AdCPBaseModel):
        a: int | None = None
        b: int | None = None

        @model_validator(mode="after")
        def _one_of(self) -> Grouped:
            if not ({"a", "b"} & self.model_fields_set):
                raise ValueError("a or b is required")
            return self

    with pytest.raises(ValidationError, match="a or b is required"):
        Grouped(a=None)
    assert Grouped.model_validate({"a": None}).model_fields_set == {"a"}


def test_a_required_nullable_field_keeps_a_none_keyword_argument() -> None:
    class Required(AdCPBaseModel):
        value: int | None

    model = Required(value=None)
    assert model.model_fields_set == {"value"}
    assert model.model_dump(mode="json") == {"value": None}


def test_an_excluded_field_is_never_put_back() -> None:
    class WithInternal(AdCPBaseModel):
        x: int | None = None
        internal: Annotated[str | None, Field(exclude=True)] = None

    model = WithInternal.model_validate({"x": None, "internal": None})
    assert model.model_dump(mode="json") == {"x": None}


def test_a_none_inside_a_dict_argument_is_a_wire_null() -> None:
    model = Model(required=1, inner={"y": None})
    assert model.model_dump(mode="json") == {"required": 1, "inner": {"y": None}}


def test_assigning_none_unsets_and_assigning_a_value_sets() -> None:
    model = Model.model_validate({"required": 1, "x": None})
    model.x = 5
    assert model.model_dump(mode="json") == {"required": 1, "x": 5}
    model.x = None
    assert "x" not in model.model_fields_set
    assert model.model_dump(mode="json") == {"required": 1}


def test_model_copy_update_with_none_unsets() -> None:
    model = Model.model_validate({"required": 1, "x": None, "inner": {"y": None}})
    copied = model.model_copy(update={"x": None})
    assert copied.model_dump(mode="json") == {"required": 1, "inner": {"y": None}}
    kept = model.model_copy()
    assert kept.model_dump(mode="json") == {"required": 1, "x": None, "inner": {"y": None}}


def test_include_and_exclude_apply_to_a_re_emitted_null() -> None:
    model = Model.model_validate({"required": 1, "x": None, "inner": {"y": None}})
    assert model.model_dump(mode="json", exclude={"x"}) == {"required": 1, "inner": {"y": None}}
    assert model.model_dump(mode="json", include={"x"}) == {"x": None}


def test_a_never_null_field_still_refuses_json_null() -> None:
    with pytest.raises(ValidationError):
        Model.model_validate_json('{"required": 1, "never": null}')
    assert Model.model_validate({"required": 1, "never": None}).model_dump(mode="json") == {
        "required": 1
    }


def test_a_generated_null_admitting_field_round_trips() -> None:
    from adcp.types.domains.core.targeting_input import TargetingOverlayInput

    assert "signal_targeting_groups" in null_admitting_fields(TargetingOverlayInput)
    doc = {"signal_targeting_groups": None}
    parsed = TargetingOverlayInput.model_validate_json(json.dumps(doc))
    assert parsed.model_dump(mode="json") == doc
    assert TargetingOverlayInput(signal_targeting_groups=None).model_dump(mode="json") == {}


def test_a_canonical_model_re_emits_a_received_null() -> None:
    """``CanonicalBoundaryModel`` replaces the base serializer and keeps the pass."""
    from adcp.types.canonical_creative import CanonicalBoundaryModel

    class Canonical(CanonicalBoundaryModel):
        x: int | None = None

    assert Canonical.model_validate({"x": None}).model_dump(mode="json") == {"x": None}
    assert Canonical(x=None).model_dump(mode="json") == {}


def _every_model() -> list[type[AdCPBaseModel]]:
    import adcp.types._eager as eager
    import adcp.types.canonical_creative as canonical
    import adcp.types.domains as domains
    import adcp.types.versioned as versioned

    found: dict[type, None] = {}
    for info in pkgutil.walk_packages(domains.__path__, prefix=domains.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue  # a removed-schema shim that raises on import
        for obj in list(vars(module).values()):
            if isinstance(obj, type) and issubclass(obj, AdCPBaseModel):
                found[obj] = None
    for module in (eager, canonical, versioned):
        for obj in list(vars(module).values()):
            if isinstance(obj, type) and issubclass(obj, AdCPBaseModel):
                found[obj] = None
    return list(found)


def test_no_custom_init_model_has_a_null_admitting_field() -> None:
    """A model with its own ``__init__`` is also constructed by validation.

    Pydantic calls a non-base ``__init__`` from inside ``model_validate`` too,
    so ``AdCPBaseModel.__init__`` cannot tell a received ``null`` from a
    ``None`` keyword argument there. Such a class must therefore have nothing
    to remember: every optional field carries ``NoExplicitNull``, as the
    schema it declares demands.
    """
    offenders = [
        (model.__module__, model.__name__, sorted(null_admitting_fields(model)))
        for model in _every_model()
        if getattr(model, "__pydantic_custom_init__", False) and null_admitting_fields(model)
    ]
    assert not offenders, offenders
