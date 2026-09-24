"""Canonical private routing through C fanout, HTTP, re-emission and activity."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from itertools import cycle

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_server import ReportingStatusNotificationHandler
from adcp.reporting.outbox import (
    PgReportingActivityUnionStore,
    ReportingActivityProjector,
    ReportingActivitySupport,
    ReportingNotificationError,
    ReportingNotificationWorker,
    ReportingStatusProjector,
    ReportingStatusSupport,
    ReportingStatusSweeper,
)
from adcp.reporting.outbox.status_support import validate_status_claims

from . import test_reporting_status_projection_contract as _contract
from ._reliable_support import NotificationHarness, notification_subscription
from .test_reporting_notification_outbox import statement

status_harness = _contract.status_harness


def status_worker(h, n, *, activity=True):
    return ReportingNotificationWorker(
        outbox=h.status.outbox,
        subscriptions=n.subscriptions,
        signing=n.signing,
        cipher=n.cipher,
        clock=h.clock,
        activity=h.status.outbox if activity else None,
    )


async def drain_http(h, worker):
    for _ in range(80):
        expanded = await worker.expand_one(account_id="acct_a")
        delivered = await worker.deliver_one(account_id="acct_a")
        if not (expanded or delivered):
            return
        h.clock.advance(timedelta(seconds=6))
    pytest.fail("HTTP queue failed to become idle")


async def test_colliding_notification_ids_isolate_reemit_fanout_delivery_restart_and_stale_ack(
    status_harness, monkeypatch
):
    h = status_harness
    n = NotificationHarness(h.reliable)
    n.receiver.install(monkeypatch)
    for consumer in ("buyer", "auditor"):
        n.subscriptions.put(
            replace(
                notification_subscription(subscriber=consumer),
                principal_id=consumer,
                event_types=("reporting.status_changed",),
            )
        )
    obligation, revision, _ = await h.seed(readable=True)
    for consumer in ("buyer", "auditor"):
        await h.ledger.record_consumer_status(
            replace(
                statement(obligation, consumer),
                consumer_status="received",
                reporting_revision_id=revision.reporting_revision_id,
                observed_revision_content_sha256=revision.revision_content_sha256,
            )
        )
    await h.status.baseline(account_id="acct_a")
    await h.ledger.set_revision_readable(
        account_id="acct_a", reporting_revision_id=revision.reporting_revision_id, readable=False
    )
    import adcp.reporting.outbox.status as status_module

    identifiers = cycle(("collision-configuration", "collision-obligation"))
    with monkeypatch.context() as patch:
        patch.setattr(status_module, "uuid4", lambda: next(identifiers))
        await h.drain()
    events = await h.status.outbox.list_events(account_id="acct_a")
    assert len(events) == 6 and len({e.notification_id for e in events}) == 2
    assert {e.consumer_namespace for e in events} == {"", "buyer", "auditor"}
    with pytest.raises(ReportingNotificationError, match="event_unavailable"):
        await h.status.outbox.reemit(
            account_id="acct_a", notification_id="collision-obligation", now=h.clock()
        )
    worker = status_worker(h, n)
    while await worker.expand_one(account_id="acct_a"):
        pass
    original = await h.status.outbox.list_deliveries(account_id="acct_a")
    assert len(original) == 8
    for row in original:
        opened = n.cipher.open(row.delivery)
        b = row.delivery.binding
        assert b.consumer_namespace in {"", b.principal_id}
        assert "consumer_id" not in json.loads(opened.prepared.body)
        assert "consumer_namespace" not in json.loads(opened.prepared.body)
    stale = await h.status.outbox.claim_delivery(
        account_id="acct_a", now=h.clock(), lease_seconds=1
    )
    assert stale is not None
    h.clock.now = stale.expires_at
    cls = type(h.status)
    await h.reliable.restart()
    h.status = cls(h.ledger)
    worker = status_worker(h, n)
    for state in ("complete", "pending", "suppressed", "quarantined"):
        status_operation_2 = await h.status.outbox.finish_delivery(
            stale, now=h.clock(), state=state
        )
        assert not status_operation_2
    # Retry the same body and idempotency key after a real SDK HTTP failure.
    n.receiver.responses["buyer"].append(503)
    await drain_http(h, worker)
    assert {r.state for r in await h.status.outbox.list_deliveries(account_id="acct_a")} == {
        "complete"
    }
    for row in original:
        bodies = [
            r.body
            for r in n.receiver.received
            if r.idempotency_key == row.delivery.binding.idempotency_key
        ]
        assert bodies and len(set(bodies)) == 1
    for consumer in ("buyer", "auditor"):
        status_operation_3 = await h.status.outbox.reemit(
            account_id="acct_a",
            consumer_namespace=consumer,
            notification_id="collision-obligation",
            now=h.clock(),
        )
        assert status_operation_3 == 2
    await drain_http(h, worker)
    deliveries = await h.status.outbox.list_deliveries(account_id="acct_a")
    assert len(deliveries) == 10
    assert len({r.delivery.binding.idempotency_key for r in deliveries}) == 10
    for consumer in ("buyer", "auditor"):
        activity = await h.status.outbox.list_activity(account_id="acct_a", consumer_id=consumer)
        assert activity and all(a.binding.principal_id == consumer for a in activity)
    assert not await h.status.outbox.list_activity(account_id="other", consumer_id="buyer")


@pytest.mark.parametrize(
    "missing",
    [
        None,
        "schedule",
        "projector",
        "sweeper",
        "baseline",
        "schema",
        "worker",
        "task",
        "signing_probe",
        "account_surface",
    ],
)
async def test_status_capability_requires_each_component_independently_of_activity(
    status_harness, missing
):
    h = status_harness
    n = NotificationHarness(h.reliable)
    n.subscriptions.put(
        replace(notification_subscription(), event_types=("reporting.status_changed",))
    )
    await h.seed()
    if missing != "baseline":
        await h.status.baseline(account_id="acct_a")

    async def resolve(request, context):
        return ReportingStatusCaller("acct_a", "buyer")

    mount = ReportingStatusNotificationHandler(
        ReportingStatusHandler(h.ledger, consumer_status_enabled=True), resolve_caller=resolve
    )
    probe = replace(notification_subscription(), event_types=("reporting.status_changed",))
    support = ReportingStatusSupport(
        h.status,
        status_worker(h, n, activity=False),
        ReportingStatusProjector(h.status),
        ReportingStatusSweeper(h.status),
        True,
        ("acct_a",),
        True,
        mount,
        (probe,),
    )
    if missing == "schedule":
        support = replace(support, scheduled=False)
    elif missing == "projector":
        support = replace(support, projector=None)
    elif missing == "sweeper":
        support = replace(support, sweeper=None)
    elif missing == "worker":
        support = replace(support, worker=n.worker())
    elif missing == "task":
        support = replace(support, handler=None)
    elif missing == "signing_probe":
        support = replace(support, readiness_subscriptions=())
    elif missing == "account_surface":
        support = replace(support, account_surface_complete=False)
    elif missing == "schema" and h.reliable.blobs.pool is not None:
        async with h.reliable.blobs.pool.connection() as c:
            await c.execute("DROP INDEX reporting_status_scope_due")
    expected = missing is None and not isinstance(h.ledger, InMemoryReportingLedgerStore)
    flags = await support.advertised_notifications()
    assert flags == (
        {"status_task": "get_reporting_status", "status_notification": "reporting.status_changed"}
        if expected
        else {}
    )
    claim = {
        "media_buy": {
            "reporting_delivery": {
                "status_task": "get_reporting_status",
                "status_notification": "reporting.status_changed",
            }
        }
    }
    if expected:
        await validate_status_claims(claim, support=support)
    else:
        with pytest.raises(ReportingNotificationError, match="status_capability_requires"):
            await validate_status_claims(claim, support=support)


async def test_closed_activity_union_orders_before_limit_and_preserves_b_path(
    status_harness, monkeypatch
):
    h = status_harness
    if h.reliable.blobs.pool is None:
        pytest.skip("closed concrete durable B+C union")
    n = NotificationHarness(h.reliable)
    n.receiver.install(monkeypatch)
    n.subscriptions.put(
        replace(
            notification_subscription(),
            event_types=("reporting.status_changed", "reporting.ledger_changed"),
        )
    )
    _, revision, _ = await h.seed(readable=True)
    b_worker = n.worker()
    b_worker.activity = b_worker.outbox
    await h.status.baseline(account_id="acct_a")
    await drain_http(h, b_worker)
    h.clock.advance(timedelta(days=1))
    await h.ledger.set_revision_readable(
        account_id="acct_a", reporting_revision_id=revision.reporting_revision_id, readable=False
    )
    await h.drain()
    c_worker = status_worker(h, n)
    await drain_http(h, c_worker)
    union = PgReportingActivityUnionStore(b_worker.outbox, h.status.outbox)
    rows = await union.list_activity(account_id="acct_a", consumer_id="buyer", limit=2)
    assert len(rows) == 2 and all(
        r.binding.notification_type == "reporting.status_changed" for r in rows
    )
    assert len(await union.list_activity(account_id="acct_a", consumer_id="buyer", limit=3)) == 3
    assert len(await b_worker.outbox.list_activity(account_id="acct_a", consumer_id="buyer")) == 1
    projector = ReportingActivityProjector(union)
    status_support = ReportingStatusSupport(
        h.status,
        c_worker,
        ReportingStatusProjector(h.status),
        ReportingStatusSweeper(h.status),
        True,
        ("acct_a",),
    )
    support = ReportingActivitySupport(b_worker, h.ledger, projector, status_support)
    assert await support.capability_flags(account_activity=projector) == {
        "reporting": True,
        "account_notifications": True,
    }
    assert await support.capability_flags() == {"reporting": True, "account_notifications": False}
    # B-only wiring remains valid and deliberately reads only B history.
    assert await ReportingActivitySupport(
        b_worker, h.ledger, ReportingActivityProjector(b_worker.outbox)
    ).durable()
    h.clock.advance(timedelta(days=31))
    status_operation_1 = await union.purge_activity(
        account_id="acct_a", consumer_id="buyer", now=h.clock()
    )
    assert status_operation_1 == 3
    assert not await union.list_activity(account_id="acct_a", consumer_id="buyer")
