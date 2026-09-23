"""Cross-layer real PostgreSQL failure matrix, extended by later slices.

Producer, fanout worker, HTTP worker, receiver and observer have independent
processes/pools. Named IPC/SQL barriers control interleavings; no sleeps.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from functools import wraps

import pytest

from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.outbox import DeliveryLease, PgReportingOutbox

from ._generation_support import isolated_reporting_pool
from ._reliable_support import ManualClock, _BytesStore, service_process


def case_deadline(function):
    @wraps(function)
    async def run(*args, **kwargs):
        try:
            return await asyncio.wait_for(function(*args, **kwargs), 150)
        except (TimeoutError, asyncio.TimeoutError):
            raise AssertionError(f"reporting_matrix case={function.__name__} deadline") from None

    return run


async def settle(pool):
    """A SQL barrier ensures disconnected transactions released their fences."""
    async with pool.connection() as conn, conn.transaction():
        await PgReportingReconciliationStore._lock_account(conn, "acct_a")
        await conn.execute(
            "SELECT 1 FROM reporting_notification_expansions WHERE account_id = %s FOR UPDATE",
            ("acct_a",),
        )
        await conn.execute(
            "SELECT 1 FROM reporting_notification_deliveries WHERE account_id = %s FOR UPDATE",
            ("acct_a",),
        )


async def produce_and_expand(pool):
    async with service_process(pool, "producer") as producer:
        await producer.event("done")
        await producer.finish()
    async with service_process(pool, "fanout") as fanout:
        assert (await fanout.event("done"))["did_work"]
        await fanout.finish()


@pytest.mark.parametrize("crash", ["event_inserted", "revision_committed"])
@case_deadline
async def test_revision_commit_to_fanout_process_crash_restart(crash):
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingReconciliationStore(pool=pool).create_schema()
        clock = ManualClock()
        observer = PgReportingReconciliationStore(pool=pool, clock=clock, notifications=True)
        outbox = PgReportingOutbox(pool=pool, clock=clock)
        async with service_process(pool, "producer", pause=crash) as producer:
            await producer.event(crash)
            revision = await observer.get_revision(
                account_id="acct_a", reporting_revision_id="rpr_acct_a_process"
            )
            events = await outbox.list_events(account_id="acct_a")
            assert (revision is not None) == (crash == "revision_committed")
            assert len(events) == (1 if crash == "revision_committed" else 0)
            await producer.kill()
        await settle(pool)
        await produce_and_expand(pool)
        after = await outbox.list_events(account_id="acct_a")
        assert len(after) == 1
        if events:
            assert after == events
        assert (await outbox.list_deliveries(account_id="acct_a"))[0].state == "pending"
        assert await outbox.list_events(account_id="other") == ()
        assert await outbox.list_deliveries(account_id="other") == ()


@case_deadline
async def test_process_death_partway_through_fanout_cannot_mix_membership():
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingReconciliationStore(pool=pool).create_schema()
        async with service_process(pool, "producer") as producer:
            await producer.event("done")
            await producer.finish()
        clock = ManualClock()
        outbox = PgReportingOutbox(pool=pool, clock=clock)
        async with service_process(
            pool, "fanout", subscribers=["old-a", "old-b"], pause="fanout_partial"
        ) as fanout:
            await fanout.event("fanout_partial")
            assert await outbox.list_deliveries(account_id="acct_a") == ()
            await fanout.kill()
        await settle(pool)
        async with service_process(
            pool, "fanout", subscribers=["replacement"], advance_seconds=61
        ) as restarted:
            assert (await restarted.event("done"))["did_work"]
            await restarted.finish()
        rows = await outbox.list_deliveries(account_id="acct_a")
        assert [row.delivery.binding.subscriber_id for row in rows] == ["replacement"]
        assert len(await outbox.list_events(account_id="acct_a")) == 1
        async with service_process(
            pool, "fanout", subscribers=["yet-another"], advance_seconds=62
        ) as replay:
            assert not (await replay.event("done"))["did_work"]
        assert await outbox.list_deliveries(account_id="acct_a") == rows


@pytest.fixture
def certificate(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.derive_private_key(3, ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "receiver.example.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2040, 1, 1, tzinfo=timezone.utc))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("receiver.example.test")]), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "receiver.pem", tmp_path / "receiver-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    return {"certificate": str(cert_file), "certificate_key": str(key_file)}


@pytest.mark.parametrize("crash", ["before_ack", "ack_written", "ack_connection_loss"])
@case_deadline
async def test_http_acceptance_to_ack_process_failure_converges_with_immutable_retry(
    crash, certificate
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingReconciliationStore(pool=pool).create_schema()
        await produce_and_expand(pool)
        clock = ManualClock()
        outbox = PgReportingOutbox(pool=pool, clock=clock)
        (original,) = await outbox.list_deliveries(account_id="acct_a")
        body_store = _BytesStore(pool)
        async with service_process(
            pool, "receiver", pause_responses=True, **certificate
        ) as receiver:
            port = (await receiver.event("listening"))["port"]
            network = {**certificate, "receiver_port": port}
            pause = "before_ack" if crash == "before_ack" else "ack_written"
            # Import and open the competitor's pool before holding an HTTP
            # response. Process startup must never race the sender's timeout.
            async with service_process(pool, "worker", start_paused=True, **network) as competitor:
                await competitor.event("worker_ready")
                async with service_process(pool, "worker", pause=pause, **network) as worker:
                    stale = await acceptance_to_ack(
                        pool, outbox, original, receiver, worker, competitor, pause, crash
                    )
            await settle(pool)
            clock.advance(timedelta(seconds=61))
            await receiver.send(advance_seconds=61)
            await receiver.event("clock_advanced")
            async with service_process(
                pool,
                "worker",
                pause="before_ack",
                advance_seconds=61,
                signing_generation=2,
                **network,
            ) as restarted:
                assert (await receiver.event("http_accepted"))["attempt"] == 2
                for state in ("complete", "pending", "suppressed", "quarantined"):
                    assert not await outbox.finish_delivery(stale, now=clock(), state=state)
                await receiver.send(release_http=2)
                await receiver.event("http_released")
                await restarted.event("before_ack")
                await restarted.send(**{"continue": "before_ack"})
                assert (await restarted.event("done"))["did_work"]
                await restarted.finish()
            await receiver.send(stop=True)
            await receiver.finish()
        key = original.delivery.binding.idempotency_key
        attempts = [
            json.loads(await body_store.get("http-attempt", "acct_a", key, str(n))) for n in (1, 2)
        ]
        assert attempts[0]["body"] == attempts[1]["body"]
        assert base64.b64decode(attempts[0]["body"]) == await body_store.get(
            "receiver", "acct_a", key
        )
        assert attempts[0]["headers"]["signature"] != attempts[1]["headers"]["signature"]
        assert attempts[0]["verified_key"].endswith("key-1")
        assert attempts[1]["verified_key"].endswith("key-2")
        (retained,) = await outbox.list_deliveries(account_id="acct_a")
        assert retained.delivery == original.delivery
        assert retained.state == "complete" and retained.attempt_count == 2
        assert len(await outbox.list_events(account_id="acct_a")) == 1


async def acceptance_to_ack(pool, outbox, original, receiver, worker, competitor, pause, crash):
    assert (await receiver.event("http_accepted"))["attempt"] == 1
    # HTTP is waiting for its response. No worker connection holds
    # a transaction, and the other worker cannot claim this lease.
    async with pool.connection() as conn:
        states = await (
            await conn.execute(
                "SELECT state FROM pg_stat_activity WHERE application_name = %s",
                (f"reporting-matrix-worker-{worker.process.pid}",),
            )
        ).fetchall()
        assert states and all(state == "idle" for (state,) in states)
        lease_row = await (
            await conn.execute(
                "SELECT lease_token, lease_expires_at, claim_count"
                " FROM reporting_notification_deliveries"
                " WHERE account_id = %s AND delivery_id = %s",
                ("acct_a", original.delivery.binding.delivery_id),
            )
        ).fetchone()
    stale = DeliveryLease(original.delivery, *lease_row)
    await competitor.send(**{"continue": "worker_ready"})
    assert not (await competitor.event("done"))["did_work"]
    await competitor.finish()
    await receiver.send(release_http=1)
    await receiver.event("http_released")
    stopped = await worker.event(pause)
    assert (await outbox.list_deliveries(account_id="acct_a"))[0].state == "leased"
    if crash == "ack_connection_loss":
        async with pool.connection() as conn:
            assert (
                await (
                    await conn.execute("SELECT pg_terminate_backend(%s)", (stopped["backend_pid"],))
                ).fetchone()
            )[0]
        await worker.send(**{"continue": pause})
        failure = await worker.event("service_failed")
        assert failure["classification"] == "database_failure"
        await worker.finish(code=1)
    else:
        await worker.kill()
    return stale


@case_deadline
async def test_child_barrier_deadline_exits_with_named_sanitized_diagnostic():
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with service_process(pool, "barrier_probe", deadlines={"barrier:held": 0}) as probe:
            await probe.event("held")
            error = await probe.event("service_failed")
            assert error == {
                "point": "service_failed",
                "classification": "deadline",
                "stage": "barrier:held",
            }
            await probe.finish(code=1)
