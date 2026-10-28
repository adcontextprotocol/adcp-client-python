"""A capabilities block carries only what the seller declared (#1684, #1686).

Two SDK 10 beta reports from the same mechanism (#1613): the generator wrote
every schema default into the generated capabilities models and the default
dump emitted every field, so a seller's capabilities block advertised things
the seller never declared.

* #1684: ``Signals`` defaulted ``data_provider_domains_complete=True`` and the
  3.3 schema's ``dependencies`` rule requires ``data_provider_domains`` whenever
  that flag is present. A seller declaring signals without provider domains,
  which is legitimate, failed the SDK's own capabilities validation at boot.
* #1686: ``WebhookSigning(supported=False)`` advertised
  ``profiles: ["adcp/webhook-signing/v1"]``.

Both models come straight from the generator (``adcp.decisioning.capabilities``
re-exports ``adcp.types.capabilities``, which binds the generated classes), so
the ``OmitUnsetDefault`` marker reaches them: an unset schema default stays
off the dump, the attribute still reads the default, and a seller that wants
the value on the wire sets it. These tests pin that for the two reported
fields and for every schema-optional default in the capabilities response.
"""

from __future__ import annotations

import typing
from typing import Literal

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

import adcp.types.capabilities as capabilities
from adcp.decisioning.capabilities import Signals
from adcp.types.base import AdCPBaseModel, is_omit_unset_default, omit_unset_default_fields
from adcp.types.domains.protocol.get_adcp_capabilities_response import WebhookSigning
from adcp.validation.schema_validator import validate_response


def _capabilities(**sections: object) -> dict[str, object]:
    return {
        "status": "completed",
        "adcp": {"major_versions": [3], "idempotency": {"supported": False}},
        "supported_protocols": sorted(sections),
        **sections,
    }


# --- #1684 ------------------------------------------------------------------------


def test_signals_without_provider_domains_validates_at_boot() -> None:
    """The issue's reproduction: a legitimate signals block passes the SDK's own check."""
    signals = Signals(discovery_modes=["brief"]).model_dump(mode="json", exclude_none=True)
    assert signals == {"discovery_modes": ["brief"]}
    assert validate_response("get_adcp_capabilities", _capabilities(signals=signals)).issues == []


def test_the_completeness_flag_reads_its_default_but_is_not_advertised() -> None:
    model = Signals(discovery_modes=["brief"])
    assert model.data_provider_domains_complete is True
    assert "data_provider_domains_complete" not in model.model_dump(mode="json")
    # The SDK 9 output, on request.
    assert model.model_dump(mode="json", exclude_unset=False)["data_provider_domains_complete"]


def test_a_declared_flag_is_advertised_with_its_list() -> None:
    model = Signals(
        discovery_modes=["brief"],
        data_provider_domains=["provider.example"],
        data_provider_domains_complete=True,
    )
    signals = model.model_dump(mode="json")
    assert signals["data_provider_domains_complete"] is True
    assert validate_response("get_adcp_capabilities", _capabilities(signals=signals)).issues == []


def test_a_declared_flag_without_its_list_is_still_the_schema_violation_it_is() -> None:
    """The ``dependencies`` rule is the schema's, and validation names it.

    A construction-time refusal is deliberately not added here: the compiled
    constraints the generator rewrite (#1522) emits cover ``dependencies`` on
    every schema, and a bespoke validator on this one class would be
    superseded by it.
    """
    signals = Signals(discovery_modes=["brief"], data_provider_domains_complete=True).model_dump(
        mode="json"
    )
    assert signals["data_provider_domains_complete"] is True
    issues = validate_response("get_adcp_capabilities", _capabilities(signals=signals)).issues
    assert [(issue.pointer, issue.keyword) for issue in issues] == [("/signals", "dependencies")]


# --- #1686 ------------------------------------------------------------------------


def test_unsupported_webhook_signing_advertises_no_profile() -> None:
    assert WebhookSigning(supported=False).model_dump(mode="json", exclude_none=True) == {
        "supported": False
    }


def test_supported_webhook_signing_advertises_only_what_the_seller_set() -> None:
    """``profiles`` has a schema default, and omitting it means that default.

    The schema declares ``profiles`` default ``["adcp/webhook-signing/v1"]``, so
    a reader that applies defaults sees the same thing whether the key is
    present or absent; a seller that wants the profile on the wire sets it.
    Emitting it only when ``supported`` is true would make the dump depend on
    a sibling value, which is the kind of hidden rule #1613 removed.
    """
    assert WebhookSigning(supported=True).model_dump(mode="json") == {"supported": True}
    explicit = WebhookSigning(supported=True, profiles=["adcp/webhook-signing/v1"])
    assert explicit.model_dump(mode="json")["profiles"] == ["adcp/webhook-signing/v1"]
    parsed = WebhookSigning.model_validate_json(
        '{"supported": true, "profiles": ["adcp/webhook-signing/v1"]}'
    )
    assert parsed.model_dump(mode="json") == {
        "supported": True,
        "profiles": ["adcp/webhook-signing/v1"],
    }
    assert [p.value for p in WebhookSigning(supported=True).profiles] == ["adcp/webhook-signing/v1"]


def test_webhook_signing_defaults_round_trip_through_the_capabilities_schema() -> None:
    for supported in (False, True):
        block = WebhookSigning(supported=supported).model_dump(mode="json")
        response = {**_capabilities(media_buy={}), "webhook_signing": block}
        outcome = validate_response("get_adcp_capabilities", response)
        assert outcome.issues == [], (supported, outcome.issues)


# --- every capabilities sub-model --------------------------------------------------


def _capability_models() -> list[type[AdCPBaseModel]]:
    found: dict[type, None] = {}
    for name in dir(capabilities):
        obj = getattr(capabilities, name)
        if isinstance(obj, type) and issubclass(obj, AdCPBaseModel):
            found[obj] = None
    return sorted(found, key=lambda model: model.__name__)


def _is_single_literal(annotation: object) -> bool:
    return typing.get_origin(annotation) is Literal and len(typing.get_args(annotation)) == 1


@pytest.mark.parametrize("model", _capability_models(), ids=lambda model: model.__name__)
def test_every_schema_default_in_the_capabilities_response_stays_off_an_unset_dump(
    model: type[BaseModel],
) -> None:
    """A non-None default on a capabilities field is a schema default and is marked.

    The exceptions are the generator-injected single-value ``Literal``
    discriminators, which the wire requires and which are emitted either way.
    """
    unmarked = {
        name: field.default
        for name, field in model.model_fields.items()
        if field.default is not PydanticUndefined
        and field.default is not None
        and not _is_single_literal(field.annotation)
        and not is_omit_unset_default(field)
    }
    assert unmarked == {}, f"{model.__name__} would advertise {unmarked} unset"
    if not any(field.is_required() for field in model.model_fields.values()):
        dumped = model().model_dump(mode="json")
        leaked = {key for key in dumped if key in omit_unset_default_fields(model)}
        assert leaked == set(), f"{model.__name__} emitted {leaked} without the caller setting them"
