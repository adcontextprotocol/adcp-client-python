"""Buyer reporting events: ``poll``, ``events`` and ``handle_webhook`` (AdCP 3.3).

Events come from comparing durable snapshots of what the buyer reads, so a
replay before ``ack`` repeats the same ids, and a webhook is only a doorbell.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from adcp.reporting.subscriptions import (
    ReportingEventConflictError,
    VerifiedWebhook,
)
from adcp.reporting.subscriptions.facade import ReportingClient
from adcp.webhook_receiver import WebhookOutcome
from tests.reporting_inprocess import InProcessBuyer
from tests.test_reporting_subscription_managed import ACCOUNT, START, _ctx, _World

pytestmark = pytest.mark.asyncio


def _types(batch) -> list[str]:
    return sorted(event.type for event in batch.events)


async def test_the_first_poll_reports_the_present_and_ack_moves_on() -> None:
    world = _World()
    await world.subscribe()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    await world.service.run_worker()
    reporting = ReportingClient(InProcessBuyer(world.platform, _ctx))
    batch = await reporting.poll(now=world.clock())
    assert _types(batch) == [
        "account.covered",
        "revision.available",
        "revision.available",
        "subscription.state_changed",
    ]
    revision = next(e for e in batch.events if e.type == "revision.available")
    assert revision.account_id == ACCOUNT and revision.reporting_revision_id
    assert revision.id.startswith("rse_") and revision.agent_id
    # Not acked: the same events, with the same ids, come again.
    again = await reporting.poll(now=world.clock())
    assert [e.id for e in again.events] == [e.id for e in batch.events]
    await again.ack()
    after = await reporting.poll(now=world.clock())
    assert after.events == []
    # The stale batch can no longer be acknowledged.
    with pytest.raises(ReportingEventConflictError):
        await batch.ack()


async def test_missing_obligations_then_revisions_are_events() -> None:
    world = _World()
    await world.subscribe()
    reporting = ReportingClient(InProcessBuyer(world.platform, _ctx))
    world.clock.set(START + timedelta(minutes=30))
    await world.service.run_worker()
    first = await reporting.poll(baseline=True, now=world.clock())
    assert first.events == [] and first.report.definitive, first.report.issues
    await first.ack()
    late = await reporting.poll(now=START + timedelta(hours=3, minutes=30))
    assert _types(late) == ["period.missing_obligation"]
    assert late.events[0].occurred_at == "2026-11-01T03:00:00Z"
    await late.ack()
    world.clock.set(START + timedelta(hours=3, minutes=30))
    await world.service.run_worker()
    delivered = await reporting.poll(now=world.clock())
    assert _types(delivered) == ["revision.available", "revision.available"]


async def test_losing_and_regaining_authority_are_events() -> None:
    world = _World()
    await world.subscribe()
    reporting = ReportingClient(InProcessBuyer(world.platform, _ctx))
    world.clock.set(START + timedelta(minutes=30))
    await (await reporting.poll(baseline=True, now=world.clock())).ack()
    account = world.seller.accounts.pop(ACCOUNT)
    await world.service.touch(account_id=ACCOUNT)
    await world.service.drain_subscription_triggers()
    lost = await reporting.poll(now=world.clock())
    assert _types(lost) == ["account.unauthorized"]
    await lost.ack()
    world.clock.set(START + timedelta(hours=1))
    world.seller.accounts[ACCOUNT] = account
    await world.service.touch(account_id=ACCOUNT)
    await world.service.drain_subscription_triggers()
    back = await reporting.poll(now=world.clock())
    assert "account.regranted" in _types(back)
    regranted = next(e for e in back.events if e.type == "account.regranted")
    # A re-grant within retention keeps authorized_at: the chain continues.
    assert regranted.to_dict()["data"]["chain"] == "continued"
    assert regranted.to_dict()["data"]["authorized_at"] == "2026-10-01T00:00:00Z"


async def test_a_fresh_client_reads_the_chain_from_the_saved_snapshot() -> None:
    from adcp.reporting.subscriptions import InMemoryReportingEventStore

    world = _World()
    await world.subscribe()
    store = InMemoryReportingEventStore()

    def fresh() -> ReportingClient:
        return ReportingClient(InProcessBuyer(world.platform, _ctx), event_store=store)

    world.clock.set(START + timedelta(minutes=30))
    await (await fresh().poll(baseline=True, now=world.clock())).ack()
    account = world.seller.accounts.pop(ACCOUNT)
    await world.service.touch(account_id=ACCOUNT)
    await world.service.drain_subscription_triggers()
    await (await fresh().poll(now=world.clock())).ack()
    world.clock.set(START + timedelta(hours=1))
    world.seller.accounts[ACCOUNT] = account
    await world.service.touch(account_id=ACCOUNT)
    await world.service.drain_subscription_triggers()
    back = await fresh().poll(now=world.clock())
    regranted = next(e for e in back.events if e.type == "account.regranted")
    assert regranted.to_dict()["data"]["chain"] == "continued"


async def test_handle_webhook_needs_a_verified_delivery_and_polls_inline() -> None:
    world = _World()
    await world.subscribe()
    reporting = ReportingClient(InProcessBuyer(world.platform, _ctx))
    with pytest.raises(TypeError, match="verified"):
        await reporting.handle_webhook({"notification_type": "principal.changed"})
    with pytest.raises(ValueError, match="not verified"):
        await reporting.handle_webhook(WebhookOutcome(rejected=True))
    with pytest.raises(TypeError):
        VerifiedWebhook({}, "seller")
    outcome = WebhookOutcome(sender_identity="https://seller.example|kid", payload=None)
    batch = await reporting.handle_webhook(outcome)
    assert batch is not None and "subscription.state_changed" in _types(batch)


async def test_the_events_loop_acks_each_batch_and_wakes_on_the_doorbell() -> None:
    world = _World()
    await world.subscribe()
    reporting = ReportingClient(InProcessBuyer(world.platform, _ctx))
    stop = asyncio.Event()
    seen: list[str] = []

    async def consume() -> None:
        async for event in reporting.events(poll_every=timedelta(hours=1), stop=stop):
            seen.append(event.type)

    task = asyncio.create_task(consume())
    for _ in range(200):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert "subscription.state_changed" in seen
    # A verified webhook wakes the running loop instead of polling inline.
    ringing = await reporting.handle_webhook(
        VerifiedWebhook.from_outcome(WebhookOutcome(sender_identity="seller", payload=None))
    )
    assert ringing is None
    stop.set()
    await asyncio.wait_for(task, timeout=30)
    # The loop acknowledged what it delivered: only clock-driven period events remain.
    remaining = await reporting.poll()
    assert "subscription.state_changed" not in _types(remaining)


async def test_a_failed_read_never_fabricates_events() -> None:
    world = _World()
    await world.subscribe()
    client = InProcessBuyer(world.platform, _ctx)
    reporting = ReportingClient(client)
    world.clock.set(START + timedelta(minutes=30))
    await world.service.run_worker()
    account = world.seller.accounts.pop(ACCOUNT)
    await world.service.touch(account_id=ACCOUNT)
    await world.service.drain_subscription_triggers()
    await (await reporting.poll(now=world.clock())).ack()
    for task in ("get_principal", "list_accounts"):
        client.fail.add(task)
        batch = await reporting.poll(now=world.clock())
        assert batch.events == [], (task, [e.type for e in batch.events])
        await batch.ack()
        client.fail.discard(task)
    world.seller.accounts[ACCOUNT] = account


async def test_a_ring_during_a_cycle_is_not_swallowed() -> None:
    world = _World()
    await world.subscribe()
    reporting = ReportingClient(InProcessBuyer(world.platform, _ctx))
    source = reporting._event_source()
    polls = 0
    original = source.poll

    async def counting(**kwargs):
        nonlocal polls
        polls += 1
        batch = await original(**kwargs)
        if polls == 1:
            # A webhook rings while the first cycle is still running.
            source._doorbell.set()
        return batch

    source.poll = counting
    stop = asyncio.Event()

    async def consume() -> None:
        async for _event in reporting.events(poll_every=timedelta(hours=1), stop=stop):
            pass

    task = asyncio.create_task(consume())
    for _ in range(200):
        if polls >= 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=30)
    assert polls >= 2


async def test_the_events_loop_survives_an_acknowledgement_conflict() -> None:
    world = _World()
    await world.subscribe()
    reporting = ReportingClient(InProcessBuyer(world.platform, _ctx))
    other = ReportingClient(InProcessBuyer(world.platform, _ctx), event_store=None)
    source = reporting._event_source()
    other._event_source().store = source.store  # two workers, one store
    stop = asyncio.Event()
    seen: list[str] = []
    raced = False

    async def consume() -> None:
        nonlocal raced
        async for event in reporting.events(poll_every=timedelta(hours=1), stop=stop):
            seen.append(event.type)
            if not raced:
                raced = True
                # The other worker acknowledges the same snapshot first.
                await (await other.poll()).ack()

    task = asyncio.create_task(consume())
    for _ in range(200):
        if raced and task.done() is False and len(seen) >= 1:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    assert not task.done(), task.exception() if task.done() else None
    stop.set()
    await asyncio.wait_for(task, timeout=30)


def test_a_rejoin_is_a_new_account_covered_event() -> None:
    from datetime import datetime, timezone

    from adcp.reporting.subscriptions.events import derive_events

    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    key = '["analytics", "acc_a"]'
    joined = {"covered": [key]}
    first = derive_events({}, joined, agent_id="a", observed_at=now)
    left = {"covered": [], "covered_generations": joined["covered_generations"]}
    assert derive_events(joined, left, agent_id="a", observed_at=now) == []
    rejoined = {"covered": [key]}
    again = derive_events(left, rejoined, agent_id="a", observed_at=now)
    assert [e.type for e in first] == [e.type for e in again] == ["account.covered"]
    assert first[0].id != again[0].id
