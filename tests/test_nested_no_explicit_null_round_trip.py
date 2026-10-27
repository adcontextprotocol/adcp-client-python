"""A nested AdCP model leaves unset optional fields off the wire from any parent (#1587).

``AdCPBaseModel.model_dump`` defaults ``exclude_none=True``, but a plain pydantic
parent's serializer never calls that override, so a nested AdCP model wrote
``"ext": null`` -- and since #1555 the same model refuses that ``null`` on the way
back in. The fix moves the omission to the field: every ``NoExplicitNull``-marked
field carries ``exclude_if`` (pydantic-core level, no Python wrap serializer), so
a ``None`` is left off wherever the model sits, and ``model_validate_json`` of
``model_dump_json`` round-trips from a direct field, a list, a dict, several
plain levels down, and a ``RootModel`` over an AdCP union.

The published JSON Schema is unchanged in both modes, and a property whose
schema admits ``null`` is still emitted as ``null`` when the dump asks for it.
"""

from __future__ import annotations

import copy
import json
import warnings
from typing import Any

import pytest
from pydantic import BaseModel, RootModel, ValidationError
from pydantic.fields import FieldInfo

import adcp.types as public_types
from adcp.types import (
    CpmVendorPricingOption,
    Product,
    ReportingWebhook,
    SyncCreativesRequest,
    TrustedMatch,
    UpdateMediaBuyRequest,
    VendorPricingOptionUnion,
)
from adcp.types.base import _REFUSE_JSON_NULL, AdCPBaseModel, NoExplicitNull, is_no_explicit_null
from adcp.types.domains.core.product import Provider
from adcp.types.domains.creative.sync_creatives_request import (
    SyncCreativesRequest as GeneratedSyncCreativesRequest,
)
from adcp.types.domains.media_buy.create_media_buy_request import CreateMediaBuyRequest
from tests.conformance import _schema_parity as parity

# --- the reported case -----------------------------------------------------------


class _Parent(BaseModel):
    """The issue's parent: an ordinary pydantic model holding AdCP models."""

    pricing: CpmVendorPricingOption
    trusted_match: TrustedMatch


def _parent() -> _Parent:
    return _Parent(
        pricing=CpmVendorPricingOption(
            pricing_option_id="cpm", model="cpm", cpm=1.0, currency="USD"
        ),
        trusted_match=TrustedMatch(context_match=True),
    )


def test_issue_repro_round_trips_from_a_plain_parent() -> None:
    parent = _parent()
    body = parent.model_dump_json()
    document = json.loads(body)
    assert "ext" not in document["pricing"]
    assert "providers" not in document["trusted_match"]
    assert _Parent.model_validate_json(body) == parent


def test_nested_dump_matches_the_direct_dump() -> None:
    """The model serializes the same way wherever it sits.

    The parent passes no ``exclude_none``, so the precise statement is that the
    nested document equals the direct dump under ``exclude_none=False``; with no
    nullable field set to ``None`` here, that is also the default direct dump.
    """
    parent = _parent()
    document = parent.model_dump(mode="json")
    assert document["pricing"] == parent.pricing.model_dump(mode="json")
    assert document["trusted_match"] == parent.trusted_match.model_dump(mode="json")
    assert document["trusted_match"] == parent.trusted_match.model_dump(
        mode="json", exclude_none=False
    )
    assert json.loads(parent.model_dump_json())["pricing"] == json.loads(
        parent.pricing.model_dump_json()
    )


def test_provider_level_nesting_round_trips() -> None:
    """One level down: a provider that omits ``countries`` / ``uid_types``."""
    parent = _Parent(
        pricing=_parent().pricing,
        trusted_match=TrustedMatch(
            context_match=True,
            providers=[Provider(agent_url="https://tmp.example.com/", identity_match=True)],
        ),
    )
    body = parent.model_dump_json()
    provider = json.loads(body)["trusted_match"]["providers"][0]
    assert "countries" not in provider
    assert "uid_types" not in provider
    assert _Parent.model_validate_json(body) == parent


class _VendorPricing(RootModel[VendorPricingOptionUnion]):
    """A ``RootModel`` over an AdCP union."""


_PRICING_DOCUMENT = {"pricing_option_id": "cpm", "model": "cpm", "cpm": 1.0, "currency": "USD"}


