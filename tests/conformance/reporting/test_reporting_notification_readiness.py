"""Optional wiring, truthful capabilities, and complete dirty scope evidence."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore, ReportingDeliveryScope
from adcp.reporting.outbox import (
    InMemoryReportingOutbox,
    ReportingNotificationError,
    ReportingStatusScope,
)

from ._generation_support import NOW, configuration
from ._reconciliation_support import scenario
from .test_reporting_notification_outbox import seed


async def test_capability_fragment_exposes_only_complete_notifications(notification_harness):
    h = notification_harness
    await seed(h)
    fields = await h.worker().advertised_notifications(h.reliable.store, account_id="acct_a")
    assert fields == {
        "ledger_notification": "reporting.ledger_changed",
        "supports_webhook_activity": False,
    }
    assert "status_notification" not in fields
    # A Core obligation alone cannot authorize a readiness capability.
    core_scope = ReportingDeliveryScope(configuration().generation_key, "buyer", "rpo_acct_a")
    with pytest.raises(ReportingNotificationError):
        await h.worker().advertised_notifications(
            h.reliable.store, account_id="acct_a", ready_scope=core_scope
        )


async def test_managed_capability_requires_retained_configuration_and_frozen_scope(
    notification_harness,
):
    h = notification_harness
    s = await scenario(h.reliable.store)
    fields = await h.worker().advertised_notifications(
        h.reliable.store, account_id="acct_a", ready_scope=s.delivery.scope
    )
    assert fields == {
        "ledger_notification": "reporting.ledger_changed",
        "readiness_notification": "reporting.delivery_ready",
        "supports_webhook_activity": False,
    }
    with pytest.raises(ReportingNotificationError):
        await h.worker().advertised_notifications(
            h.reliable.store, account_id="other", ready_scope=s.delivery.scope
        )
    with pytest.raises(ReportingNotificationError):
        await h.worker().advertised_notifications(
            h.reliable.store,
            account_id="acct_a",
            ready_scope=replace(s.delivery.scope, consumer_id="unconfigured"),
        )


async def test_capability_rejects_unwired_store_and_unavailable_signing(notification_harness):
    h = notification_harness
    with pytest.raises(ReportingNotificationError, match="notification_chain_unready"):
        await h.worker().advertised_notifications(
            InMemoryReportingLedgerStore(), account_id="acct_a"
        )
    from adcp.webhook_sender import ScopePermanentlyUnknown

    worker = h.worker()
    worker.signing = None
    with pytest.raises(ScopePermanentlyUnknown):
        await worker.advertised_notifications(h.reliable.store, account_id="acct_a")


@pytest.mark.parametrize("damage", ["generation", "obligation", "feed", "consumer_replay"])
async def test_optional_issue_scope_must_resolve_inside_trusted_namespace(
    notification_harness, damage
):
    h = notification_harness
    obligation, _, _ = await seed(h)
    scope = ReportingStatusScope.for_obligation(obligation, "buyer")
    if damage == "generation":
        scope = replace(
            scope, generation_key=replace(scope.generation_key, delivery_config_version=77)
        )
    elif damage == "obligation":
        scope = replace(scope, reporting_obligation_id="missing-or-foreign")
    elif damage == "feed":
        scope = replace(scope, feed_purpose="billing")
    else:
        await h.reliable.store.ensure_issue_opened(
            account_id="acct_a", consumer_id="auditor", issue_key="same-opaque-key", observed_at=NOW
        )
    before = await h.outbox.read_status_dirty(account_id="acct_a")
    with pytest.raises(ReportingNotificationError, match="invalid_status_scope"):
        await h.reliable.store.ensure_issue_opened(
            account_id="acct_a",
            consumer_id="buyer",
            issue_key="same-opaque-key",
            observed_at=NOW,
            status_scope=scope,
        )
    assert await h.outbox.read_status_dirty(account_id="acct_a") == before


async def test_mutable_memory_configuration_lifecycle_keeps_core_semantics():
    store = InMemoryReportingLedgerStore(notifications=True, clock=lambda: NOW)
    outbox = InMemoryReportingOutbox(store)
    first = configuration()
    second = replace(
        first,
        deactivated_at=first.deactivated_at + timedelta(hours=1),
        automated_recovery_window=timedelta(hours=2),
    )
    await store.put_configuration(first)
    await store.put_configuration(second)
    await store.put_configuration(second)
    await store.put_configuration(first)
    dirty = await outbox.read_status_dirty(account_id="acct_a")
    assert [record.cause_generation for record in dirty] == [1, 2, 3]
    assert dirty[1].before == dirty[0].after and dirty[2].before == dirty[1].after
    assert dirty[1].after.deactivated_at == second.deactivated_at
    assert dirty[1].after.automated_recovery_seconds == 7200
    assert dirty[2].after == dirty[0].after
    assert await outbox.list_events(account_id="acct_a") == ()
