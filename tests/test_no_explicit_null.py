"""An optional property refuses an explicit ``null`` unless its schema admits one.

The generator renders every optional property as ``T | None = None``. That is
the right Python surface — omitted reads as ``None`` and dumps as absent — but
it also admitted an explicit JSON ``null`` that the schema, and so the SDK's
own JSON-Schema arm, rejects (#1547). The generator now marks each optional
property the bundled validator says cannot be ``null`` and the post-generation
pass points the mark at :data:`adcp.types.base.NoExplicitNull`.

The rule is about the wire: a JSON document carrying ``null`` is refused, and
the published JSON Schema has no null arm. A Python ``None`` — keyword
construction, ``model_validate`` on a dict — stays the SDK's spelling of
"not supplied", symmetric with ``model_dump``'s ``exclude_none`` default, unless
``ADCP_STRICT_VALIDATION`` asks for it to be refused too.

``tests/conformance/test_schema_model_parity.py`` is the sweep: it mutates every
leaf of every graded tool's request to ``None`` and requires the bundled schema
and the model (in JSON mode) to agree, with the
``optional_field_accepts_explicit_null`` divergence no longer declared. These
tests pin the rule's shape on the reported case, a genuinely nullable property,
and a sample of generated models.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ValidationError

import adcp.types as public_types
from adcp.types import base as types_base
from adcp.types.base import NoExplicitNull, is_no_explicit_null
from adcp.types.domains.creative.sync_creatives_request import SyncCreativesRequest
from adcp.types.domains.media_buy.update_media_buy_request import UpdateMediaBuyRequest
from adcp.validation import get_schema, validate_request

#: The #1547 reproduction: a legal assignment-only sync, plus ``creatives: null``.
_SYNC_WITH_NULL_CREATIVES: dict[str, Any] = {
    "adcp_version": "3.2",
    "account": {"account_id": "acct_1"},
    "idempotency_key": "repro-null-creatives-0001",
    "creatives": None,
    "assignment_operations": [
        {"operation": "unassign", "creative_id": "cr_1", "package_id": "pkg_1"},
    ],
}


def _null_errors(exc: ValidationError) -> list[tuple[tuple[Any, ...], str]]:
    return [(error["loc"], error["type"]) for error in exc.errors()]


def test_explicit_null_creatives_is_refused_like_the_bundled_schema() -> None:
    """The reported case: both arms now reject the JSON document at the same place."""
    schema_node = get_schema("sync_creatives", "request", version="3.2")["properties"]["creatives"]
    assert schema_node["type"] == "array"

    outcome = validate_request("sync_creatives", _SYNC_WITH_NULL_CREATIVES, version="3.2")
    assert [(issue.pointer, issue.keyword) for issue in outcome.issues] == [("/creatives", "type")]

    with pytest.raises(ValidationError) as excinfo:
        SyncCreativesRequest.model_validate_json(json.dumps(_SYNC_WITH_NULL_CREATIVES))
    # The type's own error, exactly as a required array given null would report.
    assert _null_errors(excinfo.value) == [(("creatives",), "list_type")]


def test_a_python_none_still_spells_absence() -> None:
    """``model_validate`` on a dict and keyword construction keep the SDK's convention.

    ``None`` on the Python side means "not supplied": it reads back as ``None``
    and ``model_dump`` leaves it off the wire, exactly as an omitted property
    would. Refusing it here would break every caller who passes an optional
    value through as ``field=maybe_none``.
    """
    parsed = SyncCreativesRequest.model_validate(_SYNC_WITH_NULL_CREATIVES)
    assert parsed.creatives is None
    assert "creatives" not in parsed.model_dump()

    base = {k: v for k, v in _SYNC_WITH_NULL_CREATIVES.items() if k != "creatives"}
    constructed = SyncCreativesRequest(**base, creatives=None)
    assert constructed.creatives is None
    assert constructed.model_dump() == SyncCreativesRequest(**base).model_dump()


def test_strict_validation_refuses_a_python_none_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ADCP_STRICT_VALIDATION`` extends the refusal to Python-mode input.

    The policy is read when a model's core schema is built, like the
    ``extra`` policy it travels with, so the check uses a model built under it.
    """
    monkeypatch.setattr(types_base, "_EXTRA_POLICY", "forbid")

    class Strict(BaseModel):
        items: Annotated[list[str] | None, NoExplicitNull] = None

    assert Strict().items is None
    with pytest.raises(ValidationError) as excinfo:
        Strict.model_validate({"items": None})
    assert _null_errors(excinfo.value) == [(("items",), "list_type")]
    with pytest.raises(ValidationError):
        Strict(items=None)
    assert Strict.model_validate_json('{"items": ["a"]}').items == ["a"]