def test_root_model_over_an_adcp_union_round_trips() -> None:
    wrapped = _VendorPricing.model_validate(_PRICING_DOCUMENT)
    assert isinstance(wrapped.root, AdCPBaseModel)
    body = wrapped.model_dump_json()
    assert "ext" not in json.loads(body)
    assert _VendorPricing.model_validate_json(body) == wrapped


class _Containers(BaseModel):
    items: list[CpmVendorPricingOption]
    by_key: dict[str, TrustedMatch]


def test_list_and_dict_containers_round_trip() -> None:
    parent = _Containers(
        items=[_parent().pricing, _parent().pricing],
        by_key={"a": TrustedMatch(context_match=True), "b": TrustedMatch(context_match=False)},
    )
    body = parent.model_dump_json()
    document = json.loads(body)
    assert all("ext" not in item for item in document["items"])
    assert all("providers" not in item for item in document["by_key"].values())
    assert _Containers.model_validate_json(body) == parent


class _Inner(BaseModel):
    parent: _Parent


class _Middle(BaseModel):
    inners: list[_Inner]


class _Outer(BaseModel):
    middle: _Middle
    pricing_by_key: dict[str, _VendorPricing]


def test_several_plain_levels_down_round_trip() -> None:
    outer = _Outer(
        middle=_Middle(inners=[_Inner(parent=_parent())]),
        pricing_by_key={"cpm": _VendorPricing.model_validate(_PRICING_DOCUMENT)},
    )
    body = outer.model_dump_json()
    assert "null" not in body
    assert _Outer.model_validate_json(body) == outer
    assert _Outer.model_validate(outer.model_dump(mode="json")) == outer


# --- legitimate nulls are kept ------------------------------------------------------

_NULLABLE_UPDATE: dict[str, Any] = {
    "account": {"account_id": "acct_1"},
    "media_buy_id": "mb_1",
    "idempotency_key": "nullable-clear-0001",
    # Both admit ``null`` in the bundled schema (a clear), neither is marked.
    "budget_cap_timezone": None,
    "daily_budget_cap": None,
}


class _Holder(BaseModel):
    request: UpdateMediaBuyRequest


def test_nullable_fields_still_emit_null_from_a_plain_parent() -> None:
    """A schema-admitted ``null`` is a value, and the parent's dump keeps it."""
    holder = _Holder(request=UpdateMediaBuyRequest.model_validate(_NULLABLE_UPDATE))
    for body in (holder.model_dump_json(), holder.model_dump_json(exclude_none=False)):
        document = json.loads(body)["request"]
        assert document["budget_cap_timezone"] is None
        assert document["daily_budget_cap"] is None
        # Marked optional fields that were never supplied stay off the wire.
        assert "name" not in document
        assert "push_notification_config" not in document
        assert _Holder.model_validate_json(body) == holder


def test_exclude_none_false_on_a_direct_dump_keeps_only_schema_admitted_nulls() -> None:
    """``exclude_none=False`` is still honoured where ``null`` can mean something.

    A marked field is left off even then: ``null`` is never a valid value for it,
    so there is nothing to keep, and the document stays readable in JSON mode.
    """
    request = UpdateMediaBuyRequest.model_validate(_NULLABLE_UPDATE)
    assert "budget_cap_timezone" not in request.model_dump()
    loose = request.model_dump(exclude_none=False)
    assert loose["budget_cap_timezone"] is None
    assert loose["daily_budget_cap"] is None
    assert "name" not in loose
    assert "push_notification_config" not in loose
    wire = request.model_dump_json(exclude_none=False)
    assert UpdateMediaBuyRequest.model_validate_json(wire).budget_cap_timezone is None
    assert "budget_cap_timezone" in UpdateMediaBuyRequest.model_validate_json(wire).model_fields_set


def test_exclude_none_false_keeps_an_unmarked_none_inside_an_open_dict() -> None:
    """Only marked *fields* are affected; a ``None`` value in an opaque object is data.

    ``ext`` (like ``context``) keeps its nulls on every dump path (#1575/#1600).
    """
    pricing = CpmVendorPricingOption(
        pricing_option_id="cpm", model="cpm", cpm=1.0, currency="USD", ext={"note": None}
    )
    assert pricing.model_dump(exclude_none=False)["ext"] == {"note": None}
    assert pricing.model_dump()["ext"] == {"note": None}


# --- the marker carries both halves --------------------------------------------------


