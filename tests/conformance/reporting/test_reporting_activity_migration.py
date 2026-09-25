"""PG16 multi-worker fencing, subset readiness, and actual A/B rolling binaries."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from importlib.resources import files
from pathlib import Path

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.outbox import (
    ActivityOutcome,
    ActivityRequest,
    PgReportingOutbox,
    ReportingActivityProjector,
    ReportingNotificationError,
)
from adcp.reporting.outbox._schema import REQUIRED_OBJECTS, schema_objects, validate_schema

from ._generation_support import (
    NOW,
    configuration,
    isolated_reporting_pool,
    obligation_for,
    require_rolling_database,
    revision_for,
)
from ._provisional_catalog import PROVISIONAL_OBJECTS
from ._reliable_support import (
    Barrier,
    NotificationHarness,
    notification_subscription,
    reliable_factory,
)
from .test_reporting_notification_migration import CHAIN, WAIVER_OBJECTS, retained_physical_rows
from .test_reporting_notification_packaging import run_step
from .test_reporting_webhook_activity import prepare, reserve, rows

SQL = files("adcp.reporting.ledger").joinpath("reporting_webhook_activity.sql").read_text()
# The integrated A predecessor includes checkpoint and locale-portable readiness.
BASE = "17ee407ae3978c8a2bb54437287afbf9dafb8130"
ROOT = Path(__file__).resolve().parents[3]


async def install_a(pool):
    async with pool.connection() as conn, conn.transaction():
        for name in CHAIN:
            await conn.execute(files("adcp.reporting.ledger").joinpath(name).read_text())


async def test_required_manifest_is_identical_across_random_schemas_and_repeated_migration():
    snapshots = []
    identities = []
    manifest = files("adcp.reporting.outbox").joinpath("required_schema.json").read_text()
    async with (
        isolated_reporting_pool(autocommit=True) as first,
        isolated_reporting_pool(autocommit=True) as second,
    ):
        for pool in (first, second):
            store = PgReportingLedgerStore(pool=pool)
            for _ in range(2):
                await store.create_schema()
                async with pool.connection() as conn:
                    objects = await schema_objects(conn)
                    assert len(WAIVER_OBJECTS) == 10
                    assert objects == {**REQUIRED_OBJECTS, **WAIVER_OBJECTS, **PROVISIONAL_OBJECTS}
                    # Regenerating unchanged SQL must have byte-for-byte zero
                    # diff, including all function security/search_path flags.
                    required = {key: objects[key] for key in REQUIRED_OBJECTS}
                    assert json.dumps(required, indent=2, sort_keys=True) + "\n" == manifest
                    snapshots.append(objects)
            async with pool.connection() as conn:
                identities.append(
                    await (
                        await conn.execute(
                            "SELECT current_schema(),"
                            " 'reporting_webhook_attempt_guard()'::regprocedure::oid"
                        )
                    ).fetchone()
                )
        assert identities[0][0] != identities[1][0] and identities[0][1] != identities[1][1]
        assert snapshots == [snapshots[0]] * 4


@pytest.mark.parametrize("autocommit", [False, True])
async def test_populated_a_to_b_migration_is_additive_repeated_and_atomic(autocommit):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        await install_a(pool)
        ledger = PgReportingLedgerStore(pool=pool, clock=lambda: NOW, notifications=True)
        config = configuration()
        obligation = obligation_for(config)
        revision, data = revision_for(obligation)
        await ledger.put_configuration(config)
        await ledger.commit_obligation(obligation)
        await ledger.commit_revision(revision, data)
        before = await retained_physical_rows(pool)
        for _ in range(2):
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(SQL)
                await validate_schema(conn, activity=True)
            assert await retained_physical_rows(pool) == before
        outbox = PgReportingOutbox(pool=pool)
        assert await outbox.list_activity(account_id="acct_a", consumer_id="buyer") == ()
        assert len(await outbox.list_events(account_id="acct_a")) == 1


async def test_concurrent_and_interrupted_b_migration_visibility():
    async with isolated_reporting_pool(autocommit=True) as pool:
        from psycopg_pool import AsyncConnectionPool

        await install_a(pool)
        gate = Barrier()
        async with AsyncConnectionPool(pool.conninfo, kwargs=pool.kwargs, open=False) as observer:

            async def interrupted():
                async with pool.connection() as conn, conn.transaction():
                    await conn.execute(SQL)
                    await gate.pause()

            task = asyncio.create_task(interrupted())
            await gate.wait()
            async with observer.connection() as conn:
                assert (
                    await (
                        await conn.execute("SELECT to_regclass('reporting_webhook_attempts')")
                    ).fetchone()
                )[0] is None
                with pytest.raises(ReportingNotificationError, match="missing"):
                    await validate_schema(conn, activity=True)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            async def install(selected):
                async with selected.connection() as conn, conn.transaction():
                    await conn.execute(SQL)

            await asyncio.wait_for(
                asyncio.gather(*(install(selected) for selected in (pool, observer) * 3)),
                timeout=20,
            )
            async with observer.connection() as conn:
                await validate_schema(conn, activity=True)


async def test_required_subset_accepts_unrelated_adopter_objects():
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingLedgerStore(pool=pool).create_schema()
        async with pool.connection() as conn:
            await conn.execute(
                "CREATE INDEX adopter_lookup ON reporting_notification_deliveries (principal_id)"
            )
            await conn.execute(
                "ALTER TABLE reporting_webhook_attempts ADD COLUMN adopter_note integer DEFAULT 0"
            )
            await conn.execute(
                "ALTER TABLE reporting_webhook_attempts ADD CONSTRAINT adopter_payload"
                " CHECK (payload_size_bytes >= 0)"
            )
            await conn.execute(
                "CREATE FUNCTION adopter_noop() RETURNS TRIGGER LANGUAGE plpgsql"
                " AS $$ BEGIN RETURN NEW; END $$"
            )
            await conn.execute(
                "CREATE TRIGGER adopter_observe BEFORE INSERT ON reporting_webhook_attempts"
                " FOR EACH ROW EXECUTE FUNCTION adopter_noop()"
            )
            await validate_schema(conn)
            await validate_schema(conn, activity=True)


@pytest.mark.parametrize(
    "damage,classification",
    [
        ("DROP INDEX reporting_webhook_activity_newest", "missing"),
        (
            "ALTER TABLE reporting_webhook_attempts ALTER COLUMN principal_id DROP NOT NULL",
            "changed",
        ),
        (
            "ALTER TABLE reporting_webhook_attempts DISABLE TRIGGER"
            " reporting_webhook_attempt_guard",
            "disabled",
        ),
        (
            "ALTER TABLE reporting_webhook_attempts DROP CONSTRAINT reporting_webhook_outcome",
            "missing",
        ),
        ("ALTER FUNCTION reporting_webhook_attempt_guard() SECURITY DEFINER", "changed"),
        ("ALTER FUNCTION reporting_webhook_attempt_guard() STABLE", "changed"),
        ("ALTER FUNCTION reporting_webhook_attempt_guard() LEAKPROOF", "changed"),
        (
            "ALTER FUNCTION reporting_webhook_attempt_guard() SET search_path TO pg_catalog",
            "changed",
        ),
        (
            "CREATE OR REPLACE FUNCTION reporting_webhook_attempt_guard()"
            " RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
            "changed",
        ),
    ],
)
async def test_damaged_required_b_objects_fail_closed_with_safe_actionable_classification(
    damage, classification
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingLedgerStore(pool=pool).create_schema()
        async with pool.connection() as conn:
            # PK columns cannot drop NOT NULL; use an immutable non-PK column.
            await conn.execute(
                damage.replace("principal_id DROP NOT NULL", "reservation_token DROP NOT NULL")
            )
            await validate_schema(conn)  # Layer A stays independently ready.
            with pytest.raises(
                ReportingNotificationError, match=f"notification_schema_unready:{classification}:"
            ) as caught:
                await validate_schema(conn, activity=True)
            assert "RETURN NEW" not in str(caught.value) and "SECURITY DEFINER" not in str(
                caught.value
            )


async def test_required_named_constraint_must_be_validated():
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingLedgerStore(pool=pool).create_schema()
        async with pool.connection() as conn:
            await conn.execute(
                "ALTER TABLE reporting_webhook_attempts DROP CONSTRAINT"
                " reporting_webhook_timestamps"
            )
            await conn.execute(
                "ALTER TABLE reporting_webhook_attempts ADD CONSTRAINT reporting_webhook_timestamps"
                " CHECK (completed_at >= fired_at) NOT VALID"
            )
            with pytest.raises(ReportingNotificationError, match="disabled:constraint"):
                await validate_schema(conn, activity=True)


@pytest.mark.parametrize("flag", ["indisvalid", "indisready"])
async def test_required_unusable_index_blocks_capability_boot(flag):
    from adcp.reporting.outbox import ReportingActivitySupport

    async with reliable_factory("postgres", notifications=True) as reliable:
        from psycopg import sql

        h = NotificationHarness(reliable)
        outbox, worker = await prepare(h)
        support = ReportingActivitySupport(
            worker, reliable.store, ReportingActivityProjector(outbox)
        )
        assert await support.durable()
        # Controlled DDL after a completed startup proof must invalidate it.
        support.invalidate_schema_validation()
        # The task-owned PG16 admin fixture can model the catalog state left
        # by a failed concurrent index build without timing a real crash.
        async with reliable.blobs.pool.connection() as conn:
            await conn.execute(
                sql.SQL(
                    "UPDATE pg_index SET {}=false"
                    " WHERE indexrelid='reporting_webhook_activity_newest'::regclass"
                ).format(sql.Identifier(flag))
            )
        with pytest.raises(
            ReportingNotificationError, match="notification_schema_unready:disabled:index:"
        ):
            await support.durable()


async def test_independent_pg_workers_reserve_once_and_lock_parent_before_head(monkeypatch):
    async with reliable_factory("postgres", notifications=True) as reliable:
        from psycopg import AsyncConnection
        from psycopg_pool import AsyncConnectionPool

        h = NotificationHarness(reliable)
        outbox, _ = await prepare(h)
        lease = await outbox.claim_delivery(
            account_id="acct_a", now=reliable.clock(), lease_seconds=60
        )
        assert lease is not None
        commands = []
        execute = AsyncConnection.execute

        async def record(conn, query, params=None, **kwargs):
            if isinstance(query, str) and "reporting_" in query:
                commands.append(query)
                if any(
                    name in query
                    for name in ("reporting_webhook_attempts", "reporting_webhook_attempt_heads")
                ):
                    assert "account_id" in query and "principal_id" in query
                    if query.startswith(("SELECT", "UPDATE", "DELETE")):
                        assert "WHERE account_id = %s" in query and "principal_id = %s" in query
            return await execute(conn, query, params, **kwargs)

        monkeypatch.setattr(AsyncConnection, "execute", record)
        pool = reliable.blobs.pool
        async with AsyncConnectionPool(pool.conninfo, kwargs=pool.kwargs, open=False) as other_pool:
            other = PgReportingOutbox(pool=other_pool, clock=reliable.clock)
            request = ActivityRequest("https://receiver.example.test/reporting?TOKEN_SECRET", 1)
            reserved = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        box.reserve_attempt(lease, request=request, now=reliable.clock())
                        for box in (outbox, other) * 3
                    )
                ),
                timeout=10,
            )
            assert sum(item is not None for item in reserved) == 1
            attempt = next(item for item in reserved if item is not None)
            assert attempt.attempt == 1
            parent_index = next(i for i, query in enumerate(commands) if "FOR UPDATE" in query)
            head_index = next(
                i
                for i, query in enumerate(commands)
                if "INSERT INTO reporting_webhook_attempt_heads" in query
            )
            assert parent_index < head_index
            assert await other.list_activity(account_id="acct_a", consumer_id="buyer", limit=1) == (
                attempt,
            )
            forged = replace(attempt, binding=replace(attempt.binding, principal_id="other"))
            completed_attempt_5 = await other.complete_attempt(
                forged, outcome=ActivityOutcome("timeout"), now=reliable.clock()
            )
            assert not completed_attempt_5
            completed_attempt_6 = await outbox.complete_attempt(
                attempt, outcome=ActivityOutcome("timeout"), now=reliable.clock()
            )
            assert completed_attempt_6
            purged_count_4 = await other.purge_activity(
                account_id="acct_a", consumer_id="buyer", now=reliable.clock()
            )
            assert purged_count_4 == 0


@pytest.mark.parametrize("locked", ["parent", "head"])
async def test_lock_wait_rechecks_expiry_before_reservation_and_rolls_back_counter(
    locked, monkeypatch
):
    async with reliable_factory("postgres", notifications=True) as reliable:
        from psycopg import AsyncConnection
        from psycopg_pool import AsyncConnectionPool

        h = NotificationHarness(reliable)
        outbox, _ = await prepare(h)
        previous_lease, previous = await reserve(h, outbox)
        completed_attempt_1 = await outbox.complete_attempt(
            previous, outcome=ActivityOutcome("failed", 500, 1), now=reliable.clock()
        )
        assert completed_attempt_1
        finished_delivery_1 = await outbox.finish_delivery(
            previous_lease, now=reliable.clock(), state="pending", retry_at=reliable.clock()
        )
        assert finished_delivery_1
        lease = await outbox.claim_delivery(
            account_id="acct_a", now=reliable.clock(), lease_seconds=1
        )
        pool = reliable.blobs.pool
        async with AsyncConnectionPool(pool.conninfo, kwargs=pool.kwargs, open=False) as second:
            other = PgReportingOutbox(pool=second, clock=reliable.clock)
            entered = asyncio.get_running_loop().create_future()
            execute = AsyncConnection.execute

            async def observe(conn, query, params=None, **kwargs):
                target = (
                    "FROM reporting_notification_deliveries"
                    if locked == "parent"
                    else "INSERT INTO reporting_webhook_attempt_heads"
                )
                if isinstance(query, str) and target in query and not entered.done():
                    entered.set_result(conn.info.backend_pid)
                return await execute(conn, query, params, **kwargs)

            async with pool.connection() as conn, conn.transaction():
                b = lease.delivery.binding
                if locked == "parent":
                    await conn.execute(
                        "SELECT 1 FROM reporting_notification_deliveries WHERE account_id=%s"
                        " AND principal_id=%s AND delivery_id=%s FOR UPDATE",
                        (b.account_id, b.principal_id, b.delivery_id),
                    )
                else:
                    await conn.execute(
                        "SELECT 1 FROM reporting_webhook_attempt_heads WHERE account_id=%s"
                        " AND principal_id=%s AND subscriber_id=%s"
                        " AND idempotency_key=%s FOR UPDATE",
                        (b.account_id, b.principal_id, b.subscriber_id, b.idempotency_key),
                    )
                monkeypatch.setattr(AsyncConnection, "execute", observe)
                task = asyncio.create_task(
                    other.reserve_attempt(
                        lease,
                        request=ActivityRequest("https://example.test/hooks", 1),
                        now=reliable.clock(),
                    )
                )
                pid = await asyncio.wait_for(entered, timeout=5)

                async def blocked():
                    while True:
                        row = await (
                            await conn.execute("SELECT pg_blocking_pids(%s)", (pid,))
                        ).fetchone()
                        if row[0]:
                            return
                        await asyncio.sleep(0)

                await asyncio.wait_for(blocked(), timeout=5)
                reliable.clock.advance(timedelta(seconds=2))
            if locked == "parent":
                task_result_1 = await asyncio.wait_for(task, timeout=5)
                assert task_result_1 is None
            else:
                with pytest.raises(ReportingNotificationError, match="activity_lease_expired"):
                    await asyncio.wait_for(task, timeout=5)
            assert len(await rows(other)) == 1
            _, next_attempt = await reserve(h, other)
            assert next_attempt.attempt == 2


@pytest.mark.parametrize("mutation", ["DELETE", "RESET", "RETARGET"])
async def test_retained_head_cannot_be_deleted_reset_or_retargeted_after_purge(mutation):
    async with reliable_factory("postgres", notifications=True) as reliable:
        import psycopg

        h = NotificationHarness(reliable)
        outbox, _ = await prepare(h)
        _, attempt = await reserve(h, outbox)
        completed_attempt_2 = await outbox.complete_attempt(
            attempt, outcome=ActivityOutcome("timeout"), now=reliable.clock()
        )
        assert completed_attempt_2
        reliable.clock.advance(timedelta(days=31))
        purged_count_1 = await outbox.purge_activity(
            account_id="acct_a", consumer_id="buyer", now=reliable.clock()
        )
        assert purged_count_1 == 1
        commands = {
            "DELETE": "DELETE FROM reporting_webhook_attempt_heads",
            "RESET": "UPDATE reporting_webhook_attempt_heads SET last_attempt=1",
            "RETARGET": "UPDATE reporting_webhook_attempt_heads SET principal_id='foreign',"
            " last_attempt=last_attempt+1",
        }
        async with reliable.blobs.pool.connection() as conn:
            with pytest.raises(psycopg.Error, match="counter must advance and be retained"):
                await conn.execute(
                    commands[mutation] + " WHERE account_id=%s AND principal_id=%s",
                    ("acct_a", "buyer"),
                )
            await conn.rollback()
        _, next_attempt = await reserve(h, outbox)
        assert next_attempt.attempt == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "'https://example.test/RAW_SECRET'"),
        ("payload_size_bytes", "99"),
        ("idempotency_key", "'other'"),
        ("notification_id", "'other'"),
        ("principal_id", "'other'"),
        ("reservation_token", "'other'"),
        ("attempt", "99"),
    ],
)
async def test_sql_rejects_mutable_identity_request_and_token(field, value):
    async with reliable_factory("postgres", notifications=True) as reliable:
        import psycopg
        from psycopg import sql

        h = NotificationHarness(reliable)
        outbox, _ = await prepare(h)
        _, attempt = await reserve(h, outbox)
        async with reliable.blobs.pool.connection() as conn:
            with pytest.raises(psycopg.Error) as caught:
                await conn.execute(
                    sql.SQL(
                        "UPDATE reporting_webhook_attempts SET {} = {}"
                        " WHERE account_id=%s AND principal_id=%s"
                    ).format(sql.Identifier(field), sql.SQL(value)),
                    ("acct_a", "buyer"),
                )
            await conn.rollback()
            assert "RAW_SECRET" not in str(caught.value)
        assert await outbox.list_activity(account_id="acct_a", consumer_id="buyer") == (attempt,)


async def test_pending_orphans_are_retained_without_parent_and_db_clock_cannot_be_overridden():
    async with reliable_factory("postgres", notifications=True) as reliable:
        h = NotificationHarness(reliable)
        _, _ = await prepare(h)
        outbox = PgReportingOutbox(pool=reliable.blobs.pool)  # Real database clock.
        lease = await outbox.claim_delivery(account_id="acct_a", now=NOW, lease_seconds=60)
        attempt = await outbox.reserve_attempt(
            lease, request=ActivityRequest("https://example.test/webhooks", 1), now=NOW
        )
        assert attempt is not None and attempt.fired_at > NOW
        async with reliable.blobs.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM reporting_notification_deliveries"
                " WHERE account_id=%s AND principal_id=%s",
                ("acct_a", "buyer"),
            )
        purged_count_2 = await outbox.purge_activity(
            account_id="acct_a", consumer_id="buyer", now=NOW + timedelta(days=900)
        )
        assert purged_count_2 == 0
        completed_attempt_3 = await outbox.complete_attempt(
            attempt, outcome=ActivityOutcome("connection_error"), now=NOW
        )
        assert completed_attempt_3
        completed = (await outbox.list_activity(account_id="acct_a", consumer_id="buyer"))[0]
        assert completed.completed_at >= attempt.fired_at
        purged_count_3 = await outbox.purge_activity(
            account_id="acct_a", consumer_id="buyer", now=NOW + timedelta(days=900)
        )
        assert purged_count_3 == 0


@pytest.fixture(scope="module")
def actual_a_source(tmp_path_factory):
    require_rolling_database()
    target = tmp_path_factory.mktemp("reporting-1168a-source") / "worktree"
    checkout = subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), BASE],
        cwd=ROOT,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert (
        checkout.returncode == 0
    ), "rolling gate requires reviewed A commit; checkout with fetch-depth: 0"
    try:
        yield target
    finally:
        removed = subprocess.run(
            ["git", "worktree", "remove", "--force", str(target)],
            cwd=ROOT,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert removed.returncode == 0, "task-owned A worktree cleanup failed"


A_SCRIPT = r"""
import asyncio, hashlib, json, sys
from datetime import datetime, timedelta
from pathlib import Path
values = json.load(sys.stdin)
sys.path.insert(0, str(Path.cwd() / 'src'))
from psycopg_pool import AsyncConnectionPool
from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.outbox import (
    PgReportingOutbox, ReportingEnvelopeCipher, ReportingNotificationSubscription,
)
from adcp.reporting.outbox._schema import validate_schema
import adcp.reporting.outbox.worker as worker_module
assert Path(worker_module.__file__).is_relative_to(Path.cwd())
assert 'activity' not in __import__('inspect').signature(
    worker_module.ReportingNotificationWorker
).parameters
async def main():
    clock = lambda: datetime.fromisoformat(values['now'])
    async with AsyncConnectionPool(values['conninfo'], kwargs=values['kwargs'], open=False) as pool:
        ledger = PgReportingLedgerStore(pool=pool, notifications=True, clock=clock)
        await ledger.create_schema()
        async with pool.connection() as conn:
            await validate_schema(conn)
        if values['action'] == 'bootstrap':
            return
        from adcp.reporting.ledger.notification_models import decode_event
        from adcp.reporting.outbox import ReportingSigningMaterial
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        subscription = ReportingNotificationSubscription(**values['subscription'])
        class Subscriptions:
            async def list_active(self, **kwargs): return (subscription,)
            async def get_active(self, **kwargs): return subscription
        class Signing:
            async def resolve(self, **kwargs):
                return ReportingSigningMaterial(Ed25519PrivateKey.from_private_bytes(b'\x01'*32),
                    'https://seller.example.test/keys#key-1', 'ed25519', frozenset({'ed25519'}))
        outbox = PgReportingOutbox(pool=pool, clock=clock)
        cipher = ReportingEnvelopeCipher(b'e'*32)
        worker = worker_module.ReportingNotificationWorker(
            outbox=outbox, subscriptions=Subscriptions(), cipher=cipher,
            signing=Signing(), clock=clock,
        )
        advertised = await worker.advertised_notifications(ledger, account_id='acct_a')
        assert advertised['supports_webhook_activity'] is False
        expanded_1 = await worker.expand_one(account_id='acct_a')
        assert expanded_1
        lease = await outbox.claim_delivery(account_id='acct_a', now=clock(), lease_seconds=60)
        opened = cipher.open(lease.delivery)
        finished_delivery_1 = await outbox.finish_delivery(
            lease, now=clock(), state='pending', retry_at=clock()
        )
        assert finished_delivery_1
        print(json.dumps({'body_sha256': hashlib.sha256(opened.prepared.body).hexdigest(),
                          'idempotency_key': opened.prepared.idempotency_key}))
