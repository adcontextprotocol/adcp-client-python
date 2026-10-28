"""``model_dump()`` writes wire names for aliased fields (#1615).

The generator aliases a wire key that is not a Python identifier
(``$schema`` -> ``field_schema``, ``pass`` -> ``pass_``). In SDK 9 a dump
without ``by_alias=True`` wrote the Python name, which is not a key the
schema admits; the SDK's own wire paths passed ``by_alias`` and adopters
dumping a model themselves did not. SDK 10 makes the wire name the default,
and ``tests/conformance/test_wire_round_trip.py`` holds every generated
model to it with no ``alias_emitted`` allowance.
"""

from __future__ import annotations

import json
from typing import Annotated

import pytest
from pydantic import Field

from adcp.types.base import AdCPBaseModel


class _Aliased(AdCPBaseModel):
    field_schema: Annotated[str, Field(alias="$schema")]
    pass_: Annotated[list[str] | None, Field(alias="pass")] = None
    plain: str | None = None


def _aliased_fields(model: type[AdCPBaseModel]) -> dict[str, str]:
    return {
        name: field.alias
        for name, field in model.model_fields.items()
        if field.alias is not None and field.alias != name
    }


@pytest.mark.parametrize("dump", ["model_dump", "model_dump_json"])
def test_default_dump_writes_the_wire_name(dump: str) -> None:
    model = _Aliased.model_validate({"$schema": "https://example.com/ext.json", "pass": ["a"]})
    out = model.model_dump(mode="json") if dump == "model_dump" else model.model_dump_json()
    data = out if isinstance(out, dict) else json.loads(out)
    assert data == {"$schema": "https://example.com/ext.json", "pass": ["a"]}


def test_by_alias_false_restores_the_python_name() -> None:
    model = _Aliased.model_validate({"$schema": "https://example.com/ext.json"})
    assert model.model_dump(mode="json", by_alias=False) == {
        "field_schema": "https://example.com/ext.json"
    }


def test_construction_accepts_both_spellings() -> None:
    by_wire = _Aliased.model_validate({"$schema": "https://example.com/ext.json"})
    by_name = _Aliased(field_schema="https://example.com/ext.json")
    assert by_wire == by_name
    assert by_name.model_dump(mode="json") == {"$schema": "https://example.com/ext.json"}


def test_model_dump_as_received_is_unchanged() -> None:
    model = _Aliased.model_validate({"$schema": "https://example.com/ext.json", "plain": None})
    assert model.model_dump_as_received() == {
        "$schema": "https://example.com/ext.json",
        "plain": None,
    }


def test_every_generated_alias_is_its_serialization_alias() -> None:
    """Every aliased key in the generated tree serializes under its wire name."""
    import importlib
    import pkgutil

    import adcp.types.domains as domains

    seen: set[str] = set()
    for info in pkgutil.walk_packages(domains.__path__, prefix=domains.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            # A removed-schema shim that raises on import (``enums.format_category``).
            continue
        for obj in vars(module).values():
            if not (isinstance(obj, type) and issubclass(obj, AdCPBaseModel)):
                continue
            for python_name, alias in _aliased_fields(obj).items():
                seen.add(alias)
                field = obj.model_fields[python_name]
                assert field.serialization_alias in (None, alias), (obj, python_name)
                assert obj.model_config.get("serialize_by_alias") is True, obj
    assert {"$schema", "pass", "from", "media-buy", "_truncation", "2xl"} <= seen
