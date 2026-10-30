"""Access means access: reporting tasks follow the principal's current grant.

There is no window after a loss of authority. A revoked account is unknown to
every task, receipts and statuses included. A seller that wants the buyer to
finish reconciliation keeps a reduced grant instead of revoking, and coverage
continues while that grant lists ``get_reporting_status``.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.subscriptions.engine import (
    _Account,
    _PrincipalState,
    decode_principal_state,
    encode_principal_state,
)
from tests.test_reporting_subscription_service import ACCOUNT, AGENCY, NOW, _buy, _Seller

RECONCILIATION = frozenset(
    {"get_reporting_status", "sync_reporting_receipts", "sync_reporting_status"}
)


class _Receipts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handle(self, request, *, account_id, consumer_id):
        self.calls.append({"account_id": account_id, "consumer_id": consumer_id})
        return {"status": "completed", "results": []}


class _Materializer:
    async def run_once(self):
        return None


def _seller() -> tuple[_Seller, _Receipts]:
    receipts = _Receipts()
    seller = _Seller(
        principals=(AGENCY,),
        service_kwargs={
            "receipt_handler": receipts,
            "materialization_worker": _Materializer(),
            "consumer_status_enabled": True,
        },
    )
    return seller, receipts


def _status() -> dict[str, Any]:
    return {
        "statuses": [
            {
                "reporting_status_id": "rps_grant_status_0001",
                "delivery_config_id": "analytics-hourly",
                "delivery_config_version": 1,
                "report_definition_id": "PAID_MEDIA_DAILY_V1",
                "period": {
                    "start": "2026-11-01T01:00:00Z",
                    "end": "2026-11-01T02:00:00Z",
                    "source_timezone": "UTC",
                },
                "consumer_status": "obligation_missing",
                "status_as_of": "2026-11-01T03:00:00Z",
            }
        ]
    }


async def _subscribed(allowed_tasks=None) -> tuple[_Seller, _Receipts]:
    seller, receipts = _seller()
    await seller.subscribe(
        AGENCY,
        [_buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z")],
        allowed_tasks=allowed_tasks,
    )
    await seller.service.run_worker(now=NOW)
    return seller, receipts


@pytest.mark.asyncio
async def test_a_reduced_reconciliation_grant_keeps_reconciliation_working() -> None:
    seller, receipts = await _subscribed(RECONCILIATION)
    view = await seller.status(AGENCY, "summary")
    assert view["subscription_coverage"][0]["status"] == "active"
    await seller.service.sync_reporting_receipts({"receipts": []})
    assert receipts.calls == [{"account_id": ACCOUNT, "consumer_id": AGENCY.subject}]
    ingested = await seller.service.sync_reporting_status(_status())
    assert ingested["results"][0]["result"] in {"recorded", "unchanged"}, ingested
    # The grant does not list get_media_buy_delivery, so exact reads are refused.
    with pytest.raises(LedgerConflictError) as refused:
        await seller.service.get_revision_content({"reporting_revision_id": "rpr_any"})
    assert refused.value.code == "ACCOUNT_NOT_FOUND"


@pytest.mark.asyncio
async def test_a_full_revocation_makes_the_account_unknown_to_every_task() -> None:
    seller, receipts = await _subscribed()
    del seller.host.accounts[ACCOUNT]
    await seller.engine.reconcile(AGENCY)
    for call in (
        lambda: seller.service.sync_reporting_receipts({"receipts": []}),
        lambda: seller.service.sync_reporting_status(_status()),
        lambda: seller.service.get_reporting_status({"view": "summary"}),
    ):
        with pytest.raises(LedgerConflictError) as refused:
            await call()
        assert refused.value.code == "ACCOUNT_NOT_FOUND"
    # No post-revocation window: the receipt never reached the handler.
    assert receipts.calls == []


@pytest.mark.asyncio
async def test_a_grant_without_get_reporting_status_ends_coverage() -> None:
    seller, receipts = await _subscribed(RECONCILIATION)
    seller.host.accounts[ACCOUNT] = type(seller.host.accounts[ACCOUNT])(
        ACCOUNT,
        timezone="UTC",
        authorized_at=seller.host.accounts[ACCOUNT].authorized_at,
        allowed_tasks=frozenset({"sync_reporting_receipts"}),
    )
    await seller.engine.reconcile(AGENCY)
    assert await seller.engine.grants_task(AGENCY, ACCOUNT, "sync_reporting_receipts") is False
    assert await seller.engine.subscription_coverage(AGENCY, ACCOUNT) == []
    with pytest.raises(LedgerConflictError):
        await seller.service.sync_reporting_receipts({"receipts": []})
    assert receipts.calls == []


def test_allowed_tasks_persist_and_unscoped_documents_are_unchanged() -> None:
    state = _PrincipalState()
    state.accounts["scoped"] = _Account(timezone="UTC", operator=None, allowed_tasks=RECONCILIATION)
    state.accounts["unscoped"] = _Account(timezone="UTC", operator=None)
    document = encode_principal_state(state)
    assert document["accounts"]["scoped"]["allowed_tasks"] == sorted(RECONCILIATION)
    assert "allowed_tasks" not in document["accounts"]["unscoped"]
    decoded = decode_principal_state(document)
    assert decoded.accounts["scoped"].allowed_tasks == RECONCILIATION
    assert decoded.accounts["unscoped"].allowed_tasks is None
