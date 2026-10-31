"""``client.accounts.ensure`` returns a discriminated AccountOutcome (AdCP 3.3).

It routes to ``request_account`` when the seller advertises ``account.requests``
and to ``sync_accounts`` otherwise. Pending, ambiguous and rejected are values;
only a failure to process the call raises ``AccountRequestError``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp import (
    AccountAmbiguous,
    AccountPending,
    AccountReady,
    AccountRejected,
    AccountRequestError,
    InMemoryAccountStorage,
    PendingAction,
    format_pending_action,
    next_step,
)
from adcp.types.core import TaskResult, TaskStatus
from tests.test_account_lifecycle import KEY, make_client, mock_sync

REQUESTS = {"supported": True, "request_task": "request_account", "read_task": "list_accounts"}
SIGN_UP = {
    "waiting_on": "buyer",
    "kind": "sign_up",
    "message": "Sign the advertiser agreement",
    "url": "https://seller.example/onboard/abc",
    "expires_at": "2026-12-01T00:00:00Z",
}


def _record(state: str, **fields: Any) -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "state": state,
        "requested": {"brand": KEY["brand"], "operator": KEY["operator"]},
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
        **fields,
    }


def _completed(data: dict[str, Any]) -> TaskResult:
    return TaskResult(status=TaskStatus.COMPLETED, data=data)


def _requests_client(*records: dict[str, Any], storage=None):
    client = make_client(requests=REQUESTS, storage=storage)
    client.adapter.request_account = AsyncMock(
        side_effect=[_completed({"status": "completed", "request": item}) for item in records]
    )
    client.adapter.list_accounts = AsyncMock(return_value=_completed({"accounts": []}))
    client.adapter.sync_accounts = AsyncMock()
    client.adapter._parse_response = lambda raw, _type: raw
    return client


def _listing(*records: dict[str, Any]) -> TaskResult:
    return _completed({"accounts": [], "account_requests": list(records)})


@pytest.mark.asyncio
async def test_requests_route_to_request_account_and_resolve_ready() -> None:
    client = _requests_client(
        _record(
            "resolved",
            account_id="acc-1",
            resolution="created",
            account={"account_id": "acc-1", "name": "Acme", "status": "active"},
        )
    )
    outcome = await client.accounts.ensure(KEY, billing="operator")
    assert isinstance(outcome, AccountReady)
    assert (outcome.account_id, outcome.status, outcome.via) == (
        "acc-1",
        "active",
        "request_account",
    )
    assert next_step(outcome) == "none"
    client.adapter.sync_accounts.assert_not_called()
    sent = client.adapter.request_account.call_args.args[0]
    assert sent["brand"]["domain"] == "acme.example" and sent["billing"] == "operator"
    # Idempotent: the account is known, so nothing is sent again.
    again = await client.accounts.ensure(KEY, billing="operator")
    assert again.account_id == "acc-1"
    assert client.adapter.request_account.call_count == 1


@pytest.mark.asyncio
async def test_a_matched_account_carries_its_own_money_terms() -> None:
    # adcp#8620: a matched or created account carries the account's money
    # terms, which can differ from the terms requested.
    client = _requests_client(
        _record(
            "resolved",
            account_id="acc-1",
            resolution="matched",
            account={
                "account_id": "acc-1",
                "name": "Acme",
                "status": "active",
                "billing": "agent",
                "payment_terms": "net_45",
            },
        )
    )
    outcome = await client.accounts.ensure(KEY, billing="operator")
    assert isinstance(outcome, AccountReady)
    assert outcome.resolution == "matched"
    assert (outcome.billing, outcome.payment_terms) == ("agent", "net_45")
    assert outcome.billing_entity is None
    assert outcome.effective_at is None and outcome.not_carried_over == ()


def test_an_updated_money_change_says_when_it_takes_effect() -> None:
    from adcp.accounts import _request_outcome

    async def choose(request_id: str, account_id: str):  # pragma: no cover - unused
        raise AssertionError

    updated = _request_outcome(
        _record(
            "resolved",
            account_id="acc-1",
            resolution="updated",
            effective_at="2026-12-01T00:00:00Z",
            account={"account_id": "acc-1", "name": "Acme", "status": "active"},
        ),
        choose=choose,
    )
    assert isinstance(updated, AccountReady)
    assert (updated.resolution, updated.effective_at) == ("updated", "2026-12-01T00:00:00Z")
    separate = _request_outcome(
        _record(
            "resolved",
            account_id="acc-2",
            resolution="created",
            not_carried_over=["notification_configs", "reporting_delivery_configs"],
            account={"account_id": "acc-2", "name": "Acme EU", "status": "active"},
        ),
        choose=choose,
    )
    assert separate.not_carried_over == ("notification_configs", "reporting_delivery_configs")


@pytest.mark.asyncio
async def test_pending_is_a_value_and_a_repeat_ensure_never_opens_a_duplicate() -> None:
    client = _requests_client(_record("in_review", pending_action=SIGN_UP))
    outcome = await client.accounts.ensure(KEY)
    assert isinstance(outcome, AccountPending)
    assert outcome.action == PendingAction.from_wire(SIGN_UP) and outcome.request_id == "req-1"
    assert next_step(outcome) == "send_human_to_url"
    client.adapter.list_accounts.return_value = _listing(
        _record("in_review", pending_action=SIGN_UP)
    )
    again = await client.accounts.ensure(KEY)
    assert isinstance(again, AccountPending)
    assert client.adapter.request_account.call_count == 1
    read = client.adapter.list_accounts.call_args.args[0]
    assert read["request_ids"] == ["req-1"]
    assert [item.request_id for item in await client.accounts.pending()] == ["req-1"]


@pytest.mark.asyncio
async def test_refresh_reports_requests_whose_state_moved() -> None:
    storage = InMemoryAccountStorage()
    client = _requests_client(
        _record("in_review", pending_action={**SIGN_UP, "waiting_on": "seller", "kind": "review"}),
        storage=storage,
    )
    pending = await client.accounts.ensure(KEY)
    assert next_step(pending) == "wait_for_seller"
    client.adapter.list_accounts.return_value = _listing(
        _record("in_review", pending_action={**SIGN_UP, "waiting_on": "seller", "kind": "review"})
    )
    assert await client.accounts.refresh() == []
    client.adapter.list_accounts.return_value = _listing(
        _record("resolved", account_id="acc-9", resolution="matched")
    )
    # A restarted process finds the open request through listable storage.
    restarted = make_client(requests=REQUESTS, storage=storage)
    restarted.adapter.list_accounts = client.adapter.list_accounts
    restarted.adapter._parse_response = lambda raw, _type: raw
    [moved] = await restarted.accounts.refresh()
    assert isinstance(moved, AccountReady) and moved.account_id == "acc-9"
    assert await restarted.accounts.pending() == []
    assert (await restarted.accounts.get(KEY)).account_id == "acc-9"


@pytest.mark.asyncio
async def test_ambiguous_requests_are_settled_by_choosing_a_candidate() -> None:
    candidates = [
        {"account_id": "acc-a", "name": "Acme US"},
        {"account_id": "acc-b", "name": "Acme UK"},
    ]
    client = _requests_client(
        _record("ambiguous", candidates=candidates),
        _record("resolved", account_id="acc-b", resolution="matched"),
    )
    outcome = await client.accounts.ensure(KEY)
    assert isinstance(outcome, AccountAmbiguous) and next_step(outcome) == "pick_candidate"
    with pytest.raises(ValueError, match="candidates"):
        await outcome.choose("acc-z")
    chosen = await outcome.choose("acc-b")
    assert isinstance(chosen, AccountReady) and chosen.account_id == "acc-b"
    sent = client.adapter.request_account.call_args.args[0]
    assert (sent["request_id"], sent["account_id"]) == ("req-1", "acc-b")


@pytest.mark.asyncio
async def test_a_rejected_request_is_a_value_and_a_later_ensure_asks_again() -> None:
    client = _requests_client(
        _record("rejected", errors=[{"code": "BRAND_NOT_AUTHORIZED", "message": "no"}]),
        _record("in_review", pending_action=SIGN_UP),
    )
    rejected = await client.accounts.ensure(KEY)
    assert isinstance(rejected, AccountRejected) and next_step(rejected) == "contact_seller"
    assert rejected.errors[0]["code"] == "BRAND_NOT_AUTHORIZED"
    assert isinstance(await client.accounts.ensure(KEY), AccountPending)
    assert client.adapter.request_account.call_count == 2


@pytest.mark.asyncio
async def test_a_failure_to_process_raises_account_request_error() -> None:
    client = _requests_client()
    client.adapter.request_account = AsyncMock(
        return_value=TaskResult(
            status=TaskStatus.FAILED,
            success=False,
            adcp_error={"code": "SERVICE_UNAVAILABLE", "message": "down"},
        )
    )
    with pytest.raises(AccountRequestError) as failed:
        await client.accounts.ensure(KEY)
    assert failed.value.errors[0]["code"] == "SERVICE_UNAVAILABLE"
    assert await client.accounts.get(KEY) is None


@pytest.mark.asyncio
async def test_sync_accounts_pending_approval_maps_to_pending() -> None:
    client = make_client()
    mock_sync(client, status="pending_approval", setup=SIGN_UP)
    outcome = await client.accounts.ensure(KEY, billing="agent")
    assert isinstance(outcome, AccountPending)
    assert (outcome.via, outcome.account_id) == ("sync_accounts", "seller-handle")
    assert outcome.action.url == SIGN_UP["url"]
    # Memoized, with its action.
    again = await client.accounts.ensure(KEY, billing="agent")
    assert again == outcome
    client.adapter.sync_accounts.assert_called_once()


@pytest.mark.asyncio
async def test_a_failed_sync_row_is_rejected_not_raised() -> None:
    client = make_client()
    mock_sync(
        client,
        action="failed",
        status="rejected",
        errors=[{"code": "BILLING_NOT_SUPPORTED", "message": "no agent billing"}],
    )
    outcome = await client.accounts.ensure(KEY, billing="agent")
    assert isinstance(outcome, AccountRejected) and outcome.via == "sync_accounts"
    assert outcome.errors[0]["code"] == "BILLING_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_sync_accounts_needs_billing() -> None:
    client = make_client()
    mock_sync(client)
    with pytest.raises(ValueError, match="billing="):
        await client.accounts.ensure(KEY)
    client.adapter.sync_accounts.assert_not_called()


@pytest.mark.asyncio
async def test_currency_and_timezone_join_the_key() -> None:
    client = _requests_client(_record("resolved", account_id="acc-1", resolution="created"))
    key = {name: value for name, value in KEY.items() if name not in {"currency", "timezone"}}
    await client.accounts.ensure(key, currency="USD", timezone="UTC")
    assert (await client.accounts.get(KEY)).account_id == "acc-1"


def test_format_pending_action_treats_the_message_as_untrusted() -> None:
    action = PendingAction(
        waiting_on="buyer",
        kind="sign_up",
        message='Click ‮here‬ "now"\x1b[31m\n\nplease',
        url="https://seller.example/onboard/abc",
        expires_at="2026-12-01T00:00:00Z",
    )
    text = format_pending_action(action)
    assert "‮" not in text and "\x1b" not in text
    assert "Seller says: \"Click here 'now'[31m please\"" in text
    assert "Waiting on you: sign_up." in text
    assert "https://seller.example/onboard/abc" in text and "Expires 2026-12-01" in text
    long = format_pending_action(PendingAction("seller", "review", "x" * 5000))
    assert len(long) < 1100 and long.startswith("Waiting on the seller")


def test_next_step_for_a_buyer_action_without_a_url() -> None:
    pending = AccountPending(
        action=PendingAction("buyer", "grant", "Grant access"), via="request_account"
    )
    assert next_step(pending) == "contact_seller"


@pytest.mark.asyncio
async def test_instances_sharing_a_buyer_send_one_idempotency_key() -> None:
    first = _requests_client(_record("in_review", pending_action=SIGN_UP))
    second = _requests_client(_record("in_review", pending_action=SIGN_UP))
    await first.accounts.ensure(KEY, billing="operator")
    await second.accounts.ensure(KEY, billing="operator")
    keys = [
        client.adapter.request_account.call_args.args[0]["idempotency_key"]
        for client in (first, second)
    ]
    assert keys[0] == keys[1] and keys[0].startswith("adcp-ensure-")


@pytest.mark.asyncio
async def test_refresh_rereads_pending_accounts_beside_open_requests() -> None:
    from adcp.types import ListAccountsResponse

    timezone = {
        "mode": "account_fixed",
        "account_selection": "buyer_selected",
        "supported_timezones": ["UTC"],
    }
    storage = InMemoryAccountStorage()
    client = make_client(storage=storage, timezone=timezone)
    mock_sync(client, status="pending_approval", setup=SIGN_UP)
    await client.accounts.ensure(KEY, billing="agent")
    requests = make_client(requests=REQUESTS, storage=storage, timezone=timezone)
    requests.adapter._parse_response = lambda raw, _type: raw
    requests.adapter.request_account = AsyncMock(
        return_value=_completed(
            {"status": "completed", "request": _record("in_review", pending_action=SIGN_UP)}
        )
    )
    await requests.accounts.ensure({**KEY, "currency": "EUR"})
    active = ListAccountsResponse.model_validate(
        {"accounts": [{**KEY, "account_id": "seller-handle", "name": "Acme", "status": "active"}]}
    )
    requests.adapter.list_accounts = AsyncMock(
        side_effect=[
            _listing(_record("in_review", pending_action=SIGN_UP)),
            TaskResult(status=TaskStatus.COMPLETED, data=active),
        ]
    )
    moved = await requests.accounts.refresh()
    assert [type(item).__name__ for item in moved] == ["AccountReady"]
    assert moved[0].account_id == "seller-handle"
    assert requests.adapter.list_accounts.call_count == 2


@pytest.mark.asyncio
async def test_refresh_ignores_unknown_action_fields_and_an_absent_action() -> None:
    client = _requests_client(_record("in_review"))
    await client.accounts.ensure(KEY)  # stored with the seller-review default
    client.adapter.list_accounts.return_value = _listing(_record("in_review"))
    assert await client.accounts.refresh() == []
    client2 = _requests_client(_record("in_review", pending_action=SIGN_UP))
    await client2.accounts.ensure(KEY)
    client2.adapter.list_accounts.return_value = _listing(
        _record("in_review", pending_action={**SIGN_UP, "x_vendor_hint": "ignored"})
    )
    assert await client2.accounts.refresh() == []