def test_canonical_subclass_inherits_the_rule_through_inherit() -> None:
    """The canonical redeclaration (``_inherit``, #1548) keeps the generated marker."""
    field = public_types.SyncCreativesRequest.model_fields["creatives"]
    assert is_no_explicit_null(field)
    canonical = public_types.SyncCreativesRequest
    with pytest.raises(ValidationError) as excinfo:
        canonical.model_validate_json(json.dumps(_SYNC_WITH_NULL_CREATIVES))
    assert (("creatives",), "list_type") in _null_errors(excinfo.value)
    assert canonical.model_validate(_SYNC_WITH_NULL_CREATIVES).creatives is None


def test_a_redeclaration_that_keeps_the_field_info_keeps_the_marker() -> None:
    """What ``_inherit`` relies on: assigning the parent's ``FieldInfo`` carries the rule."""
    parent_field = SyncCreativesRequest.model_fields["creatives"]

    class Narrowed(SyncCreativesRequest):
        creatives: list[dict[str, object]] | None = parent_field  # type: ignore[assignment]

    assert is_no_explicit_null(Narrowed.model_fields["creatives"])
    with pytest.raises(ValidationError) as excinfo:
        Narrowed.model_validate_json(json.dumps(_SYNC_WITH_NULL_CREATIVES))
    assert _null_errors(excinfo.value) == [(("creatives",), "list_type")]


def test_omitted_creatives_still_reads_as_none() -> None:
    """Absence is the one permitted way of not supplying the property, and reads as ``None``."""
    payload = {k: v for k, v in _SYNC_WITH_NULL_CREATIVES.items() if k != "creatives"}
    assert validate_request("sync_creatives", payload, version="3.2").valid

    request = SyncCreativesRequest.model_validate(payload)
    assert request.creatives is None
    assert "creatives" not in request.model_fields_set
    assert "creatives" not in request.model_dump()

    # Keyword construction without the property behaves the same way.
    constructed = SyncCreativesRequest(**payload)
    assert constructed.creatives is None
    assert constructed.model_dump() == request.model_dump()


def test_constraints_and_description_survive_beside_the_marker() -> None:
    """The marker is appended to the field's metadata, not substituted for it."""
    field = SyncCreativesRequest.model_fields["creatives"]
    assert is_no_explicit_null(field)
    assert field.description == "Array of creative assets to sync (create or update)"
    too_many = {
        **{k: v for k, v in _SYNC_WITH_NULL_CREATIVES.items() if k != "creatives"},
        "creatives": [
            {"creative_id": f"cr_{i}", "name": f"n{i}", "format_kind": "image"} for i in range(101)
        ],
    }
    with pytest.raises(ValidationError) as excinfo:
        SyncCreativesRequest.model_validate(too_many)
    assert any(error["type"] == "too_long" for error in excinfo.value.errors())


def test_published_schema_states_what_the_bundle_states() -> None:
    """``model_json_schema`` no longer advertises a null arm for a non-nullable property."""
    node = SyncCreativesRequest.model_json_schema()["properties"]["creatives"]
    assert "anyOf" not in node
    assert node["type"] == "array"
    assert node["minItems"] == 1
    assert node["maxItems"] == 100
    assert node["description"] == "Array of creative assets to sync (create or update)"
    assert "creatives" not in SyncCreativesRequest.model_json_schema().get("required", [])