def test_marker_carries_the_exclusion_and_the_refusal() -> None:
    field = TrustedMatch.model_fields["providers"]
    assert is_no_explicit_null(field)
    assert field.exclude_if is NoExplicitNull.exclude_if
    assert field.exclude_if(None) is True
    assert field.exclude_if([]) is False
    assert isinstance(NoExplicitNull, FieldInfo)
    assert _REFUSE_JSON_NULL in field.metadata
    assert [repr(item) for item in field.metadata if item is _REFUSE_JSON_NULL] == [
        "NoExplicitNull"
    ]
    # The marker is merged into the field, never itself in ``metadata``; the
    # halves are distinct objects and compare by identity.
    assert NoExplicitNull not in field.metadata
    assert _REFUSE_JSON_NULL != NoExplicitNull
    # A generated ``Field(description=...)`` beside the marker survives the merge.
    assert field.description


def test_forward_compat_reporting_operation_id_is_left_off_when_unset() -> None:
    """The hand-written ``_ReportingOperationId`` annotation uses the same marker."""
    field = ReportingWebhook.model_fields["operation_id"]
    assert is_no_explicit_null(field)
    assert _REFUSE_JSON_NULL in field.metadata

    class Holder(BaseModel):
        webhook: ReportingWebhook

    holder = Holder(
        webhook=ReportingWebhook.model_validate(
            {
                "url": "https://buyer.example.com/hook",
                "authentication": {"schemes": ["Bearer"], "credentials": "x" * 40},
                "reporting_frequency": "daily",
            }
        )
    )
    body = holder.model_dump_json()
    assert "operation_id" not in json.loads(body)["webhook"]
    assert Holder.model_validate_json(body) == holder


def test_canonical_redeclaration_inherits_the_exclusion() -> None:
    """``canonical_creative._inherit`` deep-copies the generated ``FieldInfo``."""
    canonical = SyncCreativesRequest.model_fields["creatives"]
    generated = GeneratedSyncCreativesRequest.model_fields["creatives"]
    assert canonical is not generated
    assert is_no_explicit_null(canonical)
    assert _REFUSE_JSON_NULL in canonical.metadata
    assert is_no_explicit_null(copy.deepcopy(canonical))

    class Holder(BaseModel):
        request: SyncCreativesRequest

    holder = Holder(
        request=SyncCreativesRequest(
            adcp_version="3.2",
            account={"account_id": "acct_1"},
            idempotency_key="repro-null-creatives-0001",
            assignment_operations=[
                {"operation": "unassign", "creative_id": "cr_1", "package_id": "pkg_1"}
            ],
        )
    )
    body = holder.model_dump_json()
    assert "creatives" not in json.loads(body)["request"]
    assert Holder.model_validate_json(body).request.creatives is None


# --- the published JSON Schema is unchanged ------------------------------------------


def _without_exclusion(model: type[BaseModel]) -> type[BaseModel]:
    """The same model as it was before #1587: every field's ``exclude_if`` cleared.

    A subclass under the original name and docstring, so titles and
    descriptions match; only the serialization exclusion differs.
    """
    namespace: dict[str, Any] = {
        "__module__": model.__module__,
        "__qualname__": model.__qualname__,
        "__doc__": model.__doc__,
        "__annotations__": {},
    }
    for name, field in model.model_fields.items():
        # A deep copy: ``copy.copy`` would share ``_attributes_set`` with the
        # real field, and popping from it would strip the exclusion from every
        # later redeclaration of that field.
        twin = copy.deepcopy(field)
        twin.exclude_if = None
        twin._attributes_set.pop("exclude_if", None)
        namespace["__annotations__"][name] = field.annotation
        namespace[name] = twin
    return type(model.__name__, (model,), namespace)