asyncio.run(asyncio.wait_for(main(), 30))
"""


async def run_a(source, pool, *, action):
    return await asyncio.to_thread(
        run_step,
        [sys.executable, "-c", A_SCRIPT],
        label=f"actual-a-{action}",
        cwd=source,
        timeout=45,
        value={
            "conninfo": pool.conninfo,
            "kwargs": pool.kwargs,
            "now": NOW.isoformat(),
            "action": action,
            "subscription": asdict(notification_subscription()),
        },
    )


async def test_actual_a_binary_on_b_database_keeps_readiness_and_delivery_identity(actual_a_source):
    async with reliable_factory("postgres", notifications=True) as reliable:
        h = NotificationHarness(reliable)
        from .test_reporting_notification_outbox import seed

        await seed(h)
        result = json.loads(await run_a(actual_a_source, reliable.blobs.pool, action="roundtrip"))
        outbox = h.outbox
        lease, attempt = await reserve(h, outbox)
        assert lease.attempt_count == 2 and attempt.attempt == 1
        assert attempt.binding.body_sha256 == result["body_sha256"]
        assert attempt.binding.idempotency_key == result["idempotency_key"]
        completed_attempt_4 = await outbox.complete_attempt(
            attempt, outcome=ActivityOutcome("success", 200, 1), now=reliable.clock()
        )
        assert completed_attempt_4
        finished_delivery_2 = await outbox.finish_delivery(
            lease, state="complete", now=reliable.clock()
        )
        assert finished_delivery_2
        async with reliable.blobs.pool.connection() as conn:
            await validate_schema(conn, activity=True)


async def test_b_binary_on_actual_a_schema_refuses_activity_without_corrupting_work(
    actual_a_source,
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        await run_a(actual_a_source, pool, action="bootstrap")
        async with pool.connection() as conn:
            await validate_schema(conn)
            with pytest.raises(ReportingNotificationError, match="missing"):
                await validate_schema(conn, activity=True)
            assert (
                await (
                    await conn.execute("SELECT to_regclass('reporting_webhook_attempts')")
                ).fetchone()
            )[0] is None
        ledger = PgReportingLedgerStore(pool=pool, clock=lambda: NOW, notifications=True)
        config = configuration()
        obligation = obligation_for(config)
        revision, data = revision_for(obligation)
        await ledger.put_configuration(config)
        await ledger.commit_obligation(obligation)
        await ledger.commit_revision(revision, data)
        await run_a(actual_a_source, pool, action="roundtrip")
        outbox = PgReportingOutbox(pool=pool, clock=lambda: NOW)
        lease = await outbox.claim_delivery(account_id="acct_a", now=NOW, lease_seconds=60)
        with pytest.raises(ReportingNotificationError, match="activity_store_unavailable"):
            await outbox.reserve_attempt(
                lease, request=ActivityRequest("https://example.test/reporting", 1), now=NOW
            )
        assert await outbox.delivery_lease_current(lease, now=NOW)
        assert len(await outbox.list_events(account_id="acct_a")) == 1
        before = (await outbox.list_deliveries(account_id="acct_a"))[0]
        await ledger.create_schema()
        assert (await outbox.list_deliveries(account_id="acct_a"))[0] == before
        reserved_attempt_1 = await outbox.reserve_attempt(
            lease, request=ActivityRequest("https://example.test/reporting", 1), now=NOW
        )
        assert reserved_attempt_1 is not None
