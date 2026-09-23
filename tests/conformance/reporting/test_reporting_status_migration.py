"""C-only catalog/data retention and competing, independently imported A/B workers."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

import pytest

from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.ledger.status_projection import lifecycle_intents, mismatch_key
from adcp.reporting.outbox import (
    ActivityRequest,
    PgReportingActivityUnionStore,
    PgReportingOutbox,
    PgStatusNotificationStore,
    ReportingEnvelopeCipher,
    ReportingNotificationError,
    ReportingNotificationWorker,
)
from adcp.reporting.outbox._schema import REQUIRED_OBJECTS, schema_objects, validate_schema
from adcp.reporting.outbox.status_schema import REQUIRED_STATUS_OBJECTS, validate_status_schema

from . import test_reporting_notification_process_matrix as _process
from ._generation_support import (
    NOW,
    configuration,
    isolated_reporting_pool,
    obligation_for,
    require_rolling_database,
    revision_for,
)
from ._reliable_support import (
    Barrier,
    FailurePlan,
    ScriptedSigning,
    ScriptedSubscriptions,
    notification_subscription,
    service_process,
)
from .test_reporting_notification_migration import foundation, retained_physical_rows
from .test_reporting_notification_outbox import statement

ROOT = Path(__file__).resolve().parents[3]
case_deadline = _process.case_deadline
certificate = _process.certificate
ARTIFACTS = {
    "a": "17ee407ae3978c8a2bb54437287afbf9dafb8130",
    "b": "0f34c666ac1961e9832fce43ef0ef6937b3c1dde",
}
SQL = files("adcp.reporting.ledger").joinpath("reporting_status_notifications.sql").read_text()
C_QUEUES = (
    "reporting_status_notification_events",
    "reporting_status_notification_expansions",
    "reporting_status_notification_deliveries",
    "reporting_status_webhook_attempt_heads",
    "reporting_status_webhook_attempts",
)


@pytest.fixture(scope="module")
def actual_sources(tmp_path_factory):
    require_rolling_database()
    targets = {}
    try:
        for release, sha in ARTIFACTS.items():
            target = tmp_path_factory.mktemp(f"status-{release}-artifact") / "worktree"
            result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(target), sha],
                cwd=ROOT,
                capture_output=True,
                timeout=60,
                check=False,
            )
            assert (
                result.returncode == 0
            ), "rolling gate requires exact reviewed A/B history; fetch-depth: 0"
            targets[release] = target
        yield targets
    finally:
        for target in targets.values():
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(target)],
                cwd=ROOT,
                capture_output=True,
                timeout=60,
                check=False,
            )
            assert result.returncode == 0, "task-owned compatibility worktree cleanup failed"


async def physical_rows(pool, tables=C_QUEUES):
    from psycopg import sql

    result = {}
    async with pool.connection() as conn:
        for table in tables:
            result[table] = await (
                await conn.execute(
                    sql.SQL(
                        "SELECT t.ctid::text, t.xmin::text, to_jsonb(t) FROM {} t ORDER BY t.ctid"
                    ).format(sql.Identifier(table))
                )
            ).fetchall()
    return result


async def seed(pool):
    ledger = PgReportingReconciliationStore(pool=pool, notifications=True)
    await ledger.create_schema()  # A/B only, before C migration.
    config = configuration()
    await ledger.put_configuration(config)
    obligation = await ledger.commit_obligation(obligation_for(config))
    first, rows = revision_for(obligation)
    await ledger.commit_revision(first, rows)
    second, rows = revision_for(obligation, suffix="second")
    second = replace(second, supersedes_reporting_revision_id=first.reporting_revision_id)
    await ledger.commit_revision(second, rows)
    third, rows = revision_for(obligation, suffix="third")
    third = replace(third, supersedes_reporting_revision_id=second.reporting_revision_id)
    await ledger.commit_revision(third, rows)
    return ledger, third


@pytest.mark.parametrize("autocommit", [False, True])
async def test_default_off_pre_outbox_lifecycle_remains_usable_until_scope_migration(autocommit):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        await foundation(pool)
        ledger = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        config = configuration()
        await ledger.put_configuration(config)
        obligation = await ledger.commit_obligation(obligation_for(config))
        revision, rows = revision_for(obligation)
        await ledger.commit_revision(revision, rows)
        first = replace(
            statement(obligation),
            consumer_status="unreadable",
            reporting_revision_id=revision.reporting_revision_id,
        )
        await ledger.record_consumer_status(first)
        opened = await ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(first))
        assert opened is not None and opened.opened_at == NOW
        assert (
            await ledger.ensure_issue_opened(
                account_id="acct_a",
                consumer_id="buyer",
                issue_key=opened.issue_key,
                observed_at=NOW,
            )
            == opened
        )
        await ledger.set_issue_state(
            account_id="acct_a", issue_key=opened.issue_key, state="waived", at=NOW
        )
        agreeing = replace(
            first,
            reporting_status_id="agreeing",
            supersedes_reporting_status_id=first.reporting_status_id,
            consumer_status="received",
            reporting_revision_id=revision.reporting_revision_id,
        )
        await ledger.record_consumer_status(agreeing)
        assert await ledger.get_issue(account_id="acct_a", issue_key=opened.issue_key) is None
        recurring = replace(
            first,
            reporting_status_id="recurring",
            supersedes_reporting_status_id=agreeing.reporting_status_id,
        )
        await ledger.record_consumer_status(recurring)
        snapshot = await ledger.read_status_snapshot(account_id="acct_a")
        assert not lifecycle_intents(snapshot)
        current = await ledger.get_issue(account_id="acct_a", issue_key=opened.issue_key)
        assert current is not None and current.generation == opened.generation + 1
        assert current.issue_id != opened.issue_id
        assert dict(snapshot.issue_scopes)[current.issue_id].generation_key == config.generation_key
        enabled = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW, notifications=True)
        with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
            await enabled.read_status_snapshot(account_id="acct_a")
        before = await retained_physical_rows(pool)
        status = PgStatusNotificationStore(enabled)
        await status.create_schema()
        assert await retained_physical_rows(pool) == before
        await status.baseline(account_id="acct_a")
        assert await status.outbox.list_events(account_id="acct_a") == ()
        repaired = await enabled.read_status_snapshot(account_id="acct_a")
        assert (
            dict(repaired.issue_scopes)[current.issue_id]
            == dict(snapshot.issue_scopes)[current.issue_id]
        )


@pytest.mark.parametrize("autocommit", [False, True])
async def test_populated_repeated_c_manifest_preserves_every_a_b_object_and_row(autocommit):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        ledger, revision = await seed(pool)
        before = await retained_physical_rows(pool)
        status = PgStatusNotificationStore(ledger)
        for _ in range(2):
            await status.create_schema()
            assert await retained_physical_rows(pool) == before
            async with pool.connection() as conn:
                objects = await schema_objects(conn)
                assert {k: objects[k] for k in REQUIRED_OBJECTS} == REQUIRED_OBJECTS
                assert {
                    k: v for k, v in objects.items() if k not in REQUIRED_OBJECTS
                } == REQUIRED_STATUS_OBJECTS
                assert (
                    json.dumps(REQUIRED_STATUS_OBJECTS, sort_keys=True, indent=2) + "\n"
                    == files("adcp.reporting.outbox")
                    .joinpath("required_status_schema.json")
                    .read_text()
                )
                await validate_schema(conn, activity=True)
                await validate_status_schema(conn, activity=True)
        await status.baseline(account_id="acct_a")
        await ledger.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=False,
        )
        await status.project_one(account_id="acct_a")
        populated = await physical_rows(pool)
        await status.create_schema()
        await ledger.create_schema()
        assert await physical_rows(pool) == populated


async def test_concurrent_interrupted_c_migration_is_atomic_and_baseline_is_restartable():
    pytest.importorskip("psycopg_pool")
    from psycopg_pool import AsyncConnectionPool

    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger, _ = await seed(pool)
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
                        await conn.execute("SELECT to_regclass('reporting_status_accounts')")
                    ).fetchone()
                )[0] is None
                with pytest.raises(
                    ReportingNotificationError, match="status_schema_unready:missing"
                ):
                    await validate_status_schema(conn)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            async def migrate(selected):
                await PgStatusNotificationStore(
                    PgReportingReconciliationStore(pool=selected, notifications=True)
                ).create_schema()

            await asyncio.wait_for(asyncio.gather(*(migrate(p) for p in (pool, observer) * 2)), 25)
            status = PgStatusNotificationStore(ledger)
            assert not await status.baseline_ready(account_id="acct_a")
            assert await status.baseline(account_id="acct_a")
            assert not await status.baseline(account_id="acct_a")
            assert await status.baseline_ready(account_id="acct_a")
            assert not await status.outbox.list_events(account_id="acct_a")


@pytest.mark.parametrize("release", ["a", "b"])
@case_deadline
async def test_c_on_actual_unmigrated_artifact_suppresses_status_only(release, actual_sources):
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with service_process(
            pool,
            f"old_{release}",
            old_source=str(actual_sources[release]),
            source_sha=ARTIFACTS[release],
        ) as child:
            ready = await child.event("old_ready")
            assert ready["source_sha"] == ARTIFACTS[release]
            assert Path(ready["module_origin"]).is_relative_to(actual_sources[release])
            assert ready["classification"] == "notifications_ready"
            status = PgStatusNotificationStore(
                PgReportingReconciliationStore(pool=pool, notifications=True)
            )
            with pytest.raises(ReportingNotificationError, match="status_schema_unready:missing"):
                await status.baseline_ready(account_id="acct_a")
            async with pool.connection() as conn:
                await validate_schema(conn)
                with pytest.raises(
                    ReportingNotificationError, match="status_schema_unready:missing"
                ):
                    await validate_status_schema(conn)
            await child.send(action="stop")
            await child.event("done")
            await child.finish()


@case_deadline
async def test_baseline_repairs_actual_b_status_without_issue_using_ingest_observation(
    actual_sources,
):
    from adcp.reporting.ledger.status_projection import mismatch_key

    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger, revision = await seed(pool)
        async with service_process(
            pool, "old_b", old_source=str(actual_sources["b"]), source_sha=ARTIFACTS["b"]
        ) as child:
            await child.event("old_ready")
            await child.send(action="orphan", revision_id=revision.reporting_revision_id)
            await child.event("old_orphan")
            await child.send(action="stop")
            await child.event("done")
            await child.finish()
        (record,) = await ledger.list_consumer_statuses(account_id="acct_a", consumer_id="buyer")
        assert await ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(record)) is None
        status = PgStatusNotificationStore(ledger)
        await status.create_schema()
        await status.baseline(account_id="acct_a")
        issue = await ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(record))
        assert issue is not None and issue.opened_at == record.recorded_at
        assert not await status.outbox.list_events(account_id="acct_a")
        private = [
            c
            for c in await status.checkpoints(account_id="acct_a")
            if c.scope.consumer_id == "buyer"
        ]
        assert len(private) == 2 and all(c.snapshot["health"] == "action_required" for c in private)
        assert all(
            c.snapshot["issues"][0]["opened_at"] == record.recorded_at.isoformat() for c in private
        )
        assert not (await status.project_one(account_id="acct_a")).did_work


@case_deadline
async def test_live_reviewed_a_b_workers_never_claim_or_touch_pending_c_queues(
    actual_sources, certificate
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger, revision = await seed(pool)
        status = PgStatusNotificationStore(ledger)
        async with service_process(
            pool, "status_receiver", deadlines={"receiver_control": 80}, **certificate
        ) as receiver:
            port = (await receiver.event("listening"))["port"]
            network = {**certificate, "receiver_port": port}
            async with (
                service_process(
                    pool,
                    "old_a",
                    old_source=str(actual_sources["a"]),
                    source_sha=ARTIFACTS["a"],
                    **network,
                ) as old_a,
                service_process(
                    pool,
                    "old_b",
                    old_source=str(actual_sources["b"]),
                    source_sha=ARTIFACTS["b"],
                    **network,
                ) as old_b,
            ):
                for release, child in (("a", old_a), ("b", old_b)):
                    ready = await child.event("old_ready")
                    assert ready["source_sha"] == ARTIFACTS[release]
                    assert Path(ready["module_origin"]).is_relative_to(actual_sources[release])
                    assert ready["classification"] == "notifications_ready"
                    print(
                        f"status_rolling artifact={release} sha={ready['source_sha']}"
                        f" module_origin={ready['module_origin']} verified=True",
                        flush=True,
                    )
                # Real B expansion/delivery runs concurrently with C migration.
                # A is already running too, so both old pools span the upgrade.
                await old_b.send(action="turn")
                await status.create_schema()
                assert (await old_b.event("old_turn"))["did_work"]
                await receiver.event("http_accepted")
                await status.baseline(account_id="acct_a")
                await ledger.set_revision_readable(
                    account_id="acct_a",
                    reporting_revision_id=revision.reporting_revision_id,
                    readable=False,
                )
                assert (await status.project_one(account_id="acct_a")).events == 2
                failures = FailurePlan()
                subscriptions = ScriptedSubscriptions(failures)
                subscriptions.put(notification_subscription(events=("reporting.status_changed",)))
                worker = ReportingNotificationWorker(
                    outbox=status.outbox,
                    subscriptions=subscriptions,
                    signing=ScriptedSigning(failures),
                    cipher=ReportingEnvelopeCipher(b"e" * 32),
                    activity=status.outbox,
                )
                while await worker.expand_one(account_id="acct_a"):
                    pass
                now = datetime.now(timezone.utc)
                lease = await status.outbox.claim_delivery(
                    account_id="acct_a", now=now, lease_seconds=60
                )
                assert lease is not None
                assert await status.outbox.reserve_attempt(
                    lease,
                    request=ActivityRequest("https://receiver.example.test/reporting", 1),
                    now=now,
                )
                assert await status.outbox.finish_delivery(
                    lease, state="pending", retry_at=now, now=now
                )
                pending = await physical_rows(pool)
                assert all(pending.values())  # Include attempt and retained head rows.
                # Guarantee the actual A decoder and HTTP sender also execute,
                # then let both old processes compete for the remaining event.
                await old_a.send(action="turn")
                assert (await old_a.event("old_turn"))["did_work"]
                assert await physical_rows(pool) == pending
                await old_a.send(action="core_write")
                await old_a.event("old_core_write")
                assert len(await ledger.list_configurations(account_id="legacy-core-only")) == 1
                assert await physical_rows(pool) == pending
                for _ in range(4):
                    await asyncio.gather(old_a.send(action="turn"), old_b.send(action="turn"))
                    turns = await asyncio.gather(old_a.event("old_turn"), old_b.event("old_turn"))
                    assert await physical_rows(pool) == pending
                    if not any(turn["did_work"] for turn in turns):
                        break
                else:
                    pytest.fail("reviewed A/B queues did not converge")
                await receiver.event("http_accepted")  # Actual A delivery.
                await receiver.event("http_accepted")  # Remaining contested A/B event.
                old_outbox = PgReportingOutbox(pool=pool)
                assert {
                    row.state for row in await old_outbox.list_deliveries(account_id="acct_a")
                } == {"complete"}
                async with pool.connection() as conn:
                    catalog = {
                        k: v
                        for k, v in (await schema_objects(conn)).items()
                        if k in REQUIRED_STATUS_OBJECTS
                    }
                for release, child in (("a", old_a), ("b", old_b)):
                    await child.send(action="schema")
                    result = await child.event("old_schema")
                    assert result["classification"] == (
                        "a_notifications_closed" if release == "a" else "notifications_ready"
                    )
                    assert await physical_rows(pool) == pending
                    async with pool.connection() as conn:
                        assert {
                            k: v
                            for k, v in (await schema_objects(conn)).items()
                            if k in REQUIRED_STATUS_OBJECTS
                        } == catalog
                        await validate_status_schema(conn, activity=True)
                await old_b.send(action="write", revision_id=revision.reporting_revision_id)
                await old_b.event("old_write")
                assert await physical_rows(pool) == pending
                async with pool.connection() as conn:
                    boundaries = await (
                        await conn.execute(
                            "SELECT through-first_sequence+1 FROM reporting_status_boundaries"
                            " ORDER BY first_sequence"
                        )
                    ).fetchall()
                    assert [r[0] for r in boundaries] == [1, 2, 1, 1]
                for child in (old_a, old_b):
                    await child.send(action="stop")
                    await child.event("done")
                    await child.finish()
            # A fresh C process replays the old writer's grouped, captured input.
            async with service_process(pool, "status_projector", turns=3) as child:
                assert (await child.event("done"))["did_work"]
                await child.finish()
            status = PgStatusNotificationStore(ledger)
            assert not (await status.project_one(account_id="acct_a")).did_work
            events = await status.outbox.list_events(account_id="acct_a")
            assert len(events) == 6 and {e.cause_generation for e in events} == {1, 2, 3}
            assert (
                await status.outbox.reemit(
                    account_id="acct_a",
                    consumer_namespace="",
                    notification_id=events[0].notification_id,
                    now=datetime.now(timezone.utc),
                )
                == 2
            )
            async with service_process(pool, "status_http_worker", **network) as child:
                assert (await child.event("done"))["did_work"]
                await child.finish()
            deliveries = await status.outbox.list_deliveries(account_id="acct_a")
            assert len(deliveries) == 7 and {r.state for r in deliveries} == {"complete"}
            for _ in deliveries:
                await receiver.event("http_accepted")
            union = PgReportingActivityUnionStore(old_outbox, status.outbox)
            activity = await union.list_activity(account_id="acct_a", consumer_id="buyer")
            assert {a.binding.notification_type for a in activity} == {
                "reporting.ledger_changed",
                "reporting.status_changed",
            }
            assert (
                len(
                    [
                        a
                        for a in activity
                        if a.outcome is not None
                        and a.outcome.status == "success"
                        and a.binding.notification_type == "reporting.status_changed"
                    ]
                )
                == 7
            )
            assert len({r.delivery.binding.idempotency_key for r in deliveries}) == 7
            assert await status.outbox.list_events(account_id="acct_a") == events
            await receiver.send(stop=True)
            await receiver.finish()


def test_required_manifests_stay_per_object_so_readiness_ignores_the_locale():
    """No manifest entry may aggregate several catalog rows in sort order.

    Earlier A snapshots sorted aggregate constraint rows under the database
    locale; the integrated A artifact pins that order to C. B and C instead
    hash named objects individually, so cross-row order cannot affect a digest.
    Keep that structure without requiring a particular deployment locale.
    """
    prefixes = ("table:", "column:", "constraint:", "index:", "trigger:", "function:")
    for name, manifest in (
        ("required_schema.json", REQUIRED_OBJECTS),
        ("required_status_schema.json", REQUIRED_STATUS_OBJECTS),
    ):
        assert manifest, name
        for key, value in manifest.items():
            assert key.startswith(prefixes), (name, key)
            assert set(value) >= {"fingerprint", "enabled"}, (name, key)


async def test_status_activity_union_requires_positional_column_parity():
    """``SELECT *`` UNION ALL maps B and C attempts by position, not by name.

    ``PgReportingActivityUnionStore`` unions both histories with ``SELECT *``,
    so the result takes B's column *names* and C's values *positionally*. A
    reordered or inserted column in either table would silently swap
    same-typed fields -- ``lease_token``/``reservation_token``,
    ``notification_id``/``idempotency_key`` -- in every projected attempt,
    with no error anywhere.
    """
    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True)
        await ledger.create_schema()
        await PgStatusNotificationStore(ledger).create_schema()
        layout = {}
        async with pool.connection() as conn:
            for table in ("reporting_webhook_attempts", "reporting_status_webhook_attempts"):
                layout[table] = await (
                    await conn.execute(
                        "SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod)"
                        " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
                        " JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = current_schema() AND c.relname = %s"
                        " AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
                        (table,),
                    )
                ).fetchall()
        assert layout["reporting_webhook_attempts"] == (layout["reporting_status_webhook_attempts"])
        assert len(layout["reporting_webhook_attempts"]) == 18
