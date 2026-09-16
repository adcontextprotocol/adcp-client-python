"""Optional wiring, truthful capabilities, and complete dirty scope evidence."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ReportingDeliveryScope,
)
from adcp.reporting.outbox import (
    InMemoryReportingOutbox,
    ReportingNotificationError,
    ReportingStatusScope,
)

from ._generation_support import NOW, configuration
from ._reconciliation_support import scenario
from ._reliable_support import reliable_factory
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


async def test_configuration_lifecycle_state_is_shared_by_memory_and_postgres(
    notification_harness,
):
    """rc.3 walks one immutable generation through ready -> inactive.

    Both stores must apply a lifecycle-only re-put to the retained generation
    and co-commit exactly one status-dirty generation for it, so #1168B's
    projector sees the same journal on either backend. A PostgreSQL
    ``ON CONFLICT DO NOTHING`` that dropped the update would leave a
    deactivated feed minting obligations forever and never mark it dirty.
    """
    h = notification_harness
    store = h.reliable.store
    first = configuration()
    assert first.deactivated_at is not None
    inactive = replace(
        first,
        deactivated_at=first.deactivated_at + timedelta(hours=1),
        automated_recovery_window=timedelta(hours=2),
        status_retention_days=30,
    )
    await store.put_configuration(first)
    # An unchanged re-put stays a no-op on both stores.
    await store.put_configuration(first)
    await store.put_configuration(inactive)
    await store.put_configuration(inactive)
    # Reverting the lifecycle is a further transition, not a rollback.
    await store.put_configuration(first)

    retained = await store.list_configurations(account_id="acct_a")
    assert [item.deactivated_at for item in retained] == [first.deactivated_at]
    assert retained[0].automated_recovery_window == first.automated_recovery_window
    assert retained[0].status_retention_days == first.status_retention_days

    dirty = [
        record
        for record in await h.outbox.read_status_dirty(account_id="acct_a")
        if record.reason == "configuration"
    ]
    assert [record.cause_generation for record in dirty] == [1, 2, 3]
    assert [record.scope for record in dirty] == [
        ReportingStatusScope("acct_a", first.generation_key)
    ] * 3
    assert dirty[0].before is None
    assert dirty[1].before == dirty[0].after and dirty[2].before == dirty[1].after
    assert dirty[1].after.deactivated_at == inactive.deactivated_at
    assert dirty[1].after.automated_recovery_seconds == 7200
    assert dirty[1].after.status_retention_days == 30
    assert dirty[2].after == dirty[0].after
    # Lifecycle state is not a ledger change: no logical event is emitted.
    assert await h.outbox.list_events(account_id="acct_a") == ()

    # Changed content is still a conflict that mutates nothing and enqueues
    # nothing, on the same generation the lifecycle re-put just touched.
    with pytest.raises(LedgerConflictError) as conflict:
        await store.put_configuration(replace(first, report_definition_id="rpd_other"))
    assert conflict.value.code == "CONFIGURATION_GENERATION_IMMUTABLE"
    assert await store.list_configurations(account_id="acct_a") == retained
    assert [
        record
        for record in await h.outbox.read_status_dirty(account_id="acct_a")
        if record.reason == "configuration"
    ] == dirty


async def test_reconciled_managed_generation_can_still_be_deactivated(notification_harness):
    """The parent reference-immutability guard must not freeze lifecycle state.

    A Managed generation referenced by reconciliation records keeps its
    published content immutable while still reaching rc.3 ``inactive``.
    """
    h = notification_harness
    s = await scenario(h.reliable.store)
    await h.reliable.store.commit_materialization(s.outcome)
    (config,) = await h.reliable.store.list_configurations(account_id="acct_a")
    assert config.generation_key == s.binding.generation_key
    assert config.deactivated_at is not None
    stopped = replace(config, deactivated_at=config.deactivated_at + timedelta(hours=3))
    await h.reliable.store.put_configuration(stopped)
    (retained,) = await h.reliable.store.list_configurations(account_id="acct_a")
    assert retained.deactivated_at == stopped.deactivated_at
    assert retained.report_definition_id == config.report_definition_id
    latest = [
        record
        for record in await h.outbox.read_status_dirty(account_id="acct_a")
        if record.reason == "configuration"
    ][-1]
    assert latest.before.deactivated_at == config.deactivated_at
    assert latest.after.deactivated_at == stopped.deactivated_at
    with pytest.raises(LedgerConflictError) as conflict:
        await h.reliable.store.put_configuration(replace(config, report_definition_id="rpd_other"))
    assert conflict.value.code == "CONFIGURATION_GENERATION_IMMUTABLE"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_default_off_issue_waive_keeps_parent_return_values(backend):
    """Opting out must not change any existing Core return value.

    ``retired_at`` is SDK bookkeeping with no rc.3 field behind it, so the A
    slice may not silently freeze it. A repeated waive keeps advancing it and a
    later ``external_ref`` still lands, exactly as before the outbox existed.
    """
    async with reliable_factory(backend, notifications=False) as reliable:
        store = reliable.store
        await store.ensure_issue_opened(
            issue_key="k", account_id="acct_a", consumer_id=None, observed_at=NOW
        )
        await store.set_issue_state(
            issue_key="k", account_id="acct_a", state="acknowledged", at=NOW
        )
        first = await store.set_issue_state(
            issue_key="k", account_id="acct_a", state="waived", at=NOW + timedelta(hours=1)
        )
        assert first.retired_at == NOW + timedelta(hours=1)
        again = await store.set_issue_state(
            issue_key="k", account_id="acct_a", state="waived", at=NOW + timedelta(hours=9)
        )
        assert again.retired_at == NOW + timedelta(hours=9)
        tagged = await store.set_issue_state(
            issue_key="k",
            account_id="acct_a",
            state="waived",
            at=NOW + timedelta(hours=20),
            external_ref="ticket-2",
        )
        assert tagged.retired_at == NOW + timedelta(hours=20)
        assert tagged.external_ref == "ticket-2"


async def test_issue_dirty_records_track_actual_retained_evidence(notification_harness):
    """The journal follows the record, so a projector can trust either store.

    An idempotent re-acknowledge moves nothing and enqueues nothing. A repeated
    waive does move ``retired_at``, so it must stay reconstructable.
    """
    h = notification_harness
    store = h.reliable.store
    await store.ensure_issue_opened(
        issue_key="k", account_id="acct_a", consumer_id=None, observed_at=NOW
    )
    await store.set_issue_state(issue_key="k", account_id="acct_a", state="acknowledged", at=NOW)
    await store.set_issue_state(
        issue_key="k", account_id="acct_a", state="acknowledged", at=NOW + timedelta(hours=1)
    )
    waived = await store.set_issue_state(
        issue_key="k", account_id="acct_a", state="waived", at=NOW + timedelta(hours=2)
    )
    rewaived = await store.set_issue_state(
        issue_key="k", account_id="acct_a", state="waived", at=NOW + timedelta(hours=3)
    )
    issues = [
        record
        for record in await h.outbox.read_status_dirty(account_id="acct_a")
        if record.reason == "issue"
    ]
    assert [record.after.issue_state for record in issues] == [
        "open",
        "acknowledged",
        "waived",
        "waived",
    ]
    assert [record.cause_generation for record in issues] == [1, 2, 3, 4]
    assert issues[2].before.issue_state == "acknowledged"
    assert issues[2].after.retired_at == waived.retired_at
    assert issues[3].before.retired_at == waived.retired_at
    assert issues[3].after.retired_at == rewaived.retired_at
    assert await h.outbox.list_events(account_id="acct_a") == ()
