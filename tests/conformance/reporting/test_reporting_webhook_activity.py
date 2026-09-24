"""Shared memory/PostgreSQL vectors at the reporting worker's actual HTTP seam."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from adcp.decisioning.accounts import ResolveContext
from adcp.decisioning.context import AuthInfo
from adcp.reporting.outbox import (
    ActivityOutcome,
    ActivityRequest,
    ReportingActivityProjector,
    ReportingNotificationError,
    ReportingNotificationWorker,
)
from adcp.webhook_sender import PreparedWebhookAttemptExpiredError, WebhookSender

from ._reliable_support import Barrier, SimulatedCrash, notification_subscription
from .test_reporting_notification_outbox import seed


def worker_for(h, outbox=None, **options):
    outbox = outbox or h.outbox
    return ReportingNotificationWorker(
        outbox=outbox,
        activity=outbox,
        subscriptions=h.subscriptions,
        signing=h.signing,
        cipher=h.cipher,
        clock=h.reliable.clock,
        **options,
    )


async def prepare(h, *, account="acct_a"):
    await seed(h, account=account)
    outbox = h.outbox
    worker = worker_for(h, outbox)
    expanded_1 = await worker.expand_one(account_id=account)
    assert expanded_1
    return outbox, worker


async def reserve(h, outbox, *, account="acct_a", lease_seconds=60):
    lease = await outbox.claim_delivery(
        account_id=account, now=h.reliable.clock(), lease_seconds=lease_seconds
    )
    assert lease is not None
    opened = h.cipher.open(lease.delivery)
    attempt = await outbox.reserve_attempt(
        lease,
        request=ActivityRequest(opened.subscription.url, len(opened.prepared.body)),
        now=h.reliable.clock(),
    )
    assert attempt is not None
    return lease, attempt


async def rows(outbox, *, account="acct_a", consumer="buyer", limit=50):
    return [
        item.to_wire()
        for item in await outbox.list_activity(
            account_id=account, consumer_id=consumer, limit=limit
        )
    ]


@pytest.mark.parametrize(
    "code,parent",
    [
        (200, "complete"),
        (299, "complete"),
        (302, "quarantined"),
        (400, "quarantined"),
        (401, "quarantined"),
        (408, "pending"),
        (425, "pending"),
        (429, "pending"),
        (500, "pending"),
        (503, "pending"),
        (599, "pending"),
    ],
)
async def test_http_outcomes_are_independent_of_parent_retry_policy(
    notification_harness, code, parent
):
    h = notification_harness
    outbox, worker = await prepare(h)
    h.receiver.responses["buyer"].append(code)
    delivered_1 = await worker.deliver_one(account_id="acct_a")
    assert delivered_1
    (row,) = await rows(outbox)
    assert row["status"] == ("success" if 200 <= code < 300 else "failed")
    assert row["attempt"] == 1 and row["http_status_code"] == code
    assert row["completed_at"] is not None and row["response_time_ms"] >= 0
    assert row["payload_size_bytes"] == len(h.receiver.received[0].body)
    assert (await outbox.list_deliveries(account_id="acct_a"))[0].state == parent
    assert row["error_message"] == (None if code < 300 else "HTTP non-success response")
    assert "sequence_number" not in row
    assert not {"lease_token", "reservation_token", "state", "principal_id"} & row.keys()
    h.reliable.clock.advance(timedelta(seconds=10))
    if parent != "pending":
        delivered_3 = await worker.deliver_one(account_id="acct_a")
        assert not delivered_3
        assert len(await rows(outbox)) == 1


@pytest.mark.parametrize(
    "error,status",
    [
        (httpx.ReadTimeout("URL_SECRET"), "timeout"),
        (httpx.ConnectTimeout("URL_SECRET"), "timeout"),
        (TimeoutError("URL_SECRET"), "timeout"),
        (httpx.ConnectError("TLS_SECRET"), "connection_error"),
        (ConnectionRefusedError("SOCKET_SECRET"), "connection_error"),
        (socket.gaierror("DNS_SECRET"), "connection_error"),
    ],
)
async def test_post_reservation_transport_classification(
    notification_harness, error, status, caplog
):
    h = notification_harness
    outbox, worker = await prepare(h)
    caplog.set_level(logging.DEBUG)
    h.receiver.responses["buyer"].append(error)
    await worker.deliver_one(account_id="acct_a")
    (row,) = await rows(outbox)
    assert row["status"] == status and row["completed_at"] is not None
    assert row["http_status_code"] is None and row["response_time_ms"] is None
    assert "SECRET" not in json.dumps(row) + caplog.text


async def test_response_latency_uses_monotonic_time(notification_harness, monkeypatch):
    from adcp.reporting.outbox import worker as worker_module

    h = notification_harness
    outbox, worker = await prepare(h)
    ticks = iter((1_000_000_000, 1_042_000_000))
    monkeypatch.setattr(worker_module.time, "monotonic_ns", lambda: next(ticks))
    await worker.deliver_one(account_id="acct_a")
    (row,) = await rows(outbox)
    assert row["response_time_ms"] == 42


@pytest.mark.parametrize("phase", ["dns_error", "ssrf", "dns_expired", "signing_expired"])
async def test_preflight_never_reserves_and_dns_signing_can_outlive_lease(
    notification_harness, monkeypatch, phase
):
    h = notification_harness
    outbox, worker = await prepare(h)
    original = socket.getaddrinfo

    def dns(host, *args, **kwargs):
        if host == "receiver.example.test":
            if phase == "dns_error":
                raise socket.gaierror("DNS_SECRET")
            if phase == "dns_expired":
                h.reliable.clock.advance(timedelta(seconds=61))
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    if phase == "ssrf":
        h.receiver.dns_addresses["receiver.example.test"] = ["127.0.0.1"]
    if phase == "signing_expired":
        resolve = h.signing.resolve

        async def delayed(**kwargs):
            material = await resolve(**kwargs)
            h.reliable.clock.advance(timedelta(seconds=61))
            return material

        monkeypatch.setattr(h.signing, "resolve", delayed)
    await worker.deliver_one(account_id="acct_a")
    assert await rows(outbox) == []
    assert h.receiver.connections == []


async def test_callback_is_single_use_and_claims_do_not_count_as_http(
    notification_harness, monkeypatch
):
    h = notification_harness
    outbox, worker = await prepare(h)
    for _ in range(3):
        claimed_delivery_1 = await outbox.claim_delivery(
            account_id="acct_a", now=h.reliable.clock(), lease_seconds=1
        )
        assert claimed_delivery_1
        h.reliable.clock.advance(timedelta(seconds=2))

    async def twice(sender, prepared, *, before_attempt=None):
        assert before_attempt is not None
        attempt_prepared_1 = await before_attempt()
        assert attempt_prepared_1
        with pytest.raises(PreparedWebhookAttemptExpiredError):
            await before_attempt()
        raise SimulatedCrash

    monkeypatch.setattr(WebhookSender, "send_prepared", twice)
    with pytest.raises(SimulatedCrash):
        await worker.deliver_one(account_id="acct_a")
    (row,) = await rows(outbox)
    assert row["attempt"] == 1 and row["status"] == "pending"
    assert all(
        row[key] is None
        for key in ("completed_at", "http_status_code", "response_time_ms", "error_message")
    )
    assert h.receiver.connections == []


@pytest.mark.parametrize("window", ["reservation_to_io", "http_to_activity_ack"])
async def test_irreducible_crash_windows_retain_pending_and_retry_exact_bytes(
    notification_harness, monkeypatch, window
):
    h = notification_harness
    outbox, worker = await prepare(h)
    method = "reserve_attempt" if window == "reservation_to_io" else "complete_attempt"
    original = getattr(outbox, method)

    async def crash(*args, **kwargs):
        if window == "reservation_to_io":
            await original(*args, **kwargs)
        raise SimulatedCrash

    monkeypatch.setattr(outbox, method, crash)
    with pytest.raises(SimulatedCrash):
        await worker.deliver_one(account_id="acct_a")
    assert (await rows(outbox))[0]["status"] == "pending"
    assert (await outbox.list_deliveries(account_id="acct_a"))[0].state == "leased"
    assert len(h.receiver.received) == (0 if window == "reservation_to_io" else 1)
    monkeypatch.setattr(outbox, method, original)
    h.reliable.clock.advance(timedelta(seconds=61))
    await worker_for(h, outbox).deliver_one(account_id="acct_a")
    result = await rows(outbox)
    assert [(r["attempt"], r["status"]) for r in result] == [(2, "success"), (1, "pending")]
    assert len({r["idempotency_key"] for r in result}) == 1
    assert len({r["notification_id"] for r in result}) == 1
    if window == "http_to_activity_ack":
        assert h.receiver.received[0].body == h.receiver.received[1].body
    assert (
        h.cipher.open((await outbox.list_deliveries(account_id="acct_a"))[0].delivery).prepared.body
        == h.receiver.received[-1].body
    )


@pytest.mark.parametrize("phase", ["reserve", "complete", "unconfirmed"])
async def test_activity_database_failure_never_sends_or_acks_past_unknown_commit(
    notification_harness, monkeypatch, phase
):
    h = notification_harness
    outbox, worker = await prepare(h)

    async def fail(*args, **kwargs):
        if phase == "unconfirmed":
            return False
        raise ReportingNotificationError("activity_store_unavailable")

    monkeypatch.setattr(
        outbox, "reserve_attempt" if phase == "reserve" else "complete_attempt", fail
    )
    with pytest.raises(ReportingNotificationError):
        await worker.deliver_one(account_id="acct_a")
    assert (await outbox.list_deliveries(account_id="acct_a"))[0].state == "leased"
    assert len(h.receiver.received) == (0 if phase == "reserve" else 1)
    if phase != "reserve":
        assert (await rows(outbox))[0]["status"] == "pending"


@pytest.mark.parametrize("deadline", [False, True])
async def test_cancellation_and_worker_deadline_leave_unknown_pending(
    notification_harness, deadline
):
    h = notification_harness
    outbox, _ = await prepare(h)
    worker = worker_for(h, outbox, lease_seconds=1 if deadline else 60)
    barrier = Barrier()
    h.reliable.failures.at("http.before", barrier)
    task = asyncio.create_task(worker.deliver_one(account_id="acct_a"))
    await barrier.wait()
    if deadline:
        task_result_1 = await asyncio.wait_for(task, timeout=3)
        assert task_result_1
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await rows(outbox))[0]["status"] == "pending"
    assert (await outbox.list_deliveries(account_id="acct_a"))[0].state == "leased"


async def test_late_completion_only_updates_its_token_after_parent_reclaim(notification_harness):
    h = notification_harness
    outbox, _ = await prepare(h)
    old_lease, old = await reserve(h, outbox)
    h.reliable.clock.advance(timedelta(seconds=61))
    current_lease, current = await reserve(h, outbox)
    assert current.attempt == 2
    completed_attempt_1 = await outbox.complete_attempt(
        old, outcome=ActivityOutcome("success", 200, 61_000), now=h.reliable.clock()
    )
    assert completed_attempt_1
    completed_attempt_2 = await outbox.complete_attempt(
        old, outcome=ActivityOutcome("failed", 500, 1), now=h.reliable.clock()
    )
    assert not completed_attempt_2
    completed_attempt_3 = await outbox.complete_attempt(
        replace(current, reservation_token=old.reservation_token),
        outcome=ActivityOutcome("timeout"),
        now=h.reliable.clock(),
    )
    assert not completed_attempt_3
    finished_delivery_1 = await outbox.finish_delivery(
        old_lease, state="complete", now=h.reliable.clock()
    )
    assert not finished_delivery_1
    assert await outbox.delivery_lease_current(current_lease, now=h.reliable.clock())
    assert [(r["attempt"], r["status"]) for r in await rows(outbox)] == [
        (2, "pending"),
        (1, "success"),
    ]


async def test_exact_completed_at_retention_floor_never_deletes_pending(notification_harness):
    h = notification_harness
    outbox, _ = await prepare(h)
    _, pending = await reserve(h, outbox)
    h.reliable.clock.advance(timedelta(days=5))
    _, completed = await reserve(h, outbox)
    h.reliable.clock.advance(timedelta(seconds=1))
    completed_attempt_4 = await outbox.complete_attempt(
        completed, outcome=ActivityOutcome("timeout"), now=h.reliable.clock()
    )
    assert completed_attempt_4
    for days in (0, 29, True):
        with pytest.raises(ReportingNotificationError, match="30_days"):
            await outbox.purge_activity(
                account_id="acct_a",
                consumer_id="buyer",
                now=h.reliable.clock(),
                retention_days=days,
            )
    h.reliable.clock.advance(timedelta(days=30))
    purged_count_1 = await outbox.purge_activity(
        account_id="acct_a", consumer_id="buyer", now=h.reliable.clock()
    )
    assert purged_count_1 == 0
    h.reliable.clock.advance(timedelta(microseconds=1))
    purged_count_2 = await outbox.purge_activity(
        account_id="acct_a", consumer_id="other", now=h.reliable.clock()
    )
    assert purged_count_2 == 0
    purged_count_3 = await outbox.purge_activity(
        account_id="other", consumer_id="buyer", now=h.reliable.clock()
    )
    assert purged_count_3 == 0
    purged_count_4 = await outbox.purge_activity(
        account_id="acct_a", consumer_id="buyer", now=h.reliable.clock()
    )
    assert purged_count_4 == 1
    assert (await outbox.list_activity(account_id="acct_a", consumer_id="buyer")) == (pending,)


async def test_purged_terminal_history_cannot_reset_a_retryable_delivery_counter(
    notification_harness,
):
    h = notification_harness
    outbox, worker = await prepare(h)
    h.receiver.responses["buyer"].append(500)
    await worker.deliver_one(account_id="acct_a")
    first = (await rows(outbox))[0]
    h.reliable.clock.advance(timedelta(days=31))
    purged_count_5 = await outbox.purge_activity(
        account_id="acct_a", consumer_id="buyer", now=h.reliable.clock()
    )
    assert purged_count_5 == 1
    assert await rows(outbox) == []
    await worker_for(h, outbox).deliver_one(account_id="acct_a")
    (second,) = await rows(outbox)
    assert second["attempt"] == 2 and second["status"] == "success"
    assert second["idempotency_key"] == first["idempotency_key"]


async def test_wire_serialization_omits_absent_optional_ids_and_preserves_pending_nulls(
    notification_harness,
):
    h = notification_harness
    outbox, _ = await prepare(h)
    _, attempt = await reserve(h, outbox)
    projected = replace(attempt, binding=replace(attempt.binding, notification_id="")).to_wire()
    assert "notification_id" not in projected and "sequence_number" not in projected
    assert all(
        projected[key] is None
        for key in (
            "completed_at",
            "http_status_code",
            "response_time_ms",
            "error_message",
        )
    )


async def test_canonical_url_bound_is_registration_closed_on_both_stores(notification_harness):
    h = notification_harness
    # A raw URL inside the bound whose percent-encoded canonical form is not
    # must never become a durable configuration: it would otherwise quarantine
    # every expansion and record no activity at all.
    with pytest.raises(ReportingNotificationError, match="invalid_configuration"):
        notification_subscription(url="https://receiver.example.test/PATH_SECRET" + " " * 8000)
    host = "https://receiver.example.test/"
    h.subscriptions.put(notification_subscription(url=host + "a" * (8192 - len(host))))
    outbox, worker = await prepare(h)
    delivered_2 = await worker.deliver_one(account_id="acct_a")
    assert delivered_2
    (row,) = await rows(outbox)
    # The rejected registration left the store working, and the maximal
    # in-bound canonical URL delivers and projects its sanitized form.
    assert (row["status"], row["attempt"]) == ("success", 1)
    assert row["url"] == host + "redacted" and len(row["url"]) <= 8192
    assert (await outbox.list_deliveries(account_id="acct_a"))[0].state == "complete"
    assert "PATH_SECRET" not in json.dumps(row)


@pytest.mark.parametrize("limit", [0, 201, -1, True, 1.5, "2"])
async def test_activity_limit_is_validated_before_store_access(notification_harness, limit):
    with pytest.raises(ReportingNotificationError, match="1_to_200"):
        await notification_harness.outbox.list_activity(
            account_id="acct_a", consumer_id="buyer", limit=limit
        )


async def test_account_principal_write_read_isolation_and_deterministic_ties(notification_harness):
    h = notification_harness
    h.subscriptions.put(notification_subscription(subscriber="audit", principal="auditor"))
    h.subscriptions.put(notification_subscription(account="acct_b", principal="other"))
    outbox, worker = await prepare(h)
    await seed(h, account="acct_b")
    await worker.expand_one(account_id="acct_b")
    for account in ("acct_a", "acct_b"):
        while await worker.deliver_one(account_id=account):
            pass
    own = await outbox.list_activity(account_id="acct_a", consumer_id="buyer")
    foreign = await outbox.list_activity(account_id="acct_a", consumer_id="auditor")
    assert len(own) == len(foreign) == 1
    assert own[0].binding.notification_id == foreign[0].binding.notification_id
    assert await rows(outbox, account="acct_b", consumer="buyer") == []
    assert await rows(outbox, account="acct_a", consumer="other") == []
    # Both predicates must hold for completion, even with another attempt's token.
    forged = replace(own[0], binding=replace(own[0].binding, principal_id="auditor"))
    completed_attempt_5 = await outbox.complete_attempt(
        forged, outcome=ActivityOutcome("timeout"), now=h.reliable.clock()
    )
    assert not completed_attempt_5
    # Re-emissions get new keys at the same timestamp; limiting is per principal.
    notification = own[0].binding.notification_id
    for _ in range(4):
        await outbox.reemit(
            account_id="acct_a", notification_id=notification, now=h.reliable.clock()
        )
        await worker.expand_one(account_id="acct_a")
        while await worker.deliver_one(account_id="acct_a"):
            pass
    all_rows = await rows(outbox, limit=200)
    assert len(all_rows) == 5
    assert all_rows == sorted(
        all_rows,
        key=lambda r: (
            r["fired_at"],
            r["notification_id"],
            r["idempotency_key"],
            r["subscriber_id"],
            r["attempt"],
        ),
        reverse=True,
    )
    assert await rows(outbox, limit=2) == all_rows[:2]
    assert await rows(outbox, limit=2) == all_rows[:2]
    projected = await ReportingActivityProjector(outbox).for_account(
        account_id="acct_a",
        context=ResolveContext(auth_info=AuthInfo(kind="bearer", principal="buyer")),
        limit=2,
    )
    assert projected == all_rows[:2]


@pytest.mark.parametrize(
    "change", ["account_id", "principal_id", "consumer_namespace", "body_sha256", "idempotency_key"]
)
async def test_reservation_cannot_rebind_authenticated_parent(notification_harness, change):
    h = notification_harness
    outbox, _ = await prepare(h)
    lease = await outbox.claim_delivery(
        account_id="acct_a", now=h.reliable.clock(), lease_seconds=60
    )
    assert lease is not None
    forged = replace(
        lease,
        delivery=replace(
            lease.delivery, binding=replace(lease.delivery.binding, **{change: "foreign"})
        ),
    )
    reserved_attempt_1 = await outbox.reserve_attempt(
        forged,
        request=ActivityRequest("https://example.test/reporting", 1),
        now=h.reliable.clock(),
    )
    assert reserved_attempt_1 is None
    assert await rows(outbox) == []


@pytest.mark.parametrize("field", ["body_sha256", "principal_id", "account_id", "lease_token"])
async def test_completion_cannot_rebind_reservation_snapshot(notification_harness, field):
    h = notification_harness
    outbox, _ = await prepare(h)
    _, attempt = await reserve(h, outbox)
    forged = (
        replace(attempt, lease_token="foreign")
        if field == "lease_token"
        else replace(attempt, binding=replace(attempt.binding, **{field: "foreign"}))
    )
    completed_attempt_6 = await outbox.complete_attempt(
        forged,
        outcome=ActivityOutcome("timeout"),
        now=h.reliable.clock(),
    )
    assert not completed_attempt_6
    assert await outbox.list_activity(account_id="acct_a", consumer_id="buyer") == (attempt,)


async def test_signed_failure_exact_retry_authorized_read_and_colliding_tenants(
    notification_harness, monkeypatch, caplog
):
    from concurrent.futures import ThreadPoolExecutor
    from uuid import UUID

    from adcp.decisioning import DecisioningPlatform
    from adcp.decisioning.handler import PlatformHandler
    from adcp.decisioning.task_registry import InMemoryTaskRegistry
    from adcp.reporting.ledger import notification_models
    from adcp.reporting.outbox import resolve_reporting_consumer, routing
    from adcp.server import ToolContext
    from adcp.signing.jwks import StaticJwksResolver
    from adcp.signing.webhook_verifier import WebhookVerifyOptions, verify_webhook_signature
    from adcp.types import ListAccountsRequest

    from ._reliable_support import notification_verification_keys

    h = notification_harness
    principal = "https://buyer.example/agent"
    other_principal = "https://other.example/agent"
    secret_url = "https://receiver.example.test/webhooks/c5b1b9b3-3e99-4da8-b930-6a9e67245553/URL_SECRET?QUERY_SECRET=1"
    h.subscriptions.put(notification_subscription(principal=principal, url=secret_url))
    h.subscriptions.put(
        notification_subscription(account="acct_b", principal=other_principal, url=secret_url)
    )
    monkeypatch.setattr(notification_models, "uuid4", lambda: UUID(int=1))
    monkeypatch.setattr(routing, "uuid4", lambda: UUID(int=2))
    outbox, worker = await prepare(h)
    await seed(h, account="acct_b")
    await worker.expand_one(account_id="acct_b")
    caplog.set_level(logging.DEBUG)
    h.receiver.responses["buyer"].extend([503, 200, 201])
    await worker.deliver_one(account_id="acct_a")
    h.reliable.clock.advance(timedelta(seconds=6))
    await worker.deliver_one(account_id="acct_a")
    await worker.deliver_one(account_id="acct_b")
    assert len(h.receiver.received) == 3
    first, second, foreign = h.receiver.received
    assert first.body == second.body and first.idempotency_key == second.idempotency_key
    assert foreign.idempotency_key == first.idempotency_key and foreign.body != first.body
    for delivery in h.receiver.received:
        assert (
            verify_webhook_signature(
                method="POST",
                url=secret_url,
                headers=delivery.headers,
                body=delivery.body,
                options=WebhookVerifyOptions(
                    jwks_resolver=StaticJwksResolver({"keys": notification_verification_keys()}),
                    clock=lambda: h.reliable.clock().timestamp(),
                ),
            ).alg
            == "ed25519"
        )

    class VisibleStore:
        def list(self, filter=None, ctx=None):
            consumer = resolve_reporting_consumer(auth_info=ctx.auth_info, agent=ctx.agent)
            return (
                [{"account_id": "acct_a", "name": "Buyer", "status": "active"}]
                if consumer == principal
                else []
            )

    class Platform(DecisioningPlatform):
        accounts = VisibleStore()

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            account_activity=ReportingActivityProjector(outbox),
        )
        response = await handler.list_accounts(
            ListAccountsRequest(include_webhook_activity=True, webhook_activity_limit=2),
            ToolContext(metadata={"adcp.auth_info": AuthInfo(kind="bearer", principal=principal)}),
        )
    activity = response["accounts"][0]["webhook_activity"]
    assert [(row["attempt"], row["status"], row["http_status_code"]) for row in activity] == [
        (2, "success", 200),
        (1, "failed", 503),
    ]
    assert all(row["notification_id"] == str(UUID(int=1)) for row in activity)
    assert await rows(outbox, account="acct_b", consumer=principal) == []
    assert await rows(outbox, account="acct_a", consumer=other_principal) == []
    diagnostics = json.dumps(response) + caplog.text
    if h.reliable.blobs.pool is not None:
        async with h.reliable.blobs.pool.connection() as conn:
            for table, expression in [
                ("reporting_webhook_attempts", "to_jsonb(t)"),
                ("reporting_notification_deliveries", "to_jsonb(t) - 'envelope'"),
            ]:
                from psycopg import sql

                record = await (
                    await conn.execute(
                        sql.SQL(
                            "SELECT {} FROM {} t WHERE account_id=%s AND principal_id=%s"
                        ).format(sql.SQL(expression), sql.Identifier(table)),
                        ("acct_a", principal),
                    )
                ).fetchall()
                diagnostics += json.dumps(record, default=str)
    for secret in (
        "URL_SECRET",
        "QUERY_SECRET",
        "c5b1b9b3-3e99-4da8-b930-6a9e67245553",
        "DO_NOT_PERSIST",
    ):
        assert secret not in diagnostics
