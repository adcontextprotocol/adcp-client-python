"""All actual production queues use durable fanout and signed at-least-once delivery."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from adcp.reporting.outbox import validate_notification_payload
from adcp.reporting.production.notifications import production_notification_workers
from adcp.signing.jwks import StaticJwksResolver
from adcp.signing.webhook_verifier import WebhookVerifyOptions, verify_webhook_signature

from ._generation_support import configuration, obligation_for, revision_for
from ._production_support import production_harness
from ._production_transport import MountedProduction
from ._projection_support import drain
from ._reliable_support import (
    DeterministicReceiverStore,
    ScriptedNotificationReceiver,
    SimulatedCrash,
    _BytesStore,
    notification_subscription,
    notification_verification_keys,
)

EVENTS = (
    "reporting.ledger_changed",
    "reporting.status_changed",
    "reporting.delivery_ready",
)


@asynccontextmanager
async def queued_production(backend, tmp_path, monkeypatch):
    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        count=0,
        notifications=True,
        notification_delivery=True,
    ) as h:
        item, support = h.item, h.production
        for subscriber, principal in (
            ("buyer", item.binding.consumer_id),
            ("second", item.binding.consumer_id),
            ("outsider", "https://buyer.example.test/other"),
        ):
            h.subscriptions.put(
                notification_subscription(
                    subscriber=subscriber,
                    principal=principal,
                    events=EVENTS,
                    url="https://receiver.example.test/reporting",
                )
            )
        h.subscriptions.put(notification_subscription(account="acct_b", events=EVENTS))
        blobs = _BytesStore(h.pool)
        await blobs.create_schema()
        receiver_store = DeterministicReceiverStore(blobs, h.notification_failures)
        receiver = ScriptedNotificationReceiver(
            SimpleNamespace(
                clock=h.clock, failures=h.notification_failures, receiver=receiver_store
            )
        )
        receiver.install(monkeypatch)
        # The harness's original Core event was already expanded at startup,
        # before registrations existed. Publish a new ordinary Core revision
        # through the real ledger transaction after registering recipients.
        core = replace(configuration(), delivery_config_id="ordinary-core")
        await h.store.put_configuration(core)
        obligation = await h.store.commit_obligation(
            replace(obligation_for(core), reporting_obligation_id="ordinary-core-obligation")
        )
        revision, rows = revision_for(obligation, suffix="ordinary-core")
        await h.store.commit_revision(revision, rows)
        await support.activate(account_id=item.config.account_id)
        production_operation_1 = await support.materializer.run_once()
        assert (production_operation_1).state == "verified"
        assert item.writer.writes == 1
        for readable in (False, True):
            await h.store.set_revision_readable(
                account_id=item.config.account_id,
                reporting_revision_id=item.revision.reporting_revision_id,
                readable=readable,
            )
        await drain(h.projection, item.config.account_id)
        if h.pool is not None:
            mount = MountedProduction(h)
            mount.authorize(item)
            async with mount.client() as client:
                for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                    _, response = await mount.call(
                        client, "get_adcp_capabilities", {}, transport=transport
                    )
                    reporting = response["media_buy"]["reporting_delivery"]
                    assert reporting["managed_delivery"] is True
                    assert response["webhook_signing"] == {
                        "supported": True,
                        "profile": "adcp/webhook-signing/v1",
                        "algorithms": ["ed25519"],
                        "legacy_hmac_fallback": False,
                        "delivery_retry_horizon_seconds": 86400,
                    }
                    assert [
                        reporting[k]
                        for k in (
                            "ledger_notification",
                            "status_notification",
                            "readiness_notification",
                        )
                    ] == list(EVENTS)
        workers = support.notification_workers
        await support.aclose()
        assert all([await w.outbox.list_events(account_id="acct_a") for w in workers])
        h.receiver, h.receiver_store = receiver, receiver_store
        h.workers = workers
        yield h


def fresh_workers(h):
    return production_notification_workers(
        h.store,
        h.projection,
        subscriptions=h.subscriptions,
        cipher=h.workers[0].cipher,
        signing=h.workers[0].signing,
    )


async def expire_queue_lease(h, worker, *, expansion=False):
    if h.pool is None:
        h.clock.advance(timedelta(seconds=61))
        return
    table = (
        "reporting_notification_expansions" if expansion else "reporting_notification_deliveries"
    )
    # The fixed SDK queue adapter maps this identifier to its actual queue.
    async with worker.outbox._connection() as c:
        await c.execute(
            f"UPDATE {table} SET lease_expires_at=clock_timestamp()-interval '1 second'"
            " WHERE account_id=%s AND state='leased'",
            ("acct_a",),
        )


async def retained_windows(h):
    if h.pool is None:
        return tuple(
            sorted((*key, *value) for key, value in h.store._production_delivery_windows.items())
        )
    async with h.pool.connection() as connection:
        return tuple(
            await (
                await connection.execute(
                    "SELECT account_id,idempotency_key,queue,body_sha256,started_at,expires_at"
                    " FROM reporting_production_delivery_windows"
                    " ORDER BY account_id,idempotency_key"
                )
            ).fetchall()
        )


def use_clock(h, moment):
    h.clock.now = moment
    h.store._clock = h.clock
    # The status queue is retained by the projector; the other two are
    # reconstructed by the factory. All database clock seams must agree.
    for worker in h.workers:
        worker.outbox._clock = h.clock


async def pin_current_clock(h):
    moment = h.clock()
    if h.pool is not None:
        from adcp.reporting.outbox.pg import database_now

        async with h.pool.connection() as connection:
            moment = await database_now(connection, None)
    use_clock(h, moment)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("fault", [False, True], ids=["same-anchor", "reservation-fault"])
async def test_retry_window_and_http_attempt_share_the_reservation_boundary(
    backend, fault, tmp_path, monkeypatch
):
    from adcp.reporting.ledger.notification_models import ReportingNotificationError

    async with queued_production(backend, tmp_path, monkeypatch) as h:
        worker = h.workers[2]
        await pin_current_clock(h)
        production_operation_2 = await worker.expand_one(account_id="acct_a")
        assert production_operation_2
        calls = 0
        with monkeypatch.context() as patch:
            if h.pool is None:
                if fault:
                    import adcp.reporting.outbox.memory as memory

                    original = memory.token_hex

                    def fail(*args):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise ReportingNotificationError("injected_reservation_failure")
                        return original(*args)

                    patch.setattr(memory, "token_hex", fail)
            else:
                original = worker.outbox._next_attempt_on

                async def step(*args):
                    result = await original(*args)
                    if fault:
                        raise ReportingNotificationError("injected_reservation_failure")
                    # Deterministic time passes while the transaction reserves
                    # its ordinal. The persisted anchor must use fired_at.
                    h.clock.advance(timedelta(microseconds=1))
                    return result

                patch.setattr(worker.outbox, "_next_attempt_on", step)
            if fault:
                with pytest.raises(ReportingNotificationError, match="injected_reservation"):
                    await worker.deliver_one(account_id="acct_a")
            else:
                h.notification_failures.at("http.accepted", SimulatedCrash())
                with pytest.raises(SimulatedCrash):
                    await worker.deliver_one(account_id="acct_a")
        windows = await retained_windows(h)
        activity = await worker.outbox.list_activity(
            account_id="acct_a", consumer_id=h.item.binding.consumer_id
        )
        if fault:
            if h.pool is None:
                assert calls == 2  # after lease claim, at the HTTP reservation
            assert not windows, "failed HTTP reservation retained a first-attempt window"
            assert not activity
            assert not h.receiver.received
        else:
            assert len(windows) == len(activity) == 1
            assert windows[0][4] == activity[0].fired_at


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
async def test_window_insert_failure_rolls_back_attempt_head_and_all_state(
    backend, queue, tmp_path, monkeypatch
):
    from adcp.reporting.ledger.notification_models import ReportingNotificationError
    from adcp.reporting.outbox.activity import ActivityRequest

    async with queued_production(backend, tmp_path, monkeypatch) as h:
        await pin_current_clock(h)
        worker = h.workers[queue]
        production_operation_3 = await worker.expand_one(account_id="acct_a")
        assert production_operation_3
        lease = await worker.outbox.claim_delivery(
            account_id="acct_a", now=h.clock(), lease_seconds=60
        )
        assert lease is not None
        with monkeypatch.context() as patch:
            if h.pool is None:

                class FaultWindows(dict):
                    def setdefault(self, *args):
                        super().setdefault(*args)
                        raise ReportingNotificationError("injected_window_failure")

                patch.setattr(h.store, "_production_delivery_windows", FaultWindows())
            else:
                original = worker.outbox._connection

                class FaultConnection:
                    def __init__(self, connection):
                        self.connection = connection

                    def transaction(self):
                        return self.connection.transaction()

                    async def execute(self, query, params=None):
                        result = await self.connection.execute(query, params)
                        if query.startswith("INSERT INTO reporting_production_delivery_windows"):
                            raise ReportingNotificationError("injected_window_failure")
                        return result

                @asynccontextmanager
                async def connection():
                    async with original() as bound:
                        yield FaultConnection(bound)

                patch.setattr(worker.outbox, "_connection", connection)
            before = await h.image()
            with pytest.raises(ReportingNotificationError, match="injected_window_failure"):
                await worker.delivery_window.reserve_attempt(
                    worker.outbox,
                    lease,
                    request=ActivityRequest("https://receiver.example.test/reporting", 1),
                    now=h.clock(),
                )
            assert await h.image() == before
            assert not await retained_windows(h)
            assert not await worker.outbox.list_activity(
                account_id="acct_a", consumer_id=lease.delivery.binding.principal_id
            )
        attempt, deadline, expired = await worker.delivery_window.reserve_attempt(
            worker.outbox,
            lease,
            request=ActivityRequest("https://receiver.example.test/reporting", 1),
            now=h.clock(),
        )
        assert not expired and attempt.attempt == 1
        assert deadline == attempt.fired_at + timedelta(seconds=86400)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
async def test_timeout_backoff_duplicate_workers_and_configuration_change_keep_first_deadline(
    backend, queue, tmp_path, monkeypatch, caplog
):
    import httpx

    async with queued_production(backend, tmp_path, monkeypatch) as h:
        await pin_current_clock(h)
        for key in tuple(h.subscriptions.values):
            if key[0] == "acct_a" and key[1] != "buyer":
                del h.subscriptions.values[key]
        worker = h.workers[queue]
        production_operation_4 = await worker.expand_one(account_id="acct_a")
        assert production_operation_4
        h.receiver.responses["buyer"].extend([429, httpx.ReadTimeout("controlled timeout")])
        production_operation_5 = await worker.deliver_one(account_id="acct_a")
        assert production_operation_5
        original = await retained_windows(h)
        assert len(original) == 1
        first = (await worker.outbox.list_deliveries(account_id="acct_a"))[0]
        assert first.state == "pending"
        duplicate = fresh_workers(h)[queue]
        production_operation_6 = await asyncio.gather(
            worker.deliver_one(account_id="acct_a"), duplicate.deliver_one(account_id="acct_a")
        )
        assert production_operation_6 == [False, False]
        h.clock.advance(timedelta(seconds=5))
        # A reconstructed worker with a longer retry interval cannot postpone
        # expiration or turn it into another day of delivery eligibility.
        restarted = fresh_workers(h)[queue]
        restarted.retry_seconds = 2 * 86400
        production_operation_7 = await restarted.deliver_one(account_id="acct_a")
        assert production_operation_7
        assert await retained_windows(h) == original
        await h.store.put_configuration(replace(h.item.config, deactivated_at=h.clock()))
        h.signing.generation = 2
        use_clock(h, original[0][5] - timedelta(microseconds=1))
        production_operation_8 = await fresh_workers(h)[queue].deliver_one(account_id="acct_a")
        assert not production_operation_8
        use_clock(h, original[0][5])
        current, duplicate = fresh_workers(h)[queue], fresh_workers(h)[queue]
        production_condition_9 = sorted(
            await asyncio.gather(
                current.deliver_one(account_id="acct_a"),
                duplicate.deliver_one(account_id="acct_a"),
            )
        ) == [False, True]
        assert production_condition_9
        final = (await current.outbox.list_deliveries(account_id="acct_a"))[0]
        assert final.delivery == first.delivery
        assert (final.state, final.error_code) == ("suppressed", "lease_expired")
        assert await retained_windows(h) == original
        activity = await current.outbox.list_activity(
            account_id="acct_a", consumer_id=h.item.binding.consumer_id
        )
        assert [r.outcome.status for r in activity] == ["timeout", "failed"]
        assert [r.attempt for r in activity] == [2, 1]
        assert len(h.receiver.received) == 1
        production_operation_10 = await current.deliver_one(account_id="acct_b")
        assert not production_operation_10
        assert not caplog.records


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
async def test_worker_timeout_after_reservation_preserves_original_window_and_pending_activity(
    backend, queue, tmp_path, monkeypatch
):
    from ._reliable_support import Barrier

    async with queued_production(backend, tmp_path, monkeypatch) as h:
        await pin_current_clock(h)
        for key in tuple(h.subscriptions.values):
            if key[0] == "acct_a" and key[1] != "buyer":
                del h.subscriptions.values[key]
        worker = h.workers[queue]
        worker.lease_seconds = 1
        production_operation_11 = await worker.expand_one(account_id="acct_a")
        assert production_operation_11
        barrier = Barrier()
        h.notification_failures.at("http.before", barrier)
        task = asyncio.create_task(worker.deliver_one(account_id="acct_a"))
        try:
            await barrier.wait()
            original = await retained_windows(h)
            assert len(original) == 1
            use_clock(h, original[0][5])
            production_operation_19 = await asyncio.wait_for(task, 3)
            assert production_operation_19
        finally:
            barrier.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        restarted = fresh_workers(h)[queue]
        production_operation_12 = await restarted.deliver_one(account_id="acct_a")
        assert production_operation_12
        assert await retained_windows(h) == original
        activity = await restarted.outbox.list_activity(
            account_id="acct_a", consumer_id=h.item.binding.consumer_id
        )
        assert len(activity) == 1 and activity[0].outcome is None
        assert not h.receiver.received
        assert (await restarted.outbox.list_deliveries(account_id="acct_a"))[
            0
        ].state == "suppressed"


async def test_pg_deadline_crossed_while_reserving_an_ordinal_rolls_it_back(tmp_path, monkeypatch):
    async with queued_production("postgres", tmp_path, monkeypatch) as h:
        worker = h.workers[2]
        production_operation_13 = await worker.expand_one(account_id="acct_a")
        assert production_operation_13
        h.notification_failures.at("http.accepted", SimulatedCrash())
        with pytest.raises(SimulatedCrash):
            await worker.deliver_one(account_id="acct_a")
        original = await retained_windows(h)
        use_clock(h, original[0][5] - timedelta(microseconds=1))
        restarted = fresh_workers(h)[2]
        before = await restarted.outbox.list_activity(
            account_id="acct_a", consumer_id=h.item.binding.consumer_id
        )
        original_next = restarted.outbox._next_attempt_on

        async def cross_deadline(*args):
            ordinal = await original_next(*args)
            h.clock.advance(timedelta(microseconds=1))
            return ordinal

        monkeypatch.setattr(restarted.outbox, "_next_attempt_on", cross_deadline)
        production_operation_14 = await restarted.deliver_one(account_id="acct_a")
        assert production_operation_14
        assert len(h.receiver.received) == 1
        assert await retained_windows(h) == original
        assert (
            await restarted.outbox.list_activity(
                account_id="acct_a", consumer_id=h.item.binding.consumer_id
            )
            == before
        )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["before", "exact", "after"])
async def test_retry_horizon_is_immutable_across_crash_rotation_and_restart(
    backend, queue, offset, tmp_path, monkeypatch
):
    async with queued_production(backend, tmp_path, monkeypatch) as h:
        worker = h.workers[queue]
        production_operation_15 = await worker.expand_one(account_id="acct_a")
        assert production_operation_15
        h.notification_failures.at("http.accepted", SimulatedCrash())
        with pytest.raises(SimulatedCrash):
            await worker.deliver_one(account_id="acct_a")
        received = h.receiver.received[0]
        original = await retained_windows(h)
        assert len(original) == 1
        row = original[0]
        assert row[1] == received.idempotency_key
        assert (row[5] - row[4]).total_seconds() == 86400
        # Only the deterministic database clock seam is advanced. The immutable
        # persisted timestamps/bytes are not edited to fabricate expiration.
        use_clock(h, row[5] + timedelta(microseconds=offset))
        h.signing.generation = 2
        restarted = fresh_workers(h)[queue]
        production_operation_16 = await restarted.deliver_one(account_id="acct_a")
        assert production_operation_16
        received_count = len(h.receiver.received)
        assert received_count == (2 if offset < 0 else 1)
        assert await retained_windows(h) == original
        target = next(
            value
            for value in await restarted.outbox.list_deliveries(account_id="acct_a")
            if value.delivery.binding.idempotency_key == received.idempotency_key
        )
        assert (target.state, target.error_code) == (
            ("complete", None) if offset < 0 else ("suppressed", "lease_expired")
        )
        assert await h.receiver_store.read("acct_a", received.idempotency_key) == received.body
        # Expiry cannot erase the original ambiguous attempt. Existing public
        # account-activity projection and authenticated polling still recover
        # the retained history; no fabricated late HTTP outcome is inserted.
        from adcp.decisioning.accounts import ResolveContext
        from adcp.decisioning.context import AuthInfo
        from adcp.reporting.outbox import ReportingActivityProjector

        principal = h.subscriptions.values[("acct_a", received.subscriber_id)].principal_id
        activity = ReportingActivityProjector(restarted.outbox)
        rows = await activity.for_account(
            account_id="acct_a",
            context=ResolveContext(auth_info=AuthInfo(kind="bearer", principal=principal)),
        )
        assert len(rows) == (2 if offset < 0 else 1)
        assert (
            await activity.for_account(
                account_id="acct_b",
                context=ResolveContext(auth_info=AuthInfo(kind="bearer", principal=principal)),
            )
            == []
        )
        mount = MountedProduction(h)
        mount.authorize(h.item)
        async with mount.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, polling = await mount.call(
                    client,
                    "get_reporting_status",
                    {"account": {"account_id": "acct_a"}, "view": "periods"},
                    transport=transport,
                )
                assert polling["status"] == "completed"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
async def test_actual_queues_retry_exact_signed_bytes_after_acceptance_before_ack(
    backend, queue, tmp_path, monkeypatch
):
    async with queued_production(backend, tmp_path, monkeypatch) as h:
        original_quarantine = await h.queue()
        for worker in h.workers:
            for _ in range(100):
                if not await worker.expand_one(account_id="acct_a"):
                    break
            else:
                pytest.fail("production fanout did not reach a bounded idle state")
        worker = h.workers[queue]
        assert await worker.outbox.list_deliveries(account_id="acct_a")
        h.notification_failures.at("http.accepted", SimulatedCrash())
        with pytest.raises(SimulatedCrash):
            await worker.deliver_one(account_id="acct_a")
        first = h.receiver.received[-1]
        assert json.loads(first.body)["notification_type"] == EVENTS[queue]
        assert await h.receiver_store.read("acct_a", first.idempotency_key) == first.body
        await expire_queue_lease(h, worker)
        h.signing.generation = 2
        for restarted in fresh_workers(h):
            for _ in range(100):
                if not await restarted.deliver_one(account_id="acct_a"):
                    break
            else:
                pytest.fail("production delivery did not reach a bounded idle state")
            assert {
                r.state for r in await restarted.outbox.list_deliveries(account_id="acct_a")
            } == {"complete"}
            assert not await restarted.outbox.list_deliveries(account_id="acct_b")
        repeated = [r for r in h.receiver.received if r.idempotency_key == first.idempotency_key]
        assert len(repeated) == 2 and repeated[0].body == repeated[1].body
        assert "key-1" in repeated[0].headers["signature-input"]
        assert "key-2" in repeated[1].headers["signature-input"]
        options = WebhookVerifyOptions(
            jwks_resolver=StaticJwksResolver({"keys": notification_verification_keys()}),
            clock=lambda: h.clock().timestamp(),
        )
        assert {json.loads(r.body)["notification_type"] for r in h.receiver.received} == set(EVENTS)
        for received in h.receiver.received:
            value = json.loads(received.body)
            validate_notification_payload(value)
            verify_webhook_signature(
                method="POST",
                url="https://receiver.example.test" + received.target,
                headers=received.headers,
                body=received.body,
                options=options,
            )
            if value["notification_type"] == "reporting.delivery_ready":
                assert received.subscriber_id in {"buyer", "second"}
            assert received.account_id == "acct_a"
        assert await h.queue() == original_quarantine
        assert h.item.writer.writes == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
async def test_new_queue_fanout_failure_rolls_back_all_recipients(
    backend, queue, tmp_path, monkeypatch
):
    async with queued_production(backend, tmp_path, monkeypatch) as h:
        worker = h.workers[queue]
        lease = await worker.outbox.claim_expansion(
            account_id="acct_a", now=h.clock(), lease_seconds=60
        )
        assert lease is not None
        from adcp.reporting.ledger.notification_models import decode_event

        event = decode_event(lease.event)
        subscriptions = await h.subscriptions.list_active(
            account_id="acct_a", notification_type=event.notification_type
        )
        deliveries = tuple(
            worker.cipher.prepare(event, s, lease.emission_generation)
            for s in subscriptions
            if s.matches(event)
        )
        assert len(deliveries) >= 2
        before = await h.image()
        with monkeypatch.context() as patch:
            if h.pool is None:
                import adcp.reporting.outbox.memory as memory

                original = memory._finish

                def fail(*args, **kwargs):
                    original(*args, **kwargs)
                    raise RuntimeError("injected fanout finish failure")

                patch.setattr(memory, "_finish", fail)
            else:
                original = worker.outbox._insert_delivery

                async def fail(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise RuntimeError("injected fanout insert failure")

                patch.setattr(worker.outbox, "_insert_delivery", fail)
            with pytest.raises(RuntimeError, match="injected fanout"):
                await worker.outbox.complete_expansion(lease, deliveries, now=h.clock())
        assert await h.image() == before
        assert not await worker.outbox.list_deliveries(account_id="acct_a")
        await expire_queue_lease(h, worker, expansion=True)
        restarted = fresh_workers(h)[queue]
        production_operation_17 = await restarted.expand_one(account_id="acct_a")
        assert production_operation_17
        assert len(await restarted.outbox.list_deliveries(account_id="acct_a")) == len(deliveries)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
@pytest.mark.parametrize("mutation", ["algorithm", "legacy", "transient", "permanent", "cancel"])
async def test_current_signing_failure_never_sends_or_exposes_private_data(
    backend, queue, mutation, tmp_path, monkeypatch, caplog
):
    from cryptography.hazmat.primitives.asymmetric import ec

    from adcp.reporting.outbox.routing import (
        ReportingLegacyAuthentication,
        ReportingSigningMaterial,
    )
    from adcp.webhook_sender import ScopePermanentlyUnknown, ScopeTransientlyUnavailable

    async with queued_production(backend, tmp_path, monkeypatch) as h:
        worker = h.workers[queue]
        production_operation_18 = await worker.expand_one(account_id="acct_a")
        assert production_operation_18
        quarantine = await h.queue()

        async def incompatible(**kwargs):
            if mutation == "transient":
                raise ScopeTransientlyUnavailable()
            if mutation == "permanent":
                raise ScopePermanentlyUnknown()
            if mutation == "cancel":
                raise asyncio.CancelledError()
            return ReportingSigningMaterial(
                ec.derive_private_key(1, ec.SECP256R1()),
                "https://seller.example.test/keys#changed",
                "ecdsa-p256-sha256",
                frozenset({"ecdsa-p256-sha256"}),
            )

        with monkeypatch.context() as patch:
            if mutation == "legacy":
                for key, subscription in tuple(h.subscriptions.values.items()):
                    h.subscriptions.values[key] = replace(
                        subscription,
                        signing_scope_id=None,
                        authentication=ReportingLegacyAuthentication("Bearer", "fixture-only"),
                    )
            else:
                patch.setattr(h.signing, "resolve", incompatible)
            if mutation == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await worker.deliver_one(account_id="acct_a")
            else:
                production_operation_20 = await worker.deliver_one(account_id="acct_a")
                assert production_operation_20
        assert not h.receiver.received
        assert not caplog.records
        deliveries = await worker.outbox.list_deliveries(account_id="acct_a")
        assert all(r.state != "complete" for r in deliveries)
        if mutation == "cancel":
            assert sum(r.state == "leased" for r in deliveries) == 1
        elif mutation == "permanent":
            assert sum(r.state == "quarantined" for r in deliveries) == 1
        else:
            assert {r.state for r in deliveries} == {"pending"}
        assert await h.queue() == quarantine
