"""An open ``format_kind`` vocabulary and tolerant readback (#1241/#1140).

``core/canonical-format-kind.json`` declares a closed 16-member ``enum`` and,
in the same file, requires a consumer to retain an unknown value and not fail
the payload — "the producer-side enum stays closed; the consumer-side enum
stays open for forward compatibility." The schema knows the rule is
DIRECTIONAL and then encodes it as one closed enum, which cannot carry that.

So this SDK does not reproduce the enum: ``format_kind`` is ``str`` at every
reference (``OPEN_VOCABULARY_SCHEMAS`` in ``scripts/generate_types.py``) and
**no model refuses a value, in either direction**. A pinned SDK cannot tell a
kind a seller invented from a kind defined after its pin, so refusing the
second to prevent the first would make this library's version a ceiling on
what the protocol permits. Checking the vocabulary is the caller's, through
``adcp.types.is_canonical_format_kind``. Upstream ask:
adcontextprotocol/adcp#7929.

What this module grades is therefore one thing rather than a pair: a value
this SDK does not recognise survives construction, validation, serialization
and re-parsing, on every model that carries the field, and nothing in the
published JSON schema says otherwise. The #1241 obligation this module was
written for — that delivery tolerance must not weaken creative input
validation — had its premise removed with the strict side; what is left of it
is graded in ``tests/test_delivery_manifest_readback.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from adcp.types import (
    CanonicalFormatKind,
    Creative,
    CreativeAsset,
    CreativeManifest,
    CreativeVariant,
    DeliveryCreative,
    Format,
    GetCreativeDeliveryResponse,
    SyncCreativesRequest,
    is_canonical_format_kind,
)
from adcp.types.aliases import DeliveryCreative as AliasDeliveryCreative
from adcp.types.creative import Creative as PartialCreative
from adcp.types.creative import CreativeAsset as PartialCreativeAsset
from adcp.types.creative import CreativeManifest as PartialCreativeManifest
from adcp.types.domains.core.creative_manifest import (
    CreativeManifest as GeneratedCreativeManifest,
)
from adcp.types.domains.creative.get_creative_delivery_response import (
    GetCreativeDeliveryResponse as GeneratedGetCreativeDeliveryResponse,
)

FUTURE_FORMAT_KIND = "future_canonical_format"


def _coordinated(format_kind: str) -> dict[str, object]:
    """``component_assets`` exactly when the kind is ``coordinated_placements``.

    ``creative-asset.json`` and ``creative-manifest.json`` both state it as one
    conditional: if the kind is ``coordinated_placements`` the manifest carries
    ``component_assets``, and otherwise it MUST NOT. So a factory parametrized
    over every kind has to follow the kind rather than pick one side.
    """
    return {"component_assets": {}} if format_kind == "coordinated_placements" else {}


def _creative_asset(format_kind: str) -> CreativeAsset:
    return CreativeAsset(
        creative_id="creative-1",
        name="Creative",
        format_kind=format_kind,
        assets={},
        **_coordinated(format_kind),
    )


def _creative(format_kind: str) -> Creative:
    now = datetime.now(timezone.utc)
    return Creative(
        creative_id="creative-1",
        name="Creative",
        format_kind=format_kind,
        status="approved",
        created_date=now,
        updated_date=now,
    )


def _delivery_creative(format_kind: str) -> DeliveryCreative:
    return DeliveryCreative(
        creative_id="creative-1",
        format_kind=format_kind,
        variants=[],
    )


def _creative_manifest(format_kind: str) -> CreativeManifest:
    return CreativeManifest(format_kind=format_kind, assets={}, **_coordinated(format_kind))


#: Every model that carries a ``format_kind``, in both directions. There is no
#: request/response split: the SDK refuses nothing, so the same expectations
#: hold on a model a buyer sends and on a row a seller returns.
FACTORIES = [_creative_asset, _creative, _creative_manifest, _delivery_creative]


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("value", [FUTURE_FORMAT_KIND, "totally_bogus", "IMAGE", ""])
def test_an_unrecognised_format_kind_is_retained_everywhere(factory, value) -> None:
    """The SDK does not discriminate, in either direction.

    ``""`` is in the list deliberately, and it is the sharpest case: the closed
    enum refused it, and ``core/canonical-format-kind.json`` is
    ``{"type": "string", "enum": [...]}`` with no ``minLength`` and no
    ``pattern``, so an open ``str`` has nothing to refuse it with. A seller
    sending an empty kind is a seller bug, and saying so is the CALLER's job
    through ``is_canonical_format_kind`` — this library carries the value.

    ``core/canonical-format-kind.json`` requires a consumer to retain an
    unknown value, and a pinned SDK cannot tell a kind a seller invented from a
    kind defined after its pin — so refusing on the way out would make this
    library's version a ceiling on what the protocol permits. Retention is
    asserted on the attribute AND on the wire, because dropping it at
    serialization would satisfy a weaker check.

    Whether to accept a kind is the caller's decision, through
    ``adcp.types.is_canonical_format_kind``, graded by
    ``tests/test_open_format_kind_vocabulary.py``.
    """
    model = factory(value)
    assert model.format_kind == value
    assert model.model_dump(mode="json")["format_kind"] == value
    assert json.loads(model.model_dump_json())["format_kind"] == value
    assert not is_canonical_format_kind(value)


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("json_input", [False, True], ids=["python", "json"])
def test_raw_validation_retains_an_unrecognised_format_kind(factory, json_input) -> None:
    """Round-tripping a document through the model keeps the value it carried."""
    model = factory("image")
    payload = model.model_dump(mode="json", exclude_unset=True)
    payload["format_kind"] = FUTURE_FORMAT_KIND

    reparsed = (
        type(model).model_validate_json(json.dumps(payload))
        if json_input
        else type(model).model_validate(payload)
    )
    assert reparsed.format_kind == FUTURE_FORMAT_KIND


@pytest.mark.parametrize("factory", FACTORIES)
def test_no_model_publishes_a_vocabulary_constraint(factory) -> None:
    """The published JSON schema agrees with the runtime: ``type: string``.

    A model whose schema said ``enum`` while its validator accepted anything
    would be the same disagreement in the other direction. Graded through the
    bundled validator rather than by reading the node, so it is the behaviour
    being checked.
    """
    model = factory("image")
    validator = Draft202012Validator(type(model).model_json_schema())
    payload = model.model_dump(mode="json", exclude_unset=True)
    validator.validate(payload)

    payload["format_kind"] = FUTURE_FORMAT_KIND
    assert list(validator.iter_errors(payload)) == []
    assert type(model).model_validate(payload).format_kind == FUTURE_FORMAT_KIND


@pytest.mark.parametrize(
    ("public", "partial"),
    [
        (CreativeAsset, PartialCreativeAsset),
        (Creative, PartialCreative),
        (CreativeManifest, PartialCreativeManifest),
    ],
)
def test_partial_imports_use_the_same_creative_models(public, partial) -> None:
    assert public is partial


@pytest.mark.parametrize("factory", [_creative_asset, _creative])
def test_creative_format_kind_remains_required_and_non_nullable(factory) -> None:
    model = factory("image")
    payload = model.model_dump(mode="json", exclude_unset=True)
    del payload["format_kind"]
    with pytest.raises(ValidationError) as missing:
        type(model).model_validate(payload)
    assert missing.value.errors()[0]["loc"] == ("format_kind",)
    assert missing.value.errors()[0]["type"] == "missing"

    payload["format_kind"] = None
    with pytest.raises(ValidationError) as null:
        type(model).model_validate(payload)
    assert null.value.errors()[0]["loc"] == ("format_kind",)


def test_manifest_format_kind_keeps_its_optional_default() -> None:
    """``format_kind`` is a declared-optional field, and omitting it entirely is refused.

    This used to construct ``CreativeManifest(assets={})`` to show the field defaults
    to ``None``. That document is schema-invalid: core/creative-manifest.json's root
    ``oneOf`` takes one of ``format_id`` / ``format_kind``, and #1368's
    enforce_root_required_groups now holds it at runtime. The canonical manifest cannot
    use the ``format_id`` arm either -- it strips legacy creative identity -- so on this
    surface ``format_kind`` is the only satisfiable arm.

    The obligation the old construction stood for is that codegen must not mark the
    field required, which is read off the declaration directly rather than inferred
    from a construction that the document rule now forbids.
    """
    field = CreativeManifest.model_fields["format_kind"]
    assert not field.is_required()
    assert field.default is None

    with pytest.raises(ValidationError):
        CreativeManifest(assets={})

    # An explicit ``None`` does NOT satisfy the document rule. It used to, while
    # the compiled check read membership alone, and that was wrong in two ways
    # the bundled schema states plainly: ``format_kind`` is an enum of strings,
    # so ``null`` is not a value it takes, and the client drops a null-valued key
    # on the way out -- so the document SENT carries neither arm's field. The
    # group is keyed on the field being present on what is sent.
    with pytest.raises(ValidationError):
        CreativeManifest(assets={}, format_kind=None)

    # Naming the arm is what satisfies it.
    named = CreativeManifest(assets={}, format_kind="image")
    assert named.format_kind == "image"


@pytest.mark.parametrize("json_input", [False, True], ids=["python", "json"])
def test_sync_request_carries_an_unrecognised_creative_format_kind(json_input) -> None:
    """A whole request nests the value unchanged, not only a bare asset.

    This used to assert a refusal. A seller on this SDK may legitimately need
    to send a kind defined after its pin, and the library has no way to tell
    that apart from an invented one, so it carries the value and the seller
    decides — see ``adcp.types.is_canonical_format_kind``.
    """
    payload = {
        "account": {"account_id": "account-1"},
        "idempotency_key": "creative-sync-idempotency-1",
        "creatives": [_creative_asset("image").model_dump(mode="json", exclude_unset=True)],
    }
    SyncCreativesRequest.model_validate(payload)
    payload["creatives"][0]["format_kind"] = FUTURE_FORMAT_KIND

    request = (
        SyncCreativesRequest.model_validate_json(json.dumps(payload))
        if json_input
        else SyncCreativesRequest.model_validate(payload)
    )
    assert request.creatives[0].format_kind == FUTURE_FORMAT_KIND
    assert json.loads(request.model_dump_json())["creatives"][0]["format_kind"] == (
        FUTURE_FORMAT_KIND
    )


def test_public_variant_carries_an_unrecognised_manifest_format_kind() -> None:
    """Nested one level deeper, the value still survives."""
    variant = {"variant_id": "variant-1", "manifest": {"assets": {}, "format_kind": "image"}}
    CreativeVariant.model_validate(variant)
    variant["manifest"]["format_kind"] = FUTURE_FORMAT_KIND

    parsed = CreativeVariant.model_validate(variant)
    assert parsed.manifest is not None
    assert parsed.manifest.format_kind == FUTURE_FORMAT_KIND


@pytest.mark.parametrize(
    ("response_type", "input_manifest_type"),
    [
        (GetCreativeDeliveryResponse, CreativeManifest),
        (GeneratedGetCreativeDeliveryResponse, GeneratedCreativeManifest),
    ],
    ids=["canonical", "generated"],
)
def test_unknown_nested_manifest_kind_round_trips_in_delivery_readback(
    response_type, input_manifest_type
) -> None:
    payload = {
        "currency": "USD",
        "reporting_period": {
            "start": "2026-09-01T00:00:00Z",
            "end": "2026-09-02T00:00:00Z",
        },
        "creatives": [
            {
                "creative_id": "creative-1",
                "format_kind": FUTURE_FORMAT_KIND,
                "variants": [
                    {
                        "variant_id": "variant-1",
                        "manifest": {"assets": {}, "format_kind": FUTURE_FORMAT_KIND},
                    }
                ],
            }
        ],
    }
    delivery = response_type.model_validate(payload)
    manifest = delivery.creatives[0].variants[0].manifest
    assert manifest is not None
    assert type(manifest) is not input_manifest_type
    assert type(delivery.creatives[0].variants[0]) is not CreativeVariant
    assert manifest.format_kind == FUTURE_FORMAT_KIND

    encoded = delivery.model_dump_json()
    assert response_type.model_validate_json(encoded).model_dump(mode="json") == (
        delivery.model_dump(mode="json")
    )

    # Both the canonical and the generated manifest accept the nested document
    # and retain the kind. One class serves both directions — the same
    # ``core/creative-manifest.json`` is referenced by
    # ``sync-creatives-request.json`` and by
    # ``get-creative-delivery-response.json`` — and there is no second, stricter
    # class for it to be measured against any more.
    nested = payload["creatives"][0]["variants"][0]["manifest"]
    assert input_manifest_type.model_validate(nested).format_kind == FUTURE_FORMAT_KIND


@pytest.mark.parametrize(
    "factory",
    [_creative_asset, _creative, _delivery_creative, _creative_manifest],
)
@pytest.mark.parametrize("kind", list(CanonicalFormatKind))
def test_a_canonical_format_kind_round_trips_as_the_string_it_is(factory, kind) -> None:
    """Every one of the sixteen, on every model, python and JSON.

    ``format_kind`` is ``str`` by decision (see the module docstring), so a
    canonical value is carried as itself rather than coerced to an enum member.
    The value is asserted against ``CanonicalFormatKind`` rather than a literal
    so the vocabulary stays the subject, and the ``str``-ness is asserted too —
    a reintroduced enum would satisfy ``==`` and fail ``type() is str``.
    """
    model = factory(kind.value)

    assert model.format_kind == kind
    assert type(model.format_kind) is str
    assert model.model_dump(mode="json")["format_kind"] == kind.value
    reparsed = type(model).model_validate_json(model.model_dump_json())
    assert reparsed.format_kind == kind
    assert type(reparsed.format_kind) is str


def test_delivery_creative_alias_is_open_and_keeps_its_identity() -> None:
    assert AliasDeliveryCreative is DeliveryCreative

    model = AliasDeliveryCreative(
        creative_id="creative-1",
        format_kind=FUTURE_FORMAT_KIND,
        variants=[],
    )
    assert model.format_kind == FUTURE_FORMAT_KIND


def _format_schema() -> dict[str, str]:
    return {
        "uri": "https://example.com/custom-format.json",
        "digest": f"sha256:{'0' * 64}",
    }


def test_custom_format_requires_shape() -> None:
    with pytest.raises(ValidationError, match="custom formats require format_shape"):
        Format(format_kind="custom", params={}, format_schema=_format_schema())


def test_custom_format_requires_schema() -> None:
    with pytest.raises(ValidationError, match="custom formats require format_schema"):
        Format(format_kind="custom", params={}, format_shape="new_shape")


def test_custom_format_accepts_shape_and_schema() -> None:
    model = Format(
        format_kind="custom",
        params={},
        format_shape="new_shape",
        format_schema=_format_schema(),
    )

    assert model.format_kind == CanonicalFormatKind.custom
    assert model.format_shape == "new_shape"
    assert model.format_schema is not None


@pytest.mark.parametrize("field", ["format_shape", "format_schema"])
def test_non_custom_format_rejects_custom_fields(field: str) -> None:
    value = "new_shape" if field == "format_shape" else _format_schema()

    with pytest.raises(
        ValidationError,
        match="format_shape and format_schema are only valid for custom formats",
    ):
        Format(format_kind="image", params={}, **{field: value})
