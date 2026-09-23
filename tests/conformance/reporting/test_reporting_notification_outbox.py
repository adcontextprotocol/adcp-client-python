"""Shared deterministic notification state-machine vectors: memory and real PG."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    ConsumerStatusRecord,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ReportingAdjustmentRecord,
    ReportingProducer,
)
from adcp.reporting.outbox import (
    ReportingEnvelopeCipher,
    ReportingLegacyAuthentication,
    ReportingNotificationError,
    ReportingStatusScope,
    validate_notification_payload,
)
from adcp.webhook_sender import ScopePermanentlyUnknown, ScopeTransientlyUnavailable, WebhookSender

from ._generation_support import END, NOW, START, configuration, obligation_for, revision_for
from ._reconciliation_support import scenario
from ._reliable_support import (
    Barrier,
    NotificationHarness,
    SimulatedCrash,
    notification_subscription,
)


async def seed(h: NotificationHarness, *, account: str = "acct_a", official: bool = False):
    store = h.reliable.store
    config = configuration(account)
    await store.put_configuration(config)
    obligation = await store.commit_obligation(obligation_for(config))
    revision, rows = revision_for(obligation)
    if official:
        revision = replace(
            revision,
            finality="official",
            finality_basis="source_final",
            finality_policy_id="policy-1",
            finalized_at=END,
        )
    await store.commit_revision(revision, rows)
    return obligation, revision, rows


def statement(obligation, consumer="buyer", suffix="one"):
    return ConsumerStatusRecord(
        reporting_status_id=f"consumer-status-{consumer}-{suffix}",
        account_id=obligation.account_id,
        consumer_id=consumer,
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        report_definition_id=obligation.report_definition_id,
        period_start=START,
        period_end=END,
        period_source_timezone="UTC",
        consumer_status="unavailable",
        status_as_of=NOW,
        recorded_at=NOW,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )


async def test_commit_replay_restart_fanout_and_random_reemission(notification_harness):
    h = notification_harness
    _, revision, rows = await seed(h)
    store = h.reliable.store
    before_dirty = await h.outbox.read_status_dirty(account_id="acct_a")
    await store.commit_revision(revision, rows)
    assert await h.outbox.read_status_dirty(account_id="acct_a") == before_dirty
    (event,) = await h.outbox.list_events(account_id="acct_a")
    assert event.fired_at == h.reliable.clock()
    h.subscriptions.put(notification_subscription(subscriber="second"))
    await h.reliable.restart()
    assert await h.outbox.list_events(account_id="acct_a") == (event,)
    await h.drain()
    statuses = await h.outbox.list_deliveries(account_id="acct_a")
    assert len(statuses) == 2 and {item.state for item in statuses} == {"complete"}
    received = h.receiver.received
    assert len({json.loads(item.body)["notification_id"] for item in received}) == 1
    assert len({item.idempotency_key for item in received}) == 2
    for item in received:
        validate_notification_payload(json.loads(item.body))
        assert json.loads(item.body)["fired_at"] == event.fired_at.isoformat().replace(
            "+00:00", "Z"
        )
    assert (
        await h.outbox.reemit(
            account_id="acct_a", notification_id=event.notification_id, now=h.reliable.clock()
        )
        == 2
    )
    await h.drain()
    assert len({item.idempotency_key for item in h.receiver.received}) == 4
    assert {json.loads(item.body)["notification_id"] for item in h.receiver.received} == {
        event.notification_id
    }
    assert await h.outbox.list_events(account_id="other") == ()
    assert await h.outbox.list_deliveries(account_id="other") == ()
    assert await h.outbox.read_status_dirty(account_id="other") == ()


async def test_two_consumers_and_rapid_mutable_transitions_remain_replayable(notification_harness):
    h = notification_harness
    obligation, revision, _ = await seed(h)
    store = h.reliable.store
    for consumer in ("buyer", "auditor"):
        status = statement(obligation, consumer)
        assert (await store.record_consumer_status(status))[1]
        assert not (await store.record_consumer_status(status))[1]
        scope = ReportingStatusScope.for_obligation(obligation, consumer)
        issue = await store.ensure_issue_opened(
            issue_key=f"opaque|not/a/scope?token={consumer}",
            account_id="acct_a",
            consumer_id=consumer,
            observed_at=h.reliable.clock(),
            status_scope=scope,
        )
        h.reliable.clock.advance()
        await store.set_issue_state(
            issue_key=issue.issue_key,
            account_id="acct_a",
            state="acknowledged",
            at=h.reliable.clock(),
        )
        await store.set_issue_state(
            issue_key=issue.issue_key,
            account_id="acct_a",
            state="acknowledged",
            at=h.reliable.clock(),
        )
        h.reliable.clock.advance()
        await store.retire_issue(
            issue_key=issue.issue_key, account_id="acct_a", at=h.reliable.clock()
        )
    for readable in (False, False, True, False, True):
        h.reliable.clock.advance()
        await store.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=readable,
        )
    dirty = await h.outbox.read_status_dirty(account_id="acct_a")
    assert [record.sequence for record in dirty] == list(range(1, len(dirty) + 1))
    readability = [record for record in dirty if record.reason == "readability"]
    assert [(record.before.readable, record.after.readable) for record in readability] == [
        (True, False),
        (False, True),
        (True, False),
        (False, True),
    ]
    assert [record.cause_generation for record in readability] == [1, 2, 3, 4]
    for consumer in ("buyer", "auditor"):
        issues = [
            record
            for record in dirty
            if record.reason == "issue" and record.scope.consumer_id == consumer
        ]
        assert [record.after.issue_state for record in issues] == [
            "open",
            "acknowledged",
            "resolved",
        ]
        assert [record.before.issue_state if record.before else None for record in issues] == [
            None,
            "open",
            "acknowledged",
        ]
        assert [record.cause_generation for record in issues] == [1, 2, 3]
        assert len({record.cause_id for record in issues}) == 1
        assert {record.scope.reporting_obligation_id for record in issues} == {
            obligation.reporting_obligation_id
        }
    assert len(await h.outbox.list_events(account_id="acct_a")) == 1
    assert "opaque" not in json.dumps([asdict(record) for record in dirty], default=str)
    await h.reliable.restart()
    assert await h.outbox.read_status_dirty(account_id="acct_a") == dirty
    assert await h.outbox.advance_status_checkpoint(
        account_id="acct_a", projector_id="future-B", expected=0, through=dirty[3].sequence
    )
    assert not await h.outbox.advance_status_checkpoint(
        account_id="acct_a", projector_id="future-B", expected=0, through=dirty[-1].sequence
    )
    assert await h.outbox.status_checkpoint(account_id="other", projector_id="future-B") == 0
    with pytest.raises(ValueError):
        await h.outbox.advance_status_checkpoint(
            account_id="other", projector_id="future-B", expected=0, through=1
        )


async def test_issue_scope_is_optional_and_cannot_cross_consumer_or_account(notification_harness):
    h = notification_harness
    obligation, _, _ = await seed(h)
    with pytest.raises(ReportingNotificationError, match="invalid_status_scope"):
        await h.reliable.store.ensure_issue_opened(
            issue_key="opaque",
            account_id="acct_a",
            consumer_id="buyer",
            observed_at=NOW,
            status_scope=ReportingStatusScope.for_obligation(obligation, "auditor"),
        )
    assert await h.reliable.store.get_issue(issue_key="opaque", account_id="acct_a") is None
    await h.reliable.store.ensure_issue_opened(
        issue_key="legacy:unknown/scope", account_id="acct_a", consumer_id="buyer", observed_at=NOW
    )
    latest = (await h.outbox.read_status_dirty(account_id="acct_a"))[-1]
    assert latest.scope == ReportingStatusScope("acct_a", consumer_id="buyer")


async def test_verified_ready_identity_includes_consumer_namespace(notification_harness):
    h = notification_harness
    h.subscriptions.put(notification_subscription(subscriber="audit", principal="auditor"))
    for consumer in ("buyer", "auditor"):
        s = await scenario(h.reliable.store, consumer_id=consumer)
        await h.reliable.store.commit_materialization(s.outcome)
        assert not (await h.reliable.store.commit_materialization(s.outcome))[1]
    events = await h.outbox.list_events(account_id="acct_a")
    ready = [event for event in events if event.notification_type == "reporting.delivery_ready"]
    assert len(ready) == 2 and len({event.notification_id for event in ready}) == 2
    assert {event.cause.consumer_id for event in ready} == {"buyer", "auditor"}
    assert len({event.causal_key for event in ready}) == 2
    await h.drain()
    deliveries = [
        json.loads(item.body)
        for item in h.receiver.received
        if json.loads(item.body)["notification_type"] == "reporting.delivery_ready"
    ]
    assert len(deliveries) == 2
    expected = {event.notification_id: event.cause.consumer_id for event in ready}
    actual_principals = {"buyer": "buyer", "audit": "auditor"}
    assert all(
        actual_principals[item["subscriber_id"]] == expected[item["notification_id"]]
        for item in deliveries
    )


async def test_core_explicit_ready_subscription_never_enqueues_ready(notification_harness):
    h = notification_harness
    h.subscriptions.values.clear()
    h.subscriptions.put(
        notification_subscription(events=("reporting.delivery_ready", "reporting.status_changed"))
    )
    obligation, revision, rows = await seed(h)
    # Neither a profile label nor a subscriber's capability request creates
    # a frozen Managed destination/obligation/materialization binding.
    assert obligation.reporting_profile == "paid_media_delivery"
    await h.reliable.store.commit_revision(revision, rows)
    await h.drain()
    assert h.receiver.received == []
    assert {
        event.notification_type for event in await h.outbox.list_events(account_id="acct_a")
    } == {"reporting.ledger_changed"}
    assert "notifications" not in inspect.signature(ReportingProducer).parameters
    assert not hasattr(InMemoryReportingLedgerStore(), "commit_materialization")


async def test_adjustments_use_exclusive_allowlisted_payload(notification_harness):
    h = notification_harness
    _, revision, _ = await seed(h, official=True)
    adjustment = ReportingAdjustmentRecord(
        "adjustment-1",
        "acct_a",
        revision.reporting_revision_id,
        "source_correction",
        START,
        END,
        (("impressions", "-1"),),
        NOW,
        NOW,
        reason_detail="provider token=NEVER_COPY https://secret.example.test/path?key=SECRET",
    )
    await h.reliable.store.commit_adjustment(adjustment)
    await h.reliable.store.commit_adjustment(adjustment)
    await h.drain()
    values = [json.loads(item.body) for item in h.receiver.received]
    assert len(values) == 2
    changed = next(item for item in values if item["change_kind"] == "adjustment_published")
    assert not (
        {"finality", "reporting_revision_id", "supersedes_reporting_revision_id"} & changed.keys()
    )
    assert "NEVER_COPY" not in json.dumps(values)
    assert "SECRET" not in json.dumps(values)
    bad = {**changed, "reporting_revision_id": "revision-extra"}
    with pytest.raises(ReportingNotificationError):
        validate_notification_payload(bad)


async def test_concurrent_identical_commit_and_conflict_have_no_ghost(notification_harness):
    h = notification_harness
    config = configuration()
    await h.reliable.store.put_configuration(config)
    obligation = await h.reliable.store.commit_obligation(obligation_for(config))
    revision, rows = revision_for(obligation)
    result = await asyncio.gather(
        *(h.reliable.store.commit_revision(revision, rows) for _ in range(5))
    )
    assert result == [revision] * 5
    conflicting = replace(revision, revision_content_sha256="f" * 64)
    result = await asyncio.gather(
        h.reliable.store.commit_revision(revision, rows),
        h.reliable.store.commit_revision(conflicting, rows),
        return_exceptions=True,
    )
    assert isinstance(result[1], LedgerConflictError)
    assert len(await h.outbox.list_events(account_id="acct_a")) == 1
    assert (
        len(
            [
                d
                for d in await h.outbox.read_status_dirty(account_id="acct_a")
                if d.reason == "revision"
            ]
        )
        == 1
    )


async def test_concurrent_fanout_and_http_claims_have_one_active_attempt(notification_harness):
    h = notification_harness
    await seed(h)
    assert (
        sum(await asyncio.gather(*(h.worker().expand_one(account_id="acct_a") for _ in range(5))))
        == 1
    )
    assert len(await h.outbox.list_deliveries(account_id="acct_a")) == 1
    barrier = Barrier()
    h.reliable.failures.at("http.accepted", barrier)
    first = asyncio.create_task(h.worker().deliver_one(account_id="acct_a"))
    try:
        await barrier.wait()
        assert not await h.worker().deliver_one(account_id="acct_a")
        assert len(h.receiver.received) == 1
    finally:
        barrier.release()
        await first


async def test_expired_lease_reclaims_and_all_stale_updates_are_fenced(notification_harness):
    h = notification_harness
    await seed(h)
    await h.worker().expand_one(account_id="acct_a")
    original = await h.outbox.claim_delivery(
        account_id="acct_a", now=h.reliable.clock(), lease_seconds=60
    )
    assert original is not None
    h.reliable.clock.advance(timedelta(seconds=61))
    reclaimed = await h.outbox.claim_delivery(
        account_id="acct_a", now=h.reliable.clock(), lease_seconds=60
    )
    assert reclaimed is not None and original.token != reclaimed.token
    assert original.delivery == reclaimed.delivery
    for state in ("complete", "pending", "quarantined", "suppressed"):
        assert not await h.outbox.finish_delivery(original, now=h.reliable.clock(), state=state)
    assert await h.outbox.finish_delivery(reclaimed, now=h.reliable.clock(), state="complete")
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].state == "complete"


@pytest.mark.parametrize("stage", ["dns", "signing"])
async def test_expiry_during_preparation_is_fenced_before_http(
    notification_harness, monkeypatch, stage
):
    import socket

    from adcp.webhook_auth import JwkSignerStrategy

    h = notification_harness
    await seed(h)
    await h.worker().expand_one(account_id="acct_a")
    (original,) = await h.outbox.list_deliveries(account_id="acct_a")
    target, name = (
        (socket, "getaddrinfo") if stage == "dns" else (JwkSignerStrategy, "build_auth_headers")
    )
    prepare = getattr(target, name)
    expired = False

    def expire_after_preparation(*args, **kwargs):
        nonlocal expired
        result = prepare(*args, **kwargs)
        if not expired:
            expired = True
            h.reliable.clock.advance(timedelta(seconds=61))
        return result

    monkeypatch.setattr(target, name, expire_after_preparation)
    assert await h.worker().deliver_one(account_id="acct_a")
    assert expired and not h.receiver.connections and not h.receiver.received
    (retained,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert retained.state == "leased" and retained.delivery == original.delivery
    await h.drain()
    (completed,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert completed.state == "complete" and completed.delivery == original.delivery
    assert len(h.receiver.received) == 1


async def test_reclaimed_expansion_rejects_stale_snapshot_and_all_finishes(notification_harness):
    h = notification_harness
    await seed(h)
    (event,) = await h.outbox.list_events(account_id="acct_a")
    stale = await h.outbox.claim_expansion(
        account_id="acct_a", now=h.reliable.clock(), lease_seconds=60
    )
    assert stale is not None
    h.reliable.clock.advance(timedelta(seconds=61))
    current = await h.outbox.claim_expansion(
        account_id="acct_a", now=h.reliable.clock(), lease_seconds=60
    )
    assert current is not None and current.token != stale.token
    old = h.cipher.prepare(event, notification_subscription(subscriber="old"), 1)
    assert not await h.outbox.complete_expansion(stale, (old,), now=h.reliable.clock())
    for state in ("complete", "pending", "quarantined", "suppressed"):
        assert not await h.outbox.finish_expansion(stale, now=h.reliable.clock(), state=state)
    assert await h.outbox.list_deliveries(account_id="acct_a") == ()
    new = h.cipher.prepare(event, notification_subscription(subscriber="new"), 1)
    assert await h.outbox.complete_expansion(current, (new,), now=h.reliable.clock())
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert row.delivery == new


async def test_acceptance_before_ack_retries_exact_body_key_with_rotated_signature(
    notification_harness,
):
    h = notification_harness
    await seed(h)
    await h.worker().expand_one(account_id="acct_a")
    h.reliable.failures.at("http.accepted", SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        await h.worker().deliver_one(account_id="acct_a")
    first = h.receiver.received[0]
    h.reliable.clock.advance(timedelta(seconds=61))
    h.signing.generation = 2
    await h.reliable.restart()
    await h.drain()
    second = h.receiver.received[1]
    assert first.body == second.body and first.idempotency_key == second.idempotency_key
    assert first.headers["signature"] != second.headers["signature"]
    assert (
        "key-1" in first.headers["signature-input"] and "key-2" in second.headers["signature-input"]
    )
    assert len(h.signing.calls) == 2
    assert await h.reliable.receiver.read("acct_a", first.idempotency_key) == first.body
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].state == "complete"


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503, 599])
async def test_retryable_http_and_retry_state_survive_restart(notification_harness, status):
    h = notification_harness
    await seed(h)
    h.receiver.responses["buyer"].append(status)
    await h.drain()
    first = h.receiver.received[0]
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].error_code == "retryable_http"
    await h.reliable.restart()
    h.reliable.clock.advance(timedelta(seconds=6))
    await h.drain()
    assert len(h.receiver.received) == 2
    assert first.body == h.receiver.received[1].body


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 410, 422])
async def test_permanent_http_is_quarantined_and_provider_text_is_never_retained(
    notification_harness, status
):
    h = notification_harness
    await seed(h)
    h.receiver.responses["buyer"].append(status)
    await h.drain()
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert row.state == "quarantined" and row.error_code == "permanent_http"
    assert "DO_NOT_PERSIST" not in repr(row)
    assert "URL_SECRET" not in repr(row)
    assert len(h.receiver.received) == 1


@pytest.mark.parametrize(
    "change",
    ["removed", "url", "credentials", "principal", "inactive", "authz", "proof", "events", "scope"],
)
async def test_removed_or_replaced_subscription_is_suppressed_without_external_leakage(
    notification_harness, change
):
    h = notification_harness
    await seed(h)
    await h.worker().expand_one(account_id="acct_a")
    original = h.subscriptions.values[("acct_a", "buyer")]
    replacements = {
        "url": dict(url="https://replacement.example.test/new?token=NEW_SECRET"),
        "credentials": dict(
            signing_scope_id=None,
            authentication=ReportingLegacyAuthentication("Bearer", "NEW_SECRET"),
        ),
        "principal": dict(principal_id="other"),
        "inactive": dict(active=False),
        "authz": dict(authorized=False),
        "proof": dict(proof_valid=False),
        "events": dict(event_types=("reporting.delivery_ready",)),
        "scope": dict(signing_scope_id="scope-two"),
    }
    if change == "removed":
        del h.subscriptions.values[("acct_a", "buyer")]
    else:
        h.subscriptions.put(replace(original, **replacements[change]))
    await h.worker().deliver_one(account_id="acct_a")
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert row.state == "suppressed"
    assert not h.signing.calls and not h.receiver.connections and not h.receiver.dns_calls
    assert h.cipher.open(row.delivery).subscription == original


async def test_subscriber_failure_does_not_block_an_independent_subscriber(notification_harness):
    h = notification_harness
    await seed(h)
    h.subscriptions.put(notification_subscription(subscriber="healthy"))
    h.receiver.responses["buyer"].append(503)
    await h.drain()
    rows = await h.outbox.list_deliveries(account_id="acct_a")
    assert {row.delivery.binding.subscriber_id: row.state for row in rows} == {
        "buyer": "pending",
        "healthy": "complete",
    }


@pytest.mark.parametrize(
    "error,expected",
    [
        (ScopePermanentlyUnknown("secret"), "quarantined"),
        (ScopeTransientlyUnavailable("secret"), "pending"),
        (OSError("provider secret"), "pending"),
    ],
)
async def test_signing_failures_are_closed_classifications(notification_harness, error, expected):
    h = notification_harness
    await seed(h)
    h.reliable.failures.at("signing.resolve", error)
    await h.drain()
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    assert row.state == expected
    assert "secret" not in repr(row)
    assert not h.receiver.connections


@pytest.mark.parametrize("fake_type", ["object", "subclass"])
async def test_fake_or_subclass_sender_never_crosses_owned_transport_boundary(
    notification_harness, fake_type
):
    h = notification_harness
    await seed(h)
    calls = []

    class MaliciousSender(WebhookSender):
        async def send_prepared(self, prepared):
            calls.append(prepared)
            raise AssertionError("secret leaked")

    fake = object.__new__(MaliciousSender) if fake_type == "subclass" else type("Fake", (), {})()
    fake._owns_client = True
    fake._allow_private_destinations = False
    if fake_type == "object":
        fake.signs_with_rfc9421 = True

    async def malicious(**kwargs):
        return fake

    h.signing.resolve = malicious
    await h.drain()
    assert not calls and not h.receiver.dns_calls and not h.receiver.connections
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].state == "quarantined"


async def test_envelope_key_rotation_preserves_old_prepared_bytes(notification_harness):
    h = notification_harness
    await seed(h)
    await h.worker().expand_one(account_id="acct_a")
    (row,) = await h.outbox.list_deliveries(account_id="acct_a")
    previous = h.cipher.open(row.delivery).prepared
    h.cipher = ReportingEnvelopeCipher(b"n" * 32, key_version="v2", previous_keys={"v1": b"e" * 32})
    assert h.cipher.open(row.delivery).prepared == previous
    await h.drain()
    assert h.receiver.received[0].body == previous.body


@pytest.mark.parametrize(
    "issue_ids", [None, [], [""], ["ok"] * 2, [f"issue-{i}" for i in range(17)]]
)
def test_full_rc3_status_conditional_and_bounded_issue_ids(issue_ids):
    value = {
        "idempotency_key": "0" * 32,
        "notification_id": "status-1",
        "subscriber_id": "buyer",
        "account_id": "acct_a",
        "notification_type": "reporting.status_changed",
        "fired_at": NOW.isoformat(),
        "delivery_config_id": "daily",
        "delivery_config_version": 1,
        "feed_purpose": "analytics",
        "health": "delayed",
    }
    if issue_ids is not None:
        value["issue_ids"] = issue_ids
    with pytest.raises(ReportingNotificationError):
        validate_notification_payload(value)
    value["issue_ids"] = ["issue-1"]
    validate_notification_payload(value)


async def test_claim_crashes_do_not_exhaust_delivery_retry_budget(notification_harness):
    h = notification_harness
    await seed(h)
    await h.worker().expand_one(account_id="acct_a")
    for _ in range(12):
        lease = await h.outbox.claim_delivery(
            account_id="acct_a", now=h.reliable.clock(), lease_seconds=1
        )
        assert lease is not None
        h.reliable.clock.advance(timedelta(seconds=2))
    assert h.receiver.received == []
    await h.reliable.restart()
    await h.drain()
    assert len(h.receiver.received) == 1
    assert (await h.outbox.list_deliveries(account_id="acct_a"))[0].state == "complete"


async def test_empty_fanout_completes_but_transient_resolution_does_not(notification_harness):
    h = notification_harness
    await seed(h)
    h.subscriptions.values.clear()
    h.reliable.failures.at("subscriptions.list.before", OSError("provider token=SECRET"))
    assert await h.worker().expand_one(account_id="acct_a")
    assert not await h.worker().expand_one(account_id="acct_a")
    h.reliable.clock.advance(timedelta(seconds=6))
    assert await h.worker().expand_one(account_id="acct_a")
    h.subscriptions.put(notification_subscription())
    h.reliable.clock.advance(timedelta(hours=1))
    assert not await h.worker().expand_one(account_id="acct_a")
    assert len(h.subscriptions.lists) == 2
    assert await h.outbox.list_deliveries(account_id="acct_a") == ()


async def test_duplicate_subscriber_snapshot_is_rejected_atomically(notification_harness):
    h = notification_harness
    await seed(h)
    subscription = notification_subscription()

    async def duplicate(**kwargs):
        return (subscription, replace(subscription, configuration_revision="different"))

    h.subscriptions.list_active = duplicate
    assert await h.worker().expand_one(account_id="acct_a")
    assert not await h.worker().expand_one(account_id="acct_a")
    assert await h.outbox.list_deliveries(account_id="acct_a") == ()


async def test_one_blocked_subscriber_does_not_hold_other_http_work(notification_harness):
    h = notification_harness
    await seed(h)
    h.subscriptions.put(notification_subscription(subscriber="second"))
    await h.worker().expand_one(account_id="acct_a")
    barrier = Barrier()
    h.reliable.failures.at("http.accepted", barrier)
    first = asyncio.create_task(h.worker().deliver_one(account_id="acct_a"))
    try:
        await barrier.wait()
        assert await h.worker().deliver_one(account_id="acct_a")
        assert len(h.receiver.received) == 2
        assert {item.subscriber_id for item in h.receiver.received} == {"buyer", "second"}
    finally:
        barrier.release()
        await first
