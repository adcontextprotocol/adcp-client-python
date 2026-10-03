"""C-only catalog/data retention and competing, independently imported A/B workers."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.ledger import ReportingStatusCaller as OwnershipCaller
from adcp.reporting.ledger.status_projection import lifecycle_intents, mismatch_key
from adcp.reporting.outbox import (
    PgReportingOutbox,
    PgStatusNotificationStore,
    ReportingNotificationError,
)
from adcp.reporting.outbox._schema import REQUIRED_OBJECTS, schema_objects, validate_schema
from adcp.reporting.outbox.status_schema import (
    REQUIRED_STATUS_OBJECTS,
    REQUIRED_STATUS_SELECTOR_OBJECTS,
    validate_status_schema,
)

from . import test_reporting_notification_process_matrix as _process
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
        status_operation_1 = await ledger.ensure_issue_opened(
            account_id="acct_a",
            consumer_id="buyer",
            issue_key=opened.issue_key,
            observed_at=NOW,
        )
        assert status_operation_1 == opened
        waived = await ledger.set_issue_state(
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
        assert await ledger.get_issue(account_id="acct_a", issue_key=opened.issue_key) == waived
        recurring = replace(
            first,
            reporting_status_id="recurring",
            supersedes_reporting_status_id=agreeing.reporting_status_id,
        )
        await ledger.record_consumer_status(recurring)
        snapshot = await ledger.read_status_snapshot(caller=OwnershipCaller("acct_a", "buyer"))
        assert not lifecycle_intents(snapshot)
        current = next(i for i in snapshot.lifecycles if i.issue_state == "open")
        assert (current.issue_key, current.generation) != (opened.issue_key, opened.generation)
        assert current.issue_id != opened.issue_id
        assert waived in snapshot.lifecycles
        assert dict(snapshot.issue_scopes)[current.issue_id].generation_key == config.generation_key
        enabled = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW, notifications=True)
        with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
            await enabled.read_status_snapshot(caller=OwnershipCaller("acct_a", "buyer"))
        before = await retained_physical_rows(pool)
        status = PgStatusNotificationStore(enabled)
        await status.create_schema()
        assert await retained_physical_rows(pool) == before
        await status.baseline(account_id="acct_a")
        assert await status.outbox.list_events(account_id="acct_a") == ()
        repaired = await enabled.read_status_snapshot(caller=OwnershipCaller("acct_a", "buyer"))
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
                assert {k: v for k, v in objects.items() if k not in REQUIRED_OBJECTS} == {
                    **REQUIRED_STATUS_OBJECTS,
                    **REQUIRED_STATUS_SELECTOR_OBJECTS,
                    **PROVISIONAL_OBJECTS,
                }
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
            status_operation_3 = await status.baseline(account_id="acct_a")
            assert status_operation_3
            status_operation_4 = await status.baseline(account_id="acct_a")
            assert not status_operation_4
            assert await status.baseline_ready(account_id="acct_a")
            assert not await status.outbox.list_events(account_id="acct_a")


@pytest.mark.parametrize("release", ["a", "b"])
@case_deadline
async def test_actual_unowned_artifact_requires_stopped_worker_migration(release, actual_sources):
    from psycopg import sql

    from adcp.reporting.migration import (
        ReportingOwnershipMigrationError,
        migrate_legacy_reporting,
    )

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
            # The old worker is explicitly stopped before moving retained state.
            await child.send(action="stop")
            await child.event("done")
            await child.finish()
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True)
        with pytest.raises(ReportingOwnershipMigrationError, match="Stop reporting workers"):
            await ledger.create_schema()
        async with pool.connection() as conn:
            with pytest.raises(ReportingNotificationError, match="consumer_id"):
                await validate_schema(conn)
            archive = "adcp_reporting_quarantine_status_" + release
            await migrate_legacy_reporting(conn, archive_schema=archive, workers_stopped=True)
            try:
                assert (
                    await (
                        await conn.execute(
                            sql.SQL("SELECT count(*) FROM {}.reporting_configurations").format(
                                sql.Identifier(archive)
                            )
                        )
                    ).fetchone()
                )[0] == 0
                await validate_schema(conn)
                await validate_status_schema(conn)
                assert (
                    await ledger.list_configurations(caller=OwnershipCaller("acct_a", "__legacy__"))
                    == ()
                )
                assert await PgReportingOutbox(pool=pool).list_events(account_id="acct_a") == ()
            finally:
                await conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(archive))
                )


@pytest.mark.parametrize("release", ["a", "b"])
@case_deadline
async def test_actual_old_workers_refuse_owned_schema_without_touching_rows(
    release, actual_sources
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        await seed(pool)
        before = await retained_physical_rows(pool)
        async with service_process(
            pool,
            f"old_{release}",
            old_source=str(actual_sources[release]),
            source_sha=ARTIFACTS[release],
        ) as child:
            failed = await child.event("failed")
            assert failed["classification"] == "RaiseException"
        assert await retained_physical_rows(pool) == before


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
