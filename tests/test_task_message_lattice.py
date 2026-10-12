"""The request/response lattice: every registered task message is a typed AdCP message.

A caller that holds a request should be able to resolve its account, decide at-most-once,
echo its context and negotiate version before knowing which tool it is; a caller that
holds a response should be able to route on task state and split envelope from payload the
same way. That needs the task messages to share a TYPE, which is what ``AdcpRequest`` and
``AdcpResponse`` are -- and ``issubclass(model, AdcpRequest)`` is then the
registration-time proof that a model is spec-derived rather than a hand-written parallel
(a field test passes for a forged model; descent does not).

The ratchet below is what keeps that true across a schema bump. Each envelope residue is
pinned to the exact set of schemas measured to lack the ancestry, with the upstream issue
that owns it, and the assertions are equality: a schema that regains the ancestry fails
until its entry is REMOVED, and a schema that loses it fails with its own name. Residues
shrink; they never grow.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from adcp.types.base import AdcpRequest, AdcpResponse, _AdcpMessage
from adcp.types.domains.core.protocol_envelope import ProtocolEnvelope
from adcp.types.domains.core.version_envelope import AdcpVersionEnvelope
from scripts.generate_types import normalize_version_envelope_composition
from scripts.post_generate_fixes import OUTPUT_DIR, SCHEMA_DIR
from scripts.task_message_lattice import task_message_roots

# ``creative/validate-input-request.json`` declares no version fields at all, so there is
# nothing for the normalization in ``generate_types`` to lift into a composition -- the
# SDK will not invent a field the schema does not have. Upstream ask: declare the envelope
# like every sibling task does (one ``allOf`` line).
_REQUEST_VERSION_RESIDUE = frozenset({"creative/validate-input-request.json"})

# The five compact media-buy responses compose ``media-buy/media-buy-commitment-response``
# instead, which is itself envelope-free. The two proposals responses declare the version
# via pointer refs, and the normalization in ``generate_types`` deliberately REFUSES them:
# their submitted arm is a bare ``$ref`` to ``core/compact-task-submitted.json``, so moving
# the stubs into a root ``allOf`` would delete the field from that arm rather than relocate
# it (see ``_has_bare_ref_root_arm``). Normalizing them would not have changed this residue
# anyway -- the submitted arm gains no ancestry either way, so the all-arms answer is 70/77
# with or without. Upstream: adcp#7892 plus the commitment / compact-task-submitted
# components, which is upstream ask four.
_RESPONSE_VERSION_RESIDUE = frozenset(
    {
        "media-buy/accept-proposal-response.json",
        "media-buy/buy-products-response.json",
        "media-buy/control-media-buy-response.json",
        "media-buy/decline-proposals-response.json",
        "media-buy/list-products-response.json",
        "media-buy/refine-proposals-response.json",
        "media-buy/request-proposals-response.json",
    }
)

# These ten declare NO protocol-envelope fields of their own, against the envelope's own
# "The ``status`` field is REQUIRED on every task response envelope". There is nothing
# equivalent to lift, so unlike the version envelope this one cannot be normalized by
# equivalence: composing it would change what validates. Upstream's to fix.
_RESPONSE_PROTOCOL_RESIDUE = frozenset(
    {
        "governance/check-governance-response.json",
        "governance/report-plan-adjustment-response.json",
        "governance/report-plan-outcome-response.json",
        "media-buy/accept-proposal-response.json",
        "media-buy/buy-products-response.json",
        "media-buy/control-media-buy-response.json",
        "media-buy/decline-proposals-response.json",
        "media-buy/list-products-response.json",
        "media-buy/refine-proposals-response.json",
        "media-buy/request-proposals-response.json",
    }
)

#: (task, kind, schema path, the root classes the message validates into)
_TaskMessage = tuple[str, str, str, tuple[type[BaseModel], ...]]

_VERSION_ENVELOPE_REF = "https://adcontextprotocol.org/schemas/3.2.4/core/version-envelope.json"


def _module_name(path: Path) -> str:
    return "adcp.types.domains." + ".".join(path.relative_to(OUTPUT_DIR).with_suffix("").parts)


def _task_message_classes() -> list[_TaskMessage]:
    """(task, kind, schema path, root classes) for all 77 registered tasks, both directions.

    Resolved exactly the way the marker-insertion pass resolves it -- same registry, same
    provenance header, same root-class rule -- so this grades the generated tree rather
    than re-deriving a second answer that could agree with the pass while both are wrong.
    """
    rows: list[_TaskMessage] = []
    for task, kind, schema_rel, module, names in task_message_roots(SCHEMA_DIR, OUTPUT_DIR):
        imported = importlib.import_module(_module_name(module))
        rows.append((task, kind, schema_rel.as_posix(), tuple(getattr(imported, n) for n in names)))
    return rows


@pytest.fixture(scope="module")
def task_messages() -> list[_TaskMessage]:
    return _task_message_classes()


def _residue(rows: list[_TaskMessage], kind: str, ancestor: type[BaseModel]) -> frozenset[str]:
    """The schemas whose root classes do not ALL descend from ``ancestor``.

    All arms, not any arm: a union whose submitted arm lacks the ancestry cannot answer
    the envelope question for every value the task can return, so counting it as composed
    would overstate what a caller may rely on by 2 schemas.
    """
    return frozenset(
        schema
        for _, row_kind, schema, classes in rows
        if row_kind == kind and not all(issubclass(c, ancestor) for c in classes)
    )


def test_registry_covers_every_task_in_both_directions(task_messages: list[_TaskMessage]) -> None:
    """77 tasks, each with one request and one response, all resolving to real classes."""
    assert len(task_messages) == 154, "the pinned bundle registers 77 tasks"
    assert sum(1 for _, kind, _, _ in task_messages if kind == "request") == 77
    assert all(classes for *_, classes in task_messages)


def test_every_task_request_is_an_adcp_request(task_messages: list[_TaskMessage]) -> None:
    missing = sorted(
        schema
        for _, kind, schema, classes in task_messages
        if kind == "request" and not all(issubclass(c, AdcpRequest) for c in classes)
    )
    assert missing == [], "task requests not marked AdcpRequest: " + ", ".join(missing)


def test_every_task_response_is_an_adcp_response(task_messages: list[_TaskMessage]) -> None:
    missing = sorted(
        schema
        for _, kind, schema, classes in task_messages
        if kind == "response" and not all(issubclass(c, AdcpResponse) for c in classes)
    )
    assert missing == [], "task responses not marked AdcpResponse: " + ", ".join(missing)


def test_a_request_is_never_also_a_response(task_messages: list[_TaskMessage]) -> None:
    """The two markers partition the task messages; nothing carries both."""
    confused = sorted(
        {c.__name__ for *_, classes in task_messages for c in classes}
        & {
            c.__name__
            for _, kind, _, classes in task_messages
            for c in classes
            if issubclass(c, AdcpRequest) and issubclass(c, AdcpResponse)
        }
    )
    assert confused == []


def test_markers_are_plain_classes_with_no_fields() -> None:
    """A field on the marker would invent a spec field on the one tool that declares none.

    ``ValidateInputRequest`` declares no version fields; an ``adcp_version`` inherited from
    the marker would put one on its wire type. So the marker is not a model: it carries
    accessors that read the instance dict, and answers ``None`` for a tool that declares
    nothing.
    """
    for marker in (AdcpRequest, AdcpResponse):
        assert not issubclass(marker, BaseModel), f"{marker.__name__} must not be a model"
        assert "model_fields" not in vars(marker)
        assert not hasattr(marker, "__pydantic_fields__")


def test_task_request_version_envelope_residue_is_pinned(task_messages: list[_TaskMessage]) -> None:
    """76 of 77 task requests descend from AdcpVersionEnvelope; the residue is one schema."""
    residue = _residue(task_messages, "request", AdcpVersionEnvelope)
    assert residue == _REQUEST_VERSION_RESIDUE, (
        "version-envelope residue moved. Newly missing: "
        f"{sorted(residue - _REQUEST_VERSION_RESIDUE)}; now composing (remove the pin): "
        f"{sorted(_REQUEST_VERSION_RESIDUE - residue)}"
    )
    assert len(residue) == 1


def test_task_response_version_envelope_residue_is_pinned(
    task_messages: list[_TaskMessage],
) -> None:
    residue = _residue(task_messages, "response", AdcpVersionEnvelope)
    assert residue == _RESPONSE_VERSION_RESIDUE, (
        "version-envelope residue moved. Newly missing: "
        f"{sorted(residue - _RESPONSE_VERSION_RESIDUE)}; now composing (remove the pin): "
        f"{sorted(_RESPONSE_VERSION_RESIDUE - residue)}"
    )


def test_task_response_protocol_envelope_residue_is_pinned(
    task_messages: list[_TaskMessage],
) -> None:
    residue = _residue(task_messages, "response", ProtocolEnvelope)
    assert residue == _RESPONSE_PROTOCOL_RESIDUE, (
        "protocol-envelope residue moved. Newly missing: "
        f"{sorted(residue - _RESPONSE_PROTOCOL_RESIDUE)}; now composing (remove the pin): "
        f"{sorted(_RESPONSE_PROTOCOL_RESIDUE - residue)}"
    )


def test_task_requests_do_not_compose_the_protocol_envelope(
    task_messages: list[_TaskMessage],
) -> None:
    """The protocol envelope wraps responses. A request carrying it would be a leak."""
    leaked = sorted(
        schema
        for _, kind, schema, classes in task_messages
        if kind == "request" and any(issubclass(c, ProtocolEnvelope) for c in classes)
    )
    assert leaked == []


def test_field_classifiers_partition_the_declared_fields(task_messages: list[_TaskMessage]) -> None:
    """version / protocol / payload are disjoint and together are exactly ``model_fields``.

    Which is what makes "split the envelope from the payload" one expression instead of a
    hand list of envelope field names per transport edge -- the protocol envelope grew
    ``governance_context`` and ``replayed`` inside one minor, and every hand list is a list
    someone forgot to extend.
    """
    for task, kind, schema, classes in task_messages:
        for cls in classes:
            # A task message is a pydantic model AND a marker. Python has no intersection
            # type, so the cast states the half this block needs -- the same answer
            # BuyerRequest's own validator gives to the same problem.
            message = cast("type[_AdcpMessage]", cls)
            version = message.version_fields()
            protocol = message.protocol_fields()
            payload = message.payload_fields()
            where = f"{task} {kind} ({schema}) {cls.__name__}"
            assert not version & payload, where
            assert not protocol & payload, where
            assert version | protocol | payload == frozenset(cls.model_fields), where


def test_a_class_that_inlines_the_version_fields_reports_no_version_stratum() -> None:
    """The residue degrades honestly: empty classifier, working accessor.

    ``validate-input`` declares no version fields, so there is nothing to classify. The
    point of the pair is the general case: a schema that INLINES the fields without
    composing shares the names but not the stratum, and ``version_fields()`` must not
    report a composition that is not there -- while ``get_adcp_version()`` still answers,
    because it reads the instance dict rather than the MRO.
    """
    from adcp.types.domains.creative.validate_input_request import ValidateInputRequest

    assert issubclass(ValidateInputRequest, AdcpRequest)
    assert not issubclass(ValidateInputRequest, AdcpVersionEnvelope)
    assert ValidateInputRequest.version_fields() == frozenset()
    assert ValidateInputRequest.payload_fields() == frozenset(ValidateInputRequest.model_fields)


def test_request_accessors_answer_for_a_tool_that_declares_the_fields() -> None:
    from adcp.types.domains.media_buy.buy_products_request import BuyProductsRequest

    request = BuyProductsRequest.model_validate(
        {
            "adcp_version": "3.2",
            "adcp_major_version": 3,
            "account": {"account_id": "acct-1"},
            "brand": {"domain": "brand.example"},
            "idempotency_key": "0123456789abcdef",
            "feed_version": "feed-1",
            "purchases": [{"product_id": "p1", "pricing_option_id": "po1"}],
            "start_time": "asap",
            "end_time": "2026-12-01T00:00:00Z",
            "context": {"trace": "t-1"},
        }
    )
    assert request.get_adcp_version() == "3.2"
    assert request.get_adcp_major_version() == 3
    assert request.get_idempotency_key() == "0123456789abcdef"
    assert request.get_account() is not None
    assert request.get_context() is not None
    assert request.get_push_notification_config() is None


def test_request_accessors_answer_none_for_fields_a_tool_does_not_declare() -> None:
    """The whole reason the accessors are on a field-less marker rather than a model.

    ``validate-input`` declares an ``account`` and no version pins, no idempotency key and
    no webhook config. A marker carrying fields would have given it all four; the accessors
    answer from what the schema actually declared.
    """
    from adcp.types.domains.creative.validate_input_request import ValidateInputRequest

    # The manifest carries a ``format_kind``: ``core/creative-manifest.json`` declares a
    # root required group of ``format_id | format_kind``, which
    # ``enforce_root_required_groups`` now enforces on the generated class. The accessors
    # under test are unaffected by which arm satisfies it.
    request = ValidateInputRequest.model_validate(
        {
            "account": {"account_id": "acct-1"},
            "manifest": {"assets": {}, "format_kind": "image"},
        }
    )
    assert request.get_account() is not None
    assert request.get_adcp_version() is None
    assert request.get_adcp_major_version() is None
    assert request.get_idempotency_key() is None
    assert request.get_push_notification_config() is None
    assert request.get_context() is None


def test_response_accessors_read_the_protocol_stratum() -> None:
    from adcp.types.domains.media_buy.create_media_buy_response import (
        CreateMediaBuyResponse3,
    )

    submitted = CreateMediaBuyResponse3.model_validate(
        {"status": "submitted", "task_id": "task-1", "message": "queued"}
    )
    assert submitted.get_status() == "submitted"
    assert submitted.get_task_id() == "task-1"
    assert submitted.get_message() == "queued"
    assert submitted.get_adcp_error() is None
    assert submitted.get_replayed() is False


def _normalize(schema: dict[str, Any], rel: str) -> dict[str, Any]:
    return normalize_version_envelope_composition(json.loads(json.dumps(schema)), Path(rel))


def test_version_normalization_rewrites_a_pointer_ref_declaration() -> None:
    """Predicate A: the authored pointer form gains a real root composition."""
    schema = {
        "type": "object",
        "properties": {
            "adcp_version": {"$ref": f"{_VERSION_ENVELOPE_REF}#/properties/adcp_version"},
            "brief": {"type": "string"},
        },
    }
    result = _normalize(schema, "media-buy/example-request.json")
    assert result["allOf"] == [{"$ref": _VERSION_ENVELOPE_REF}]
    assert "adcp_version" not in result["properties"]
    assert result["properties"]["brief"] == {"type": "string"}


def test_version_normalization_is_a_no_op_once_upstream_composes() -> None:
    """adcp#7892 landing must make this pass inert, in either form it can land in.

    A clean root ``allOf`` matches neither predicate, so the schema is returned untouched
    rather than gaining a second ``allOf`` arm. The stub-retaining form -- which draft-07
    may force on #7892 -- is also untouched, and that shape is not hypothetical: it ships
    today in ``media-buy/sync-reporting-receipts-request.json``, which already renders as
    ``SyncReportingReceiptsRequest(AdcpVersionEnvelope)``.
    """
    composed = {
        "type": "object",
        "allOf": [{"$ref": _VERSION_ENVELOPE_REF}],
        "properties": {"brief": {"type": "string"}},
    }
    assert _normalize(composed, "media-buy/example-request.json") == composed

    stub_retaining = {
        "type": "object",
        "allOf": [{"$ref": _VERSION_ENVELOPE_REF}],
        "properties": {
            "adcp_version": {"$ref": f"{_VERSION_ENVELOPE_REF}#/properties/adcp_version"},
            "brief": {"type": "string"},
        },
    }
    assert _normalize(stub_retaining, "media-buy/example-request.json") == stub_retaining

    from adcp.types.domains.media_buy.sync_reporting_receipts_request import (
        SyncReportingReceiptsRequest,
    )

    assert issubclass(SyncReportingReceiptsRequest, AdcpVersionEnvelope)


def test_version_normalization_is_idempotent() -> None:
    """Applying the pass to its own output changes nothing."""
    schema = {
        "type": "object",
        "properties": {
            "adcp_version": {"$ref": f"{_VERSION_ENVELOPE_REF}#/properties/adcp_version"}
        },
    }
    once = _normalize(schema, "media-buy/example-request.json")
    assert _normalize(once, "media-buy/example-request.json") == once


def test_version_normalization_refuses_a_schema_whose_arm_is_a_bare_ref() -> None:
    """Relocating the stubs must never DELETE the field from a union arm.

    datamodel-code-generator merges a root ``properties`` into every arm it builds, but a
    root ``allOf`` ``$ref`` becomes a base only of the arm built FROM the root object. An
    arm that is a bare ``$ref`` elsewhere is generated from the referenced schema, so the
    composition never reaches it and dropping the stub strips the field outright.

    Measured when the rewrite was applied anyway: ``RefineProposalsResponse2`` went from 10
    fields to 9 and ``RequestProposalsResponse4`` from 16 to 15, both losing
    ``adcp_version`` -- and both carry ``extra: forbid``, so a seller echoing the version on
    the submitted arm would have been REJECTED where it previously validated. The two
    schemas are refused instead, and the all-arms ancestry is 70/77 either way because that
    submitted arm gains no ancestry from the rewrite.
    """
    with_ref_arm = {
        "type": "object",
        "anyOf": [
            {"required": ["results"]},
            {
                "$ref": "https://adcontextprotocol.org/schemas/3.2.4/core/compact-task-submitted.json"
            },
        ],
        "properties": {
            "adcp_version": {"$ref": f"{_VERSION_ENVELOPE_REF}#/properties/adcp_version"}
        },
    }
    assert _normalize(with_ref_arm, "media-buy/example-response.json") == with_ref_arm

    # The same schema with inline arms IS normalized: nothing is generated from elsewhere,
    # so every arm inherits the composition.
    inline_arms = {
        "type": "object",
        "anyOf": [{"required": ["results"]}, {"required": ["task_id"]}],
        "properties": {
            "adcp_version": {"$ref": f"{_VERSION_ENVELOPE_REF}#/properties/adcp_version"}
        },
    }
    assert _normalize(inline_arms, "media-buy/example-response.json") != inline_arms


def test_the_two_pointer_ref_responses_keep_the_field_on_every_arm() -> None:
    """The live consequence of the refusal above, on the generated tree.

    Both arms of each proposals response still declare ``adcp_version``. If the refusal is
    removed, the submitted arm loses it and this goes red.
    """
    from adcp.types.domains.media_buy.refine_proposals_response import (
        RefineProposalsResponse1,
        RefineProposalsResponse2,
    )
    from adcp.types.domains.media_buy.request_proposals_response import (
        RequestProposalsResponse4,
    )

    for arm in (RefineProposalsResponse1, RefineProposalsResponse2, RequestProposalsResponse4):
        assert "adcp_version" in arm.model_fields, arm.__name__


def test_version_normalization_refuses_a_declaration_it_cannot_prove_equivalent() -> None:
    """A third convention is left alone, so it surfaces as residue with its name attached.

    ``manifest.schema.json`` declares ``adcp_version`` as a full semver, and requires it;
    ``core/verification-token-claims.json`` declares its own shape. Lifting either into the
    envelope would change what validates.
    """
    foreign = {
        "type": "object",
        "required": ["adcp_version"],
        "properties": {"adcp_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"}},
    }
    assert _normalize(foreign, "manifest.schema.json") == foreign

    equivalent_but_required = {
        "type": "object",
        "required": ["adcp_version"],
        "properties": {
            "adcp_version": {"$ref": f"{_VERSION_ENVELOPE_REF}#/properties/adcp_version"}
        },
    }
    assert (
        _normalize(equivalent_but_required, "media-buy/example-request.json")
        == equivalent_but_required
    )


def test_descent_refuses_a_forged_model_that_merely_declares_the_fields() -> None:
    """The forgeability argument the markers exist to keep true (ARCH-HIERARCHY §1).

    A field-presence test accepts any hand-written model that declares ``adcp_version`` and
    ``adcp_major_version``. That is not hypothetical: the Prebid Sales Agent once carried
    ``CompleteTaskRequest`` and ``CompleteTaskRequestLocal``, neither a subclass of the
    other, both parallel to the SDK -- and its registration refusal
    (``src/core/main.py:286``) tests descent precisely because a field test would have
    passed them both.

    Both markers must refuse that model. They do, and this test is what keeps them
    refusing: a marker that grew the two fields would make the forgery pass.
    """

    class ForgedByFields(BaseModel):
        adcp_version: str | None = None
        adcp_major_version: int | None = None

    assert {"adcp_version", "adcp_major_version"} <= set(ForgedByFields.model_fields)
    assert not issubclass(ForgedByFields, AdcpRequest)
    assert not issubclass(ForgedByFields, AdcpVersionEnvelope)


def test_the_version_envelope_has_a_public_name() -> None:
    """The adoption gate's guard conjunction must be spellable without a deep import.

    ``ProtocolEnvelope`` was already public and ``AdcpVersionEnvelope`` was not, so a
    consumer writing
    ``issubclass(model, AdcpRequest) and issubclass(model, AdcpVersionEnvelope)``
    had to reach into ``adcp.types.domains.core.version_envelope`` — which is
    exactly the import the one-surface work exists to retire, and which the Prebid Sales
    Agent carries today at ``src/core/main.py:230``. Re-export, never a copy: the public
    name must be the SAME object, or the conjunction would refuse every real request.
    """
    import adcp.types
    import adcp.types.protocol

    assert "AdcpVersionEnvelope" in adcp.types.__all__
    assert "AdcpVersionEnvelope" in adcp.types.protocol.__all__
    assert adcp.types.AdcpVersionEnvelope is AdcpVersionEnvelope
    assert adcp.types.protocol.AdcpVersionEnvelope is AdcpVersionEnvelope
    # Its already-public sibling, as the parity this mirrors.
    assert "ProtocolEnvelope" in adcp.types.__all__

    # The conjunction, written in the public spelling only, on a real task request.
    from adcp.types import GetProductsRequest
    from adcp.types.base import AdcpRequest as PublicAdcpRequest

    assert issubclass(GetProductsRequest, PublicAdcpRequest)
    assert issubclass(GetProductsRequest, adcp.types.AdcpVersionEnvelope)


def test_the_two_descent_predicates_refuse_different_forgeries() -> None:
    """Why a consumer's guard wants BOTH, not one in place of the other.

    ``AdcpRequest`` is field-less on purpose, so inheriting it supplies nothing and a forger
    can inherit it and invent fields -- it answers "someone put this class in the request
    position", not "this class carries the spec's version contract".
    ``AdcpVersionEnvelope`` answers the second and not the first: inheriting it is what
    supplies the two fields, and a model that does so is still not any task's request.

    Measured, so the asymmetry is recorded rather than assumed. Until the generated task
    table (ARCH-HIERARCHY §7.6) exists, the conjunction is the strongest available form of
    "is a registered task message", and a consumer swapping one predicate for the other
    loses a refusal.
    """

    class ForgedByMarker(AdcpRequest, BaseModel):
        adcp_version: str | None = None
        whatever: str | None = None

    class ForgedByEnvelope(AdcpVersionEnvelope):
        whatever: str | None = None

    assert issubclass(ForgedByMarker, AdcpRequest)
    assert not issubclass(ForgedByMarker, AdcpVersionEnvelope)

    assert issubclass(ForgedByEnvelope, AdcpVersionEnvelope)
    assert not issubclass(ForgedByEnvelope, AdcpRequest)

    # The conjunction refuses both; neither predicate alone does.
    for forged in (ForgedByMarker, ForgedByEnvelope):
        assert not (issubclass(forged, AdcpRequest) and issubclass(forged, AdcpVersionEnvelope))
