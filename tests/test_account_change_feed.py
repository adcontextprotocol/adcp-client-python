"""Account change feed records (AdCP 3.3, #1647).

``changed_paths`` is required on ``adcp`` / ``seller_operator`` records, the
originating request's ``idempotency_key`` reaches only its sender, and a
creative's ``seller_revision`` passes through and mirrors into
``resource_revision``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from adcp._principal_ref import PrincipalRef
from adcp.account_change_feed import (
    ROOT_POINTER,
    AccountChangeRecordError,
    AttributedAccountChange,
    account_change_recorded_payload,
    attribute_account_change,
    json_pointer,
    mirror_creative_revision,
    principal_ref_for,
    project_account_change,
    project_list_account_changes_response,
    validate_account_change,
)
from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import _requires_changed_paths, create_tool_caller
from adcp.types import ListAccountChangesResponse, ListCreativesResponse
from adcp.types.domains.core.account_change import AccountChange
from adcp.validation.client_hooks import ValidationHookConfig
from adcp.validation.schema_validator import validate_response
from adcp.webhook_sender import WebhookSender

AT = "2026-10-01T12:00:00Z"
REQUEST_KEY = "req_0123456789abcdef"
URL = "https://buyer.example/webhooks"
STRICT = ValidationHookConfig(requests="strict", responses="strict")


def record(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "change_id": "chg_1",
        "recorded_at": AT,
        "resource": {"type": "creative", "account_id": "acct_1", "resource_id": "cr_1"},
        "action": "updated",
        "origin": {"kind": "adcp"},
        "idempotency_key": REQUEST_KEY,
        "changed_paths": ["/name"],
        "repair": {"task": "list_creatives"},
    }
    value.update(overrides)
    return {key: item for key, item in value.items() if item is not None}


def page(changes: list[Any]) -> dict[str, Any]:
    return {
        "status": "completed",
        "changes": changes,
        "cursor": "cur_2",
        "has_more": False,
        "available_since": AT,
        "generated_at": AT,
    }


# --------------------------------------------------------------------------
# Record rules
# --------------------------------------------------------------------------


def test_json_pointer_escapes_tokens_and_defaults_to_root() -> None:
    assert json_pointer() == ROOT_POINTER == ""
    assert json_pointer("packages", 0, "budget") == "/packages/0/budget"
    assert json_pointer("a/b", "m~n") == "/a~1b/m~0n"


@pytest.mark.parametrize("kind", ["adcp", "seller_operator"])
@pytest.mark.parametrize("paths", [None, []])
def test_paths_required_for_processed_origins(kind: str, paths: list[str] | None) -> None:
    change = record(origin={"kind": kind}, idempotency_key=None, changed_paths=paths)
    if paths is not None:
        change["changed_paths"] = paths
    with pytest.raises(AccountChangeRecordError, match="changed_paths") as exc:
        validate_account_change(change, pointer="/changes/2")
    assert exc.value.pointer == "/changes/2/changed_paths"
    with pytest.raises(AccountChangeRecordError):
        attribute_account_change(change, None)


@pytest.mark.parametrize("kind", ["seller_system", "connected_platform", "unknown"])
def test_paths_best_effort_for_observed_origins(kind: str) -> None:
    validate_account_change(record(origin={"kind": kind}, idempotency_key=None, changed_paths=None))


def test_root_pointer_satisfies_whole_resource_changes() -> None:
    change = record(action="created", changed_paths=[ROOT_POINTER])
    validate_account_change(change)
    assert attribute_account_change(change, "p1").changed_paths == [""]


def test_idempotency_key_only_on_adcp_records() -> None:
    with pytest.raises(AccountChangeRecordError, match="idempotency_key") as exc:
        validate_account_change(record(origin={"kind": "seller_operator"}))
    assert exc.value.pointer == "/idempotency_key"


def test_generated_schema_agrees_with_the_record_rules() -> None:
    """The bundled 3.3 schema encodes the same rules the SDK enforces."""
    for change, valid in [
        (record(), True),
        (record(changed_paths=None), False),
        (record(origin={"kind": "seller_system"}, changed_paths=None, idempotency_key=None), True),
        (record(origin={"kind": "seller_system"}, changed_paths=None), False),
    ]:
        assert validate_response("list_account_changes", page([change])).valid is valid


# --------------------------------------------------------------------------
# Attribution and per-reader projection
# --------------------------------------------------------------------------


def test_principal_ref_for_accepts_context_ref_and_id() -> None:
    ctx = ToolContext(caller_identity="p1", tenant_id="t1")
    assert principal_ref_for(ctx) == PrincipalRef("t1", "p1")
    assert principal_ref_for(ToolContext()) is None
    assert principal_ref_for("p1", tenant_id="t1") == PrincipalRef("t1", "p1")
    assert principal_ref_for(PrincipalRef(None, "p1")) == PrincipalRef(None, "p1")
    assert principal_ref_for(None) is None


def test_attribution_is_off_the_wire() -> None:
    attributed = attribute_account_change(AccountChange.model_validate(record()), "p1")
    assert isinstance(attributed, AttributedAccountChange)
    assert attributed.originating_principal == PrincipalRef(None, "p1")
    dumped = attributed.model_dump(mode="json", exclude_none=True)
    assert "originating_principal" not in json.dumps(dumped)
    assert dumped == AccountChange.model_validate(record()).model_dump(
        mode="json", exclude_none=True
    )


@pytest.mark.parametrize(
    ("originator", "reader", "echoed"),
    [
        ("p1", PrincipalRef(None, "p1"), True),
        ("p1", PrincipalRef(None, "p2"), False),
        ("p1", None, False),
        (None, PrincipalRef(None, "p1"), False),
        (PrincipalRef("t1", "p1"), PrincipalRef("t2", "p1"), False),
        (PrincipalRef("t1", "p1"), ToolContext(caller_identity="p1", tenant_id="t1"), True),
        (ToolContext(caller_identity="p1"), ToolContext(caller_identity="p1"), True),
    ],
)
def test_key_echoed_only_to_the_originator(originator: Any, reader: Any, echoed: bool) -> None:
    projected = project_account_change(
        attribute_account_change(record(), originator), reader=reader
    )
    assert ("idempotency_key" in projected) is echoed
    assert projected["changed_paths"] == ["/name"]


@pytest.mark.parametrize("change", [record(), AccountChange.model_validate(record())])
def test_unattributed_records_fail_closed(change: Any) -> None:
    projected = project_account_change(change, reader=PrincipalRef(None, "p1"))
    assert "idempotency_key" not in projected
    assert change is not projected


def test_response_projection_keeps_envelope_and_refuses_bad_pages() -> None:
    model = ListAccountChangesResponse.model_validate(
        {**page([attribute_account_change(record(), "p1")]), "context": {"trace": "x"}}
    )
    projected = project_list_account_changes_response(model, reader="p1")
    assert projected["context"] == {"trace": "x"}
    assert projected["changes"][0]["idempotency_key"] == REQUEST_KEY

    bad = page([record(change_id="chg_0"), record(changed_paths=None)])
    with pytest.raises(AccountChangeRecordError) as exc:
        project_list_account_changes_response(bad, reader="p1")
    assert exc.value.pointer == "/changes/1/changed_paths"
    loose = project_list_account_changes_response(bad, reader="p1", require_changed_paths=False)
    assert all("idempotency_key" not in change for change in loose["changes"])

    error_envelope = {"adcp_error": {"code": "ACCOUNT_NOT_FOUND"}}
    assert project_list_account_changes_response(error_envelope, reader="p1") is error_envelope


# --------------------------------------------------------------------------
# Server dispatch
# --------------------------------------------------------------------------


class FeedSeller(ADCPHandler):
    def __init__(self, changes: list[Any], *, as_model: bool = True) -> None:
        self.changes = changes
        self.as_model = as_model

    async def list_account_changes(self, params: Any, context: Any = None) -> Any:
        if self.as_model:
            return ListAccountChangesResponse.model_validate(page(self.changes))
        return page(self.changes)


REQUEST = {"account": {"account_id": "acct_1"}}


def make_caller(handler: ADCPHandler, tool: str, **kwargs: Any) -> Any:
    # Without a version envelope, serve the current release (as A2A does).
    return create_tool_caller(handler, tool, default_unnegotiated_adcp_version=None, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("as_model", [True, False])
async def test_dispatch_echoes_key_only_to_the_originating_caller(as_model: bool) -> None:
    mine = attribute_account_change(record(change_id="chg_mine"), "buyer_a")
    theirs = attribute_account_change(record(change_id="chg_theirs"), "buyer_b")
    plain = record(change_id="chg_plain")
    # A key on a non-adcp record is dropped, not refused.
    operator = record(change_id="chg_op", origin={"kind": "seller_operator"})
    caller = make_caller(
        FeedSeller([mine, theirs, plain, operator], as_model=as_model),
        "list_account_changes",
        validation=STRICT,
    )

    def keys(response: dict[str, Any]) -> dict[str, str | None]:
        return {c["change_id"]: c.get("idempotency_key") for c in response["changes"]}

    as_a = await caller(dict(REQUEST), ToolContext(caller_identity="buyer_a"))
    assert keys(as_a) == {
        "chg_mine": REQUEST_KEY,
        "chg_theirs": None,
        "chg_plain": None,
        "chg_op": None,
    }
    as_b = await caller(dict(REQUEST), ToolContext(caller_identity="buyer_b"))
    assert keys(as_b)["chg_theirs"] == REQUEST_KEY
    assert keys(as_b)["chg_mine"] is None
    anonymous = await caller(dict(REQUEST), ToolContext())
    assert set(keys(anonymous).values()) == {None}
    other_tenant = await caller(
        dict(REQUEST), ToolContext(caller_identity="buyer_a", tenant_id="t9")
    )
    assert set(keys(other_tenant).values()) == {None}


@pytest.mark.asyncio
@pytest.mark.parametrize("validation", [STRICT, None])
async def test_dispatch_refuses_record_without_changed_paths(
    validation: ValidationHookConfig | None,
) -> None:
    bad = record(origin={"kind": "seller_operator"}, idempotency_key=None, changed_paths=None)
    caller = make_caller(
        FeedSeller([record(), bad], as_model=False), "list_account_changes", validation=validation
    )
    with pytest.raises(ADCPTaskError) as exc:
        await caller(dict(REQUEST), ToolContext(caller_identity="buyer_a"))
    error = exc.value.errors[0]
    assert error.code == "VALIDATION_ERROR"
    assert error.field == "/changes/1/changed_paths"
    assert "changed_paths" in error.message


def test_changed_paths_enforced_from_3_3() -> None:
    assert _requires_changed_paths("3.3-beta.3")
    assert _requires_changed_paths("3.3")
    assert _requires_changed_paths(None)
    assert not _requires_changed_paths("3.2")


@pytest.mark.asyncio
async def test_dispatch_on_a_3_2_handler_keeps_paths_optional_but_strips_keys() -> None:
    class Seller32(FeedSeller):
        _adcp_version = "3.2"

    plain = record(changed_paths=None)
    caller = make_caller(
        Seller32([plain], as_model=False), "list_account_changes", supported_versions=["3.2"]
    )
    response = await caller({**REQUEST, "adcp_version": "3.2"}, ToolContext(caller_identity="x"))
    assert "changed_paths" not in response["changes"][0]
    assert "idempotency_key" not in response["changes"][0]


# --------------------------------------------------------------------------
# account.change_recorded notifications
# --------------------------------------------------------------------------


def test_notification_payload_never_carries_the_request_key() -> None:
    change = attribute_account_change(
        record(resource={**record()["resource"], "parent_ids": {"media_buy_id": "mb_1"}}), "p1"
    )
    payload = account_change_recorded_payload(change, subscriber_id="sub_1", through_cursor="c9")
    assert payload == {
        "notification_type": "account.change_recorded",
        "notification_id": "chg_1",
        "subscriber_id": "sub_1",
        "account_id": "acct_1",
        "change_id": "chg_1",
        "recorded_at": AT,
        "resource": {
            "type": "creative",
            "resource_id": "cr_1",
            "parent_ids": {"media_buy_id": "mb_1"},
        },
        "action": "updated",
        "through_cursor": "c9",
    }


@pytest.mark.parametrize("change", [record(), AccountChange.model_validate(record())])
def test_prepare_from_a_record_mints_a_fresh_delivery_key(change: Any) -> None:
    prepared = WebhookSender.prepare_account_change_recorded(
        url=URL, payload=change, subscriber_id="sub_1"
    )
    body = json.loads(prepared.body)
    assert REQUEST_KEY not in prepared.body.decode()
    assert body["idempotency_key"] == prepared.idempotency_key != REQUEST_KEY
    assert body["notification_id"] == body["change_id"] == "chg_1"
    assert "changed_paths" not in body and "origin" not in body

    explicit = WebhookSender.prepare_account_change_recorded(
        url=URL, payload=change, subscriber_id="sub_1", idempotency_key="delivery_0123456789"
    )
    assert explicit.idempotency_key == "delivery_0123456789"


def test_prepare_refuses_non_conformant_records_and_key_reuse() -> None:
    with pytest.raises(AccountChangeRecordError, match="changed_paths"):
        WebhookSender.prepare_account_change_recorded(
            url=URL, payload=record(changed_paths=[]), subscriber_id="sub_1"
        )
    with pytest.raises(ValueError, match="must not reuse"):
        WebhookSender.prepare_account_change_recorded(
            url=URL, payload=record(), subscriber_id="sub_1", idempotency_key=REQUEST_KEY
        )
    with pytest.raises(ValueError, match="subscriber_id"):
        WebhookSender.prepare_account_change_recorded(url=URL, payload=record())


def test_prepare_from_notification_fields_is_unchanged() -> None:
    fields = account_change_recorded_payload(record(), subscriber_id="sub_1")
    prepared = WebhookSender.prepare_account_change_recorded(
        url=URL, payload=fields, idempotency_key="delivery_0123456789"
    )
    assert json.loads(prepared.body)["idempotency_key"] == "delivery_0123456789"
    with pytest.raises(ValueError, match="only when payload is a change record"):
        WebhookSender.prepare_account_change_recorded(url=URL, payload=fields, subscriber_id="s")


# --------------------------------------------------------------------------
# Creative seller_revision
# --------------------------------------------------------------------------


def creatives_page(seller_revision: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "query_summary": {"total_matching": 1, "returned": 1},
        "pagination": {"has_more": False},
        "creatives": [
            {
                "creative_id": "cr_1",
                "seller_revision": seller_revision,
                "name": "Spring",
                "format_kind": "image",
                "status": "approved",
                "created_date": AT,
                "updated_date": AT,
            }
        ],
    }


@pytest.mark.parametrize("revision", [7, "rev-7"])
def test_seller_revision_round_trips_the_model(revision: Any) -> None:
    model = ListCreativesResponse.model_validate(creatives_page(revision))
    dumped = model.model_dump(mode="json", exclude_none=True)
    assert dumped["creatives"][0]["seller_revision"] == revision


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "kept"), [(None, True), ("3.2", False)])
async def test_list_creatives_serves_seller_revision_to_3_3_only(
    version: str | None, kept: bool
) -> None:
    class CreativeSeller(ADCPHandler):
        async def list_creatives(self, params: Any, context: Any = None) -> Any:
            return ListCreativesResponse.model_validate(creatives_page(7))

    caller = make_caller(CreativeSeller(), "list_creatives", validation=STRICT)
    wire: dict[str, Any] = {} if version is None else {"adcp_version": version}
    response = await caller(wire, ToolContext(caller_identity="buyer_a"))
    assert response["creatives"][0].get("seller_revision") == (7 if kept else None)


@pytest.mark.parametrize("revision", [7, "rev-7"])
def test_mirror_creative_revision_keeps_json_type(revision: Any) -> None:
    creative = ListCreativesResponse.model_validate(creatives_page(revision)).creatives[0]
    attributed = attribute_account_change(record(), "p1")
    mirrored = mirror_creative_revision(attributed, creative)
    assert isinstance(mirrored, AttributedAccountChange)
    assert mirrored.originating_principal == PrincipalRef(None, "p1")
    assert mirrored.model_dump(mode="json")["resource_revision"] == revision
    assert attributed.resource_revision is None

    as_dict = mirror_creative_revision(record(), {"seller_revision": revision})
    assert as_dict["resource_revision"] == revision
    assert type(as_dict["resource_revision"]) is type(revision)


def test_mirror_creative_revision_clears_and_validates() -> None:
    assert "resource_revision" not in mirror_creative_revision(record(resource_revision=3), {})
    cleared = mirror_creative_revision(
        AccountChange.model_validate(record(resource_revision=3)), {}
    )
    assert "resource_revision" not in cleared.model_dump(mode="json", exclude_none=True)
    with pytest.raises(AccountChangeRecordError, match="not creative"):
        mirror_creative_revision(
            record(resource={"type": "media_buy", "account_id": "a", "resource_id": "m"}),
            {"seller_revision": 1},
        )
    for bad in (0, True, "", "x" * 256, 1.5):
        with pytest.raises(AccountChangeRecordError):
            mirror_creative_revision(record(), {"seller_revision": bad})
