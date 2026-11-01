"""The buyer's ``client.reporting.reconcile()`` and ``explain()`` against a managed seller.

The seller is the SDK's managed path, installed in process; the buyer reads it
only through wire tasks, so roster, grants and buys are all wire-sourced.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from adcp.reporting import PrincipalRef
from adcp.reporting.subscriptions import diff_coverage_traces
from adcp.reporting.subscriptions.facade import ReportingClient
from tests.reporting_inprocess import InProcessBuyer
from tests.test_reporting_subscription_managed import (
    ACCOUNT,
    AGENCY,
    OFFERING,
    START,
    _ctx,
    _World,
)

pytestmark = pytest.mark.asyncio


def _buyer(world: _World) -> tuple[ReportingClient, InProcessBuyer]:
    client = InProcessBuyer(world.platform, _ctx)
    return ReportingClient(client), client


async def _delivered_world() -> _World:
    world = _World()
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    turn = await world.service.run_worker()
    assert turn.subscription_obligations_written == 2
    return world


async def test_reconcile_derives_the_roster_and_reports_delivered_periods() -> None:
    world = await _delivered_world()
    reporting, _ = _buyer(world)
    report = await reporting.reconcile(now=world.clock())
    assert report.definitive, report.issues
    assert report.accounts == (ACCOUNT,)
    assert report.summary.owed == report.summary.delivered == 2
    first = report.periods[0]
    assert first.to_dict() | {"message": None} == {
        "account_id": ACCOUNT,
        "subscription_id": "analytics-hourly",
        "subscription_version": 1,
        "offering_id": OFFERING["offering_id"],
        "report_definition_id": first.report_definition_id,
        "media_buy_ids": ["media-buy-a1"],
        "period": {
            "start": "2026-11-01T01:00:00Z",
            "end": "2026-11-01T02:00:00Z",
            "timezone": "UTC",
        },
        "status": "delivered",
        "reason": "owed",
        "responsible_party": "none",
        "message": None,
        "expected_at": "2026-11-01T02:10:00Z",
        "reporting_obligation_id": first.reporting_obligation_id,
        "reporting_revision_id": first.reporting_revision_id,
        "finality": first.finality,
    }
    assert first.message.startswith("Owed: media-buy-a1 under")
    assert first.reporting_obligation_id and first.reporting_revision_id


async def test_a_missing_obligation_is_the_seller_after_grace_and_pending_before() -> None:
    world = _World()
    await world.subscribe()
    reporting, _ = _buyer(world)
    # No worker turn ran: the seller planned nothing.
    within = await reporting.reconcile(now=START + timedelta(hours=2, minutes=30))
    assert [line.status for line in within.periods] == ["pending"]
    later = await reporting.reconcile(now=START + timedelta(hours=3, minutes=30))
    assert [(line.status, line.responsible_party) for line in later.periods] == [
        ("missing_obligation", "seller"),
        ("pending", "none"),
    ]
    assert later.summary.missing_obligation == 1 and later.summary.pending == 1
    # A zero grace is the caller's explicit choice.
    strict = await reporting.reconcile(now=START + timedelta(hours=3), grace=timedelta(0))
    assert [line.status for line in strict.periods] == ["missing_obligation"] * 2


async def test_include_not_owed_lists_the_rest_of_the_flight_with_reasons() -> None:
    world = _World()
    world.seller.buys[0]["confirmed_at"] = "2026-11-01T02:30:00Z"
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    await world.service.run_worker()
    reporting, _ = _buyer(world)
    report = await reporting.reconcile(now=world.clock(), include_not_owed=True)
    assert [(line.period["start"], line.status, line.reason) for line in report.periods] == [
        ("2026-11-01T01:00:00Z", "not_owed", "before_confirmation"),
        ("2026-11-01T02:00:00Z", "delivered", "owed"),
    ]
    assert report.summary.not_owed == 1 and report.summary.owed == 1


async def test_since_and_explicit_accounts_narrow_the_report() -> None:
    world = await _delivered_world()
    reporting, _ = _buyer(world)
    report = await reporting.reconcile(now=world.clock(), since="2026-11-01T02:00:00Z")
    assert [line.period["start"] for line in report.periods] == ["2026-11-01T02:00:00Z"]
    unknown = await reporting.reconcile(now=world.clock(), accounts=["account-unknown"])
    assert not unknown.definitive and unknown.periods == ()
    assert [issue.code for issue in unknown.issues] == ["SUBSCRIPTION_ACCOUNT_NOT_COVERED"]


async def test_a_partial_roster_is_never_a_smaller_success() -> None:
    world = await _delivered_world()
    reporting, client = _buyer(world)
    client.fail.add("list_accounts")
    report = await reporting.reconcile(now=world.clock())
    assert not report.definitive
    assert "SUBSCRIPTION_ROSTER_UNAVAILABLE" in {issue.code for issue in report.issues}


async def test_a_lost_account_reads_from_the_last_facts_and_withholds() -> None:
    world = _World()
    await world.subscribe()
    reporting, _ = _buyer(world)
    world.clock.set(START + timedelta(hours=2, minutes=5))
    # The buyer read the account while authorized.
    first = await reporting.reconcile(now=world.clock())
    assert first.accounts == (ACCOUNT,)
    # The 01:00 period ended at 02:00 and is due 02:10; authority is lost before that.
    del world.seller.accounts[ACCOUNT]
    await world.service.touch(account_id=ACCOUNT)
    await world.service.drain_subscription_triggers()
    report = await reporting.reconcile(now=world.clock())
    assert report.accounts == (ACCOUNT,)
    assert [(line.status, line.reason) for line in report.periods] == [
        ("withheld", "owed:withheld")
    ]
    assert report.periods[0].responsible_party == "none"


async def test_buyer_and_seller_explain_one_period_identically() -> None:
    world = await _delivered_world()
    reporting, _ = _buyer(world)
    buyer = await reporting.explain(
        account_id=ACCOUNT, period="2026-11-01T01:30:00Z", now=world.clock()
    )
    seller = await world.service.explain_coverage(
        PrincipalRef(None, AGENCY), ACCOUNT, period="2026-11-01T01:30:00Z"
    )
    assert [trace.side for trace in buyer] == ["buyer"]
    assert (buyer[0].verdict, buyer[0].reason) == ("owed", "owed")
    assert buyer[0].inputs_digest == seller[0].inputs_digest
    assert diff_coverage_traces(buyer[0], seller[0]) is None
    day = await reporting.explain(account_id=ACCOUNT, period="2026-11-01", now=world.clock())
    assert [trace.reason for trace in day][:2] == ["outside_flight", "owed"]
    with pytest.raises(KeyError):
        await reporting.explain(account_id=ACCOUNT, period="2026-11-01", subscription_id="missing")


async def test_the_cli_reconciles_and_explains_against_an_agent(monkeypatch, capsys) -> None:
    import asyncio
    import json

    from adcp.reporting import cli

    world = await _delivered_world()
    reporting, _ = _buyer(world)

    class Agent:
        def __init__(self) -> None:
            self.reporting = reporting

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(cli, "_agent_client", lambda identifier: Agent())
    loop = asyncio.get_running_loop()
    code = await loop.run_in_executor(None, cli.main, ["reconcile", "--agent", "seller", "--json"])
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["summary"]["owed"] == 2
    argv = ["explain", "--agent", "seller", "--account", ACCOUNT, "--period", "2026-11-01"]
    assert await loop.run_in_executor(None, cli.main, argv) == 0
    assert "[owed]" in capsys.readouterr().out
