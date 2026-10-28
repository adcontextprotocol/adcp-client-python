"""The default dump leaves an unset schema default off (#1613).

The generator writes a schema ``default`` into the field (``sandbox: bool =
False``), which is the right attribute surface. On the wire "omitted" and
"present with the default" are different bytes, and a digest or echo over the
document tells them apart. A schema-optional property with a default (or a
``const``) carries ``OmitUnsetDefault``, and the default dump emits it only
when it is in ``model_fields_set``. ``tests/conformance/test_wire_round_trip.py``
holds every generated model to it with no ``default_emitted`` allowance.
"""

from __future__ import annotations

import copy
import importlib
import pkgutil
import typing
from typing import Annotated, Literal

import pytest
from pydantic import Field, RootModel

from adcp.types.base import (
    AdCPBaseModel,
    AdCPRootModel,
    NoExplicitNull,
    OmitUnsetDefault,
    inherit_field,
    is_omit_unset_default,
    omit_unset_default_fields,
)


class Model(AdCPBaseModel):
    kind: Literal["thing"] = "thing"  # required on the wire: no marker
    sandbox: Annotated[bool, Field(description="Optional, defaults false."), OmitUnsetDefault] = (
        False
    )
    timezone: Annotated[str | None, NoExplicitNull, OmitUnsetDefault] = "inventory_local"
    field_schema: Annotated[str, Field(alias="$schema"), OmitUnsetDefault] = "draft-07"
    plain: str | None = None


def test_marked_fields_are_the_schema_optional_defaults() -> None:
    assert omit_unset_default_fields(Model) == {"sandbox", "timezone", "field_schema"}
    assert is_omit_unset_default(Model.model_fields["sandbox"])
    assert not is_omit_unset_default(Model.model_fields["kind"])


def test_the_marker_survives_a_copy() -> None:
    assert copy.deepcopy(OmitUnsetDefault) is OmitUnsetDefault
    assert copy.copy(OmitUnsetDefault) is OmitUnsetDefault
    assert repr(OmitUnsetDefault) == "OmitUnsetDefault"


def test_a_parsed_document_dumps_without_the_defaults_it_omitted() -> None:
    parsed = Model.model_validate_json("{}")
    assert parsed.sandbox is False
    assert parsed.timezone == "inventory_local"
    assert parsed.model_dump(mode="json") == {"kind": "thing"}
    assert parsed.model_dump_json() == '{"kind":"thing"}'


def test_a_default_the_document_carried_stays() -> None:
    parsed = Model.model_validate_json('{"sandbox": false, "$schema": "draft-07"}')
    assert parsed.model_dump(mode="json") == {
        "kind": "thing",
        "sandbox": False,
        "$schema": "draft-07",
    }


def test_a_python_built_model_dumps_what_the_caller_set_plus_the_wire_required() -> None:
    assert Model().model_dump(mode="json") == {"kind": "thing"}
    assert Model(sandbox=False).model_dump(mode="json") == {"kind": "thing", "sandbox": False}
    model = Model()
    model.timezone = "UTC"
    assert model.model_dump(mode="json") == {"kind": "thing", "timezone": "UTC"}


def test_exclude_unset_false_restores_every_default() -> None:
    parsed = Model.model_validate_json("{}")
    assert parsed.model_dump(mode="json", exclude_unset=False) == {
        "kind": "thing",
        "sandbox": False,
        "timezone": "inventory_local",
        "$schema": "draft-07",
    }
    assert parsed.model_dump(mode="json", exclude_unset=True) == {}


def test_a_nested_model_follows_the_same_rule() -> None:
    class Outer(AdCPBaseModel):
        inner: Model
        items: list[Model] = []

    outer = Outer.model_validate({"inner": {}, "items": [{"sandbox": True}]})
    assert outer.model_dump(mode="json") == {
        "inner": {"kind": "thing"},
        "items": [{"kind": "thing", "sandbox": True}],
    }
    assert outer.model_dump(mode="json", exclude_unset=False)["inner"]["sandbox"] is False


