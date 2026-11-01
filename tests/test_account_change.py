"""``client.accounts.change`` and ``use`` (AdCP 3.3.0-beta.3, ``account.requests``).

A money change needs the revision the human approved; other changes read it.
A separate or matched account never re-points the registry on its own.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp import (
    AccountMatched,
    AccountPending,
    AccountRejected,
    AccountRequestError,
    AccountSeparate,
    AccountUpdated,
    next_step,
)
from adcp.exceptions import is_pre_dispatch_error
from adcp.types.core import TaskResult, TaskStatus
from tests.test_account_lifecycle import KEY, make_client
from tests.test_account_outcomes import REQUESTS, _completed, _record

pytestmark = pytest.mark.asyncio
ACCOUNT = {"account_id": "acc-1", "name": "Acme", "status": "active", "revision": 3}


def _client(*responses: Any, accounts: list[dict[str, Any]] | None = None):
    client = make_client(requests=REQUESTS)
    client.adapter.request_account = AsyncMock(side_effect=list(responses))
    client.adapter.list_accounts = AsyncMock(
        return_value=_completed({"accounts": accounts if accounts is not None else [ACCOUNT]})
    )
    client.adapter._parse_response = lambda raw, _type: raw
    return client


def _resolved(**fields: Any) -> TaskResult:
    return _completed({"status": "completed", "request": _record("resolved", **fields)})


async def test_a_money_change_needs_the_approved_revision_and_sends_nothing_without_it() -> None:
    client = _client()
    with pytest.raises(AccountRequestError) as refused:
        await client.accounts.change("acc-1", {"payment_terms": "net_60"})
    assert is_pre_dispatch_error(refused.value) and refused.value.recovery == "correctable"
    assert refused.value.error_codes == ["ACCOUNT_REVISION_REQUIRED"]
    client.adapter.request_account.assert_not_called()
    client.adapter.list_accounts.assert_not_called()


async def test_a_money_change_with_revision_is_updated_with_effective_at() -> None:
    client = _client(
        _resolved(
            account_id="acc-1",
            resolution="updated",
            effective_at="2026-12-01T00:00:00Z",
            account={**ACCOUNT, "revision": 4, "payment_terms": "net_60"},
        )
    )
    outcome = await client.accounts.change("acc-1", {"payment_terms": "net_60"}, revision=3)
    assert isinstance(outcome, AccountUpdated)
    assert (outcome.account_id, outcome.revision, outcome.effective_at) == (
        "acc-1",
        4,
        "2026-12-01T00:00:00Z",
    )
    assert next_step(outcome) == "none"
    sent = client.adapter.request_account.call_args.args[0]
    assert sent["revision"] == 3 and sent["payment_terms"] == "net_60"
    assert "allow_separate_account" not in sent
    client.adapter.list_accounts.assert_not_called()


async def test_other_changes_read_the_revision_and_retry_once_on_a_concurrent_bump() -> None:
    conflict = TaskResult(
        status=TaskStatus.FAILED,
        data={"errors": [{"code": "CONFLICT", "message": "stale revision"}]},
        success=False,
    )
    client = _client(
        conflict,
        _resolved(account_id="acc-1", resolution="updated", account={**ACCOUNT, "revision": 5}),
    )
    client.adapter.list_accounts = AsyncMock(
        side_effect=[
            _completed({"accounts": [ACCOUNT]}),
            _completed({"accounts": [{**ACCOUNT, "revision": 4}]}),
        ]
    )
    outcome = await client.accounts.change("acc-1", {"timezone": "Europe/Paris"})
    assert isinstance(outcome, AccountUpdated) and outcome.revision == 5
    first, second = (call.args[0] for call in client.adapter.request_account.call_args_list)
    assert (first["revision"], second["revision"]) == (3, 4)


async def test_a_conflict_on_the_changed_field_is_not_retried() -> None:
    conflict = TaskResult(
        status=TaskStatus.FAILED,
        data={"errors": [{"code": "CONFLICT", "message": "stale revision"}]},
        success=False,
    )
    client = _client(conflict)
    client.adapter.list_accounts = AsyncMock(
        side_effect=[
            _completed({"accounts": [{**ACCOUNT, "timezone": "UTC"}]}),
            _completed({"accounts": [{**ACCOUNT, "revision": 4, "timezone": "Asia/Tokyo"}]}),
        ]
    )
    with pytest.raises(AccountRequestError) as error:
        await client.accounts.change("acc-1", {"timezone": "Europe/Paris"})
    assert "CONFLICT" in error.value.error_codes
    assert client.adapter.request_account.call_count == 1


async def test_a_separate_account_lists_what_is_missing_and_never_repoints() -> None:
    client = _client(
        _resolved(
            account_id="acc-2",
            resolution="created",
            not_carried_over=["reporting_delivery_configs", "creatives"],
            account={"account_id": "acc-2", "name": "Acme BE", "status": "active"},
        )
    )
    await client.accounts.use(KEY, "acc-1", status="active")
    outcome = await client.accounts.change(
        "acc-1",
        {"brand": {"domain": "acme.example", "countries": ["BE"]}},
        allow_separate_account=True,
    )
    assert isinstance(outcome, AccountSeparate)
    assert (outcome.account_id, outcome.previous_account_id) == ("acc-2", "acc-1")
    assert outcome.not_carried_over == ("reporting_delivery_configs", "creatives")
    assert outcome.key is not None and outcome.key["brand"]["domain"] == "acme.example"
    assert client.adapter.request_account.call_args.args[0]["allow_separate_account"] is True
    # Nothing moved: the key still names the old account until use().
    assert (await client.accounts.get(KEY)).account_id == "acc-1"
    client.adapter.list_accounts = AsyncMock(
        return_value=_completed(
            {"accounts": [{"account_id": "acc-2", "name": "Acme BE", "status": "payment_required"}]}
        )
    )
    await client.accounts.use(outcome.key, outcome.account_id)
    record = await client.accounts.get(KEY)
    # The new account's own status is kept, so the spend gate still applies.
    assert (record.account_id, record.status) == ("acc-2", "payment_required")


async def test_matched_pending_and_rejected_outcomes() -> None:
    client = _client(
        _resolved(
            account_id="acc-9", resolution="matched", account={**ACCOUNT, "account_id": "acc-9"}
        ),
        _completed(
            {
                "status": "completed",
                "request": _record(
                    "in_review",
                    pending_action={
                        "waiting_on": "seller",
                        "kind": "review",
                        "message": "Reviewing",
                    },
                ),
            }
        ),
        _completed(
            {
                "status": "completed",
                "request": _record(
                    "rejected",
                    errors=[
                        {
                            "code": "ACCOUNT_CHANGE_REQUIRES_NEW_ACCOUNT",
                            "message": "Needs a new account",
                        }
                    ],
                ),
            }
        ),
    )
    matched = await client.accounts.change("acc-1", {"timezone": "UTC"}, revision=3)
    assert isinstance(matched, AccountMatched) and matched.account_id == "acc-9"
    pending = await client.accounts.change("acc-1", {"timezone": "UTC"}, revision=3)
    assert isinstance(pending, AccountPending) and next_step(pending) == "wait_for_seller"
    rejected = await client.accounts.change("acc-1", {"timezone": "UTC"}, revision=3)
    assert isinstance(rejected, AccountRejected) and rejected.requires_new_account


async def test_change_refuses_unknown_fields_and_sellers_without_account_requests() -> None:
    client = _client()
    with pytest.raises(AccountRequestError) as unknown:
        await client.accounts.change("acc-1", {"sandbox": True})
    assert unknown.value.error_codes == ["ACCOUNT_CHANGE_INVALID"]
    plain = make_client()
    plain.adapter.request_account = AsyncMock()
    with pytest.raises(AccountRequestError) as unsupported:
        await plain.accounts.change("acc-1", {"timezone": "UTC"})
    assert unsupported.value.error_codes == ["ACCOUNT_REQUESTS_UNSUPPORTED"]
    assert is_pre_dispatch_error(unsupported.value)
    plain.adapter.request_account.assert_not_called()


async def test_a_pending_change_is_read_back_by_request_id() -> None:
    client = _client()
    client.adapter.list_accounts = AsyncMock(
        side_effect=[
            _completed(
                {
                    "accounts": [],
                    "account_requests": [
                        _record(
                            "resolved",
                            account_id="acc-1",
                            resolution="updated",
                            account={**ACCOUNT, "revision": 4},
                        )
                    ],
                }
            ),
            _completed({"accounts": [], "account_requests": []}),
        ]
    )
    resolved = await client.accounts.change_status("acc-1", "req-1")
    assert isinstance(resolved, AccountUpdated) and resolved.revision == 4
    gone = await client.accounts.change_status("acc-1", "req-1")
    assert isinstance(gone, AccountRejected)
