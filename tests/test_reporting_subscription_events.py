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