def test_by_alias_false_omits_by_python_name() -> None:
    parsed = Model.model_validate_json("{}")
    assert parsed.model_dump(mode="json", by_alias=False) == {"kind": "thing"}


def test_model_dump_as_received_is_a_deprecated_alias() -> None:
    parsed = Model.model_validate_json('{"plain": null}')
    with pytest.warns(DeprecationWarning, match="model_dump_as_received"):
        received = parsed.model_dump_as_received()
    assert received == parsed.model_dump(mode="json") == {"kind": "thing", "plain": None}


def test_inherit_field_required_drops_the_marker() -> None:
    field = inherit_field(Model, "sandbox", required=True)
    assert not any(item is OmitUnsetDefault for item in field.metadata)
    kept = inherit_field(Model, "sandbox")
    assert any(item is OmitUnsetDefault for item in kept.metadata)


def test_a_generated_root_model_dumps_like_its_arm() -> None:
    class Arm(AdCPBaseModel):
        x: int | None = None
        sandbox: Annotated[bool, OmitUnsetDefault] = False

    class Root(AdCPRootModel[Arm | int]):
        pass

    assert issubclass(Root, RootModel)
    assert Root.model_validate({}).model_dump(mode="json") == {}
    assert Root.model_validate({}).model_dump_json() == "{}"
    assert Root.model_validate({}).model_dump(mode="json", exclude_unset=False) == {
        "sandbox": False
    }
    assert Root.model_validate(3).model_dump() == 3


# --- the generated tree ---------------------------------------------------------------


def _generated_models() -> list[type[AdCPBaseModel]]:
    import adcp.types.domains as domains

    found: dict[type, None] = {}
    for info in pkgutil.walk_packages(domains.__path__, prefix=domains.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue  # a removed-schema shim that raises on import
        for obj in list(vars(module).values()):
            if (
                isinstance(obj, type)
                and issubclass(obj, AdCPBaseModel)
                and obj.__module__ == module.__name__
            ):
                found[obj] = None
    return list(found)


def test_the_marker_is_widespread_in_the_generated_tree() -> None:
    """The rule is generator-level: hundreds of classes carry it."""
    marked = [model for model in _generated_models() if omit_unset_default_fields(model)]
    assert len(marked) > 300, len(marked)


def test_no_generated_root_model_keeps_pydantic_s_dump() -> None:
    """Every composing root the generator keeps wrapped is an ``AdCPRootModel``."""
    import adcp.types.domains as domains

    offenders = []
    for info in pkgutil.walk_packages(domains.__path__, prefix=domains.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue
        for obj in list(vars(module).values()):
            if (
                isinstance(obj, type)
                and issubclass(obj, RootModel)
                and obj.__module__ == module.__name__
                and obj is not RootModel
                and not issubclass(obj, AdCPRootModel)
                and "_ScalarRoot" not in {base.__name__ for base in obj.__mro__}
            ):
                offenders.append(f"{obj.__module__}.{obj.__name__}")
    assert offenders == [], offenders


def test_a_generated_schema_default_round_trips() -> None:
    from adcp.types.domains.core.account_ref import AccountReference2

    assert omit_unset_default_fields(AccountReference2) == {"sandbox"}
    document = {"brand": {"domain": "acme.example"}, "operator": "agency.example"}
    parsed = AccountReference2.model_validate(document)
    assert parsed.sandbox is False
    assert parsed.model_dump(mode="json") == document
    assert AccountReference2.model_validate({**document, "sandbox": False}).model_dump(
        mode="json"
    ) == {**document, "sandbox": False}


def test_a_required_discriminator_is_still_emitted_when_built_in_python() -> None:
    """An injected single-value ``Literal`` default on a required property has no marker."""
    from adcp.types.domains.core.assets.image_asset import ImageAsset

    field = ImageAsset.model_fields["asset_type"]
    assert typing.get_origin(field.annotation) is Literal
    assert not is_omit_unset_default(field)
    asset = ImageAsset(url="https://cdn.example/a.png", width=1, height=1)
    assert asset.model_dump(mode="json")["asset_type"] == "image"