@pytest.mark.parametrize(
    "model",
    [
        CpmVendorPricingOption,
        TrustedMatch,
        Provider,
        Product,
        SyncCreativesRequest,
        GeneratedSyncCreativesRequest,
        UpdateMediaBuyRequest,
        CreateMediaBuyRequest,  # nested packages
        ReportingWebhook,
    ],
    ids=lambda model: model.__name__,
)
def test_published_schema_is_unchanged_by_the_exclusion(model: type[BaseModel]) -> None:
    before = _without_exclusion(model)
    assert any(is_no_explicit_null(f) for f in model.model_fields.values())
    assert all(f.exclude_if is None for f in before.model_fields.values())
    # The real fields are untouched by building the twin.
    for field in model.model_fields.values():
        if is_no_explicit_null(field):
            assert "exclude_if" in field._attributes_set
    for mode in ("validation", "serialization"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert model.model_json_schema(mode=mode) == before.model_json_schema(mode=mode)


# --- sweeps over the generated tree ------------------------------------------------------


def _adcp_models() -> list[tuple[str, type[AdCPBaseModel]]]:
    models = []
    for name in sorted(dir(public_types)):
        if name.startswith("_"):
            continue
        obj = getattr(public_types, name, None)
        if isinstance(obj, type) and issubclass(obj, AdCPBaseModel) and obj.model_fields:
            models.append((name, obj))
    return models


def test_every_marked_field_in_the_generated_tree_carries_the_exclusion() -> None:
    """The two halves never come apart: marked iff excluded-when-None."""
    marked = 0
    for name, model in _adcp_models():
        for field_name, field in model.model_fields.items():
            refuses = _REFUSE_JSON_NULL in field.metadata
            if refuses:
                marked += 1
                assert is_no_explicit_null(field), (name, field_name)
                assert not field.is_required(), (name, field_name)
            else:
                assert field.exclude_if is None, (name, field_name)
    assert marked > 5000


def test_default_instances_of_every_model_dump_no_null_for_marked_fields() -> None:
    """Each public model, with only its defaults, dumped by a plain parent.

    ``model_construct`` fills every optional field with its default, so the
    dump is the worst case: every marked field holds ``None``. A plain parent
    passes no ``exclude_none``, so the only thing keeping ``null`` off the wire
    is the field's own exclusion. A ``null`` that does appear must belong to
    an unmarked (schema-nullable) field.
    """
    checked = 0
    for name, model in _adcp_models():
        instance = model.model_construct()
        parent = RootModel[model].model_construct(root=instance)  # type: ignore[valid-type]
        try:
            document = parent.model_dump(mode="json")
        except Exception:  # noqa: BLE001 -- a serializer that needs a validated instance
            continue
        if not isinstance(document, dict):
            continue
        checked += 1
        for field_name, field in model.model_fields.items():
            if is_no_explicit_null(field):
                for key in {field_name, field.alias or field_name}:
                    assert document.get(key, "absent") is not None, (name, field_name)
    assert checked > 600


def _models_for(tool: str) -> list[type[AdCPBaseModel]]:
    """The canonical request model first, then the ``Legacy*`` spelling when there is one.

    A synthesized document can carry legacy creative identity, which the
    canonical model refuses by design; the legacy model reads the same shape.
    """
    camel = "".join(part.capitalize() for part in tool.split("_"))
    models = []
    for name in (f"{camel}Request", camel, f"Legacy{camel}Request"):
        model = getattr(public_types, name, None)
        if isinstance(model, type) and issubclass(model, AdCPBaseModel):
            models.append(model)
    return models


@pytest.mark.parametrize(
    "tool",
    [
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_products",
        "activate_signal",
        "list_creatives",
        "get_media_buy_delivery",
        "provide_performance_feedback",
        "build_creative",
        "buy_products",
        "sync_audiences",
        "get_adcp_capabilities",
    ],
)
def test_synthesized_requests_round_trip_from_a_plain_parent(tool: str) -> None:
    """A schema-valid request, grown to reach its optional properties, through a parent."""
    models = _models_for(tool)
    if not models:
        pytest.skip(f"{tool} exposes no request model under adcp.types")
    try:
        document = parity.synthesize(tool)
    except parity.UnsynthesizableError as exc:
        pytest.skip(str(exc))
    request: AdCPBaseModel | None = None
    for model in models:
        try:
            request = model.model_validate(document)
        except ValidationError:
            continue
        break
    assert request is not None, f"no model under adcp.types reads the synthesized {tool}"

    class Holder(BaseModel):
        request: type(request)  # type: ignore[valid-type]

    holder = Holder(request=request)
    # ``by_alias``: the wire spelling, so a field such as ``pass_`` reads back
    # under its alias. The parent still passes no ``exclude_none``.
    body = holder.model_dump_json(by_alias=True)
    assert Holder.model_validate_json(body) == holder
    # The nested document is the direct dump under the parent's ``exclude_none``
    # (``False``): a schema-admitted ``null`` the synthesizer set is kept, a
    # marked field's ``None`` is not.
    assert json.loads(body)["request"] == json.loads(
        request.model_dump_json(by_alias=True, exclude_none=False)
    )