def test_genuinely_nullable_property_keeps_accepting_null() -> None:
    """A property whose schema lists ``null`` is generated without the marker."""
    schema = get_schema("update_media_buy", "request", version="3.2")
    assert "null" in schema["properties"]["budget_cap_timezone"]["type"]
    assert "null" in schema["properties"]["daily_budget_cap"]["type"]

    payload = {
        "account": {"account_id": "acct_1"},
        "media_buy_id": "mb_1",
        "idempotency_key": "nullable-timezone-0001",
        "budget_cap_timezone": None,
        "daily_budget_cap": None,
    }
    assert validate_request("update_media_buy", payload, version="3.2").valid

    request = UpdateMediaBuyRequest.model_validate_json(json.dumps(payload))
    assert request.budget_cap_timezone is None
    assert "budget_cap_timezone" in request.model_fields_set
    for name in ("budget_cap_timezone", "daily_budget_cap"):
        assert not is_no_explicit_null(UpdateMediaBuyRequest.model_fields[name])
    node = UpdateMediaBuyRequest.model_json_schema()["properties"]["budget_cap_timezone"]
    assert {"type": "null"} in node["anyOf"]


def test_json_round_trip_of_an_omitted_property_stays_omitted() -> None:
    """Dump then parse as JSON: an omitted property never becomes an explicit null."""
    base = {k: v for k, v in _SYNC_WITH_NULL_CREATIVES.items() if k != "creatives"}
    request = SyncCreativesRequest(**base, creatives=None)
    wire = request.model_dump_json()
    assert '"creatives"' not in wire
    assert SyncCreativesRequest.model_validate_json(wire).creatives is None


# --- a sample of generated models, graded against their own schema -----------------


def _generated_models() -> list[tuple[str, type[BaseModel]]]:
    names = sorted(
        name
        for name in dir(public_types)
        if not name.startswith("_") and not name.startswith("Legacy")
    )
    models = []
    for name in names:
        obj = getattr(public_types, name, None)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.model_fields:
            models.append((name, obj))
    return models


@pytest.mark.parametrize(
    "name,model",
    random.Random(1547).sample(_generated_models(), 60),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_optional_field_either_carries_the_marker_or_admits_null(
    name: str, model: type[BaseModel]
) -> None:
    """For a sample of models: the marker is the only thing that refuses ``None``, and
    pydantic's verdict on ``null`` follows the marker.

    Each optional field is probed in isolation with ``model_construct``-free input:
    a document carrying every required field's current default-less sentinel would
    need per-model synthesis, so the probe reads the field's own validator through
    the published schema instead — a marked field has no null arm, an unmarked
    optional field has one (or is a bare ``Any``).
    """
    schema = model.model_json_schema()
    required = set(schema.get("required", []))
    for field_name, field in model.model_fields.items():
        if field.is_required():
            continue
        wire_name = field.alias or field_name
        node = schema["properties"].get(wire_name)
        if node is None:
            continue
        marked = is_no_explicit_null(field)
        annotation = repr(field.annotation)
        if marked:
            assert wire_name not in required
            assert node.get("type") != "null"
            assert {"type": "null"} not in node.get("anyOf", []), (name, field_name, node)
        elif "None" in annotation and "Any" not in annotation and node.get("anyOf") is None:
            # An unmarked optional ``T | None`` must publish a null arm, unless the
            # type itself is a reference the schema expresses without ``anyOf``.
            assert "$ref" in node or node.get("type") == "null" or "allOf" in node, (
                name,
                field_name,
                node,
            )


def test_no_generated_module_still_carries_the_transient_mark() -> None:
    """The ``x-adcp-no-explicit-null`` key is a generator-internal carrier and never ships."""
    domains = Path(public_types.__file__).parent / "domains"
    offenders = [
        path.relative_to(domains)
        for path in domains.rglob("*.py")
        if "x-adcp-no-explicit-null" in path.read_text()
    ]
    assert offenders == []
    bundled = Path(public_types.__file__).parent.parent / "_schemas"
    if bundled.exists():
        for path in bundled.rglob("*.json"):
            assert "x-adcp-no-explicit-null" not in path.read_text(), path


def test_marker_is_widespread_in_the_generated_tree() -> None:
    """The rule is generator-level: it reaches thousands of fields across hundreds of modules."""
    domains = Path(public_types.__file__).parent / "domains"
    modules = [path for path in domains.rglob("*.py") if "NoExplicitNull" in path.read_text()]
    fields = sum(path.read_text().count("NoExplicitNull") - 1 for path in modules)
    assert len(modules) > 500
    assert fields > 8000
