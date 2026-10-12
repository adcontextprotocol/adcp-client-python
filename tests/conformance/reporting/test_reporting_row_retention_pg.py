"""Intent sweeps and expired-period row pruning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore, ReportingRowStoreError
from adcp.reporting.ledger.row_storage_object import (
    ReportingRowObjectContextV1,
    ReportingRowObjectPutInputV1,
    create_filesystem_reporting_row_object_provider_v1,
)
from adcp.reporting.ledger.row_write import prepare_revision_row_set
from adcp.reporting.ledger.store import LedgerConflictError

from ._generation_support import isolated_reporting_pool
from .test_reporting_row_storage_object_pg import lease, list_objects, options
from .test_reporting_row_storage_pg import fetch_all, many_rows, seeded, typed_revision, walk


async def staged_revision(
    store: PgReportingLedgerStore,
    obligation: Any,
    suffix: str,
    rows: list[dict[str, Any]],
    **kwargs: Any,
) -> Any:
    revision = typed_revision(obligation, rows, suffix=suffix, **kwargs)
    assert await store.stage_revision_rows(revision, rows) is True
    return revision


async def expire_intents(pool: Any) -> None:
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE adcp_reporting_row_write_intents"
            " SET expires_at = clock_timestamp() - INTERVAL '1 second'"
        )


async def test_a_sweep_deletes_only_the_objects_an_abandoned_upload_created(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(10)
        await staged_revision(store, obligation, "abandoned", rows)
        assert len(list_objects(tmp_path / "rows")) == 1

        # A live intent is nobody's to sweep yet.
        idle = await store.sweep_row_write_intents()
        assert (idle.swept, idle.objects_deleted, idle.failed) == (0, 0, 0)
        assert len(list_objects(tmp_path / "rows")) == 1

        await expire_intents(pool)
        result = await store.sweep_row_write_intents()
        assert (result.swept, result.objects_deleted, result.failed) == (1, 1, 0)
        assert list_objects(tmp_path / "rows") == []
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_write_intents") == [
            (0,)
        ]


async def test_a_sweep_never_deletes_adopted_or_committed_objects(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)

        # An adopted object is not recorded as created, so the sweep leaves it alone.
        rows = many_rows(10)
        revision = typed_revision(obligation, rows, suffix="adopted")
        prepared = await prepare_revision_row_set(
            store,
            revision,
            rows,
            period_start="2026-09-01T00:00:00Z",
            source_timezone=obligation.period.source_timezone,
        )
        provider = create_filesystem_reporting_row_object_provider_v1(
            roots={"rows": tmp_path / "rows"}
        )
        await provider.put_if_absent(
            ReportingRowObjectPutInputV1(
                {"root": "rows"},
                prepared.objects[0].object_ref,
                prepared.objects[0].physical_bytes,
                "application/gzip",
                {},
            ),
            ReportingRowObjectContextV1(),
        )
        await store.stage_revision_rows(revision, rows)
        await expire_intents(pool)
        result = await store.sweep_row_write_intents()
        assert (result.swept, result.objects_deleted) == (1, 0)
        assert len(list_objects(tmp_path / "rows")) == 1

        # An intent whose chunk was committed (a crash after commit, before cleanup) is
        # swept without touching the object a header now references.
        other = many_rows(11)
        committed = await staged_revision(store, obligation, "committed", other)
        await store.commit_revision(committed, other)
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_row_write_intents (row_binding_id, account_id,"
                " row_set_id, content_sha256, state, owner_token, created_objects, expires_at)"
                " SELECT 'reports', account_id, 'rpr_ghost', %s, 'open', 'ghost',"
                "   jsonb_build_array(jsonb_build_object('object_ref', object_ref,"
                "     'native_version_ref', native_version_ref)),"
                "   clock_timestamp() - INTERVAL '1 second'"
                " FROM adcp_reporting_row_chunks WHERE row_set_id = %s",
                ("f" * 64, committed.reporting_revision_id),
            )
        before = list_objects(tmp_path / "rows")
        result = await store.sweep_row_write_intents()
        assert (result.swept, result.objects_deleted) == (1, 0)
        assert list_objects(tmp_path / "rows") == before
        walked, _ = await walk(store, obligation.account_id, committed.reporting_revision_id, 500)
        assert walked == other


async def test_an_interrupted_sweep_resumes_and_a_swept_intent_blocks_its_commit(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(10)
        revision = await staged_revision(store, obligation, "resume", rows)
        # A sweep that crashed after claiming the intent but before deleting anything.
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents SET state = 'sweeping'"
            )
        with pytest.raises(ReportingRowStoreError):
            await store.commit_revision(revision, rows)
        result = await store.sweep_row_write_intents()
        assert (result.swept, result.objects_deleted, result.failed) == (1, 1, 0)
        assert list_objects(tmp_path / "rows") == []


async def test_a_sweep_that_cannot_delete_keeps_the_intent_for_the_next_pass(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        configured = options(tmp_path)
        store = PgReportingLedgerStore(pool=pool, row_storage=configured)
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        await staged_revision(store, obligation, "stuck", many_rows(10))
        await expire_intents(pool)
        # A host that no longer configures the binding cannot delete: the intent stays.
        without = PgReportingLedgerStore(
            pool=pool,
            row_storage=type(configured)(deployment_namespace="test"),
        )
        result = await without.sweep_row_write_intents()
        # Intents of bindings this host does not configure are skipped, so they can never
        # starve the intents it can delete.
        assert (result.swept, result.failed) == (0, 0)
        assert result.skipped_unconfigured == 1
        assert await fetch_all(pool, "SELECT state FROM adcp_reporting_row_write_intents") == [
            ("open",)
        ]
        again = await store.sweep_row_write_intents()
        assert (again.swept, again.objects_deleted) == (1, 1)
        assert list_objects(tmp_path / "rows") == []


async def age(pool: Any, *, days: int) -> None:
    """Move every recorded instant ``days`` into the past, as time passing would."""
    async with pool.connection() as connection:
        for table in ("reporting_revisions", "reporting_adjustments"):
            await connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")  # noqa: S608
            await connection.execute(
                f"UPDATE {table} SET created_at = created_at - make_interval(days => %s)",  # noqa: S608
                (days,),
            )
            await connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")  # noqa: S608
        await connection.execute("ALTER TABLE reporting_obligations DISABLE TRIGGER USER")
        await connection.execute(
            "UPDATE reporting_obligations SET period_end = period_end - make_interval(days => %s),"
            " period_start = period_start - make_interval(days => %s),"
            " scope_resolved_at = scope_resolved_at - make_interval(days => %s),"
            " expected_at = expected_at - make_interval(days => %s),"
            " automated_recovery_deadline_at ="
            "   automated_recovery_deadline_at - make_interval(days => %s)",
            (days, days, days, days, days),
        )
        await connection.execute("ALTER TABLE reporting_obligations ENABLE TRIGGER USER")
        present = await (
            await connection.execute(
                "SELECT to_regclass(quote_ident(current_schema()) || '.adcp_reporting_row_sets')"
            )
        ).fetchone()
        if present is not None and present[0] is not None:  # row storage is optional
            await connection.execute("ALTER TABLE adcp_reporting_row_sets DISABLE TRIGGER USER")
            await connection.execute(
                "UPDATE adcp_reporting_row_sets"
                " SET recorded_at = recorded_at - make_interval(days => %s)",
                (days,),
            )
            await connection.execute("ALTER TABLE adcp_reporting_row_sets ENABLE TRIGGER USER")


async def test_expired_periods_lose_their_row_bytes_in_every_binding(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        inline_rows = many_rows(5)
        # content_size_bytes is above zero, so the selector routes both to object
        # storage; force one revision into PostgreSQL bodies through the migration path.
        object_rows = many_rows(40)
        first = typed_revision(obligation, inline_rows, suffix="a")
        second = typed_revision(
            obligation, object_rows, suffix="b", supersedes=first.reporting_revision_id
        )
        for revision, rows in ((first, inline_rows), (second, object_rows)):
            await store.stage_revision_rows(revision, rows)
            await store.commit_revision(revision, rows)
        assert len(list_objects(tmp_path / "rows")) == 2

        # Inside the retention window nothing is pruned.
        young = await store.prune_expired_row_sets()
        assert young.retired == 0 and len(list_objects(tmp_path / "rows")) == 2

        await age(pool, days=500)
        with pytest.raises(ValueError):
            await store.prune_expired_row_sets(ledger_record_retention_days=0)
        pruned = await store.prune_expired_row_sets()
        assert (pruned.retired, pruned.failed) == (1, ())
        assert list_objects(tmp_path / "rows") == []
        assert await fetch_all(
            pool, "SELECT rows_state FROM adcp_reporting_row_sets ORDER BY row_set_id"
        ) == [("pruned",), ("pruned",)]
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_chunks") == [(0,)]
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_chunk_bodies") == [(0,)]

        # Expired rows read like an unknown revision, never as an integrity failure.
        for revision in (first, second):
            with pytest.raises(LedgerConflictError) as expired:
                await store.read_revision_rows(
                    account_id=obligation.account_id,
                    reporting_revision_id=revision.reporting_revision_id,
                )
            assert expired.value.code == "REVISION_NOT_FOUND"
        # The headers remain: records leave the ledger with the shared Core layout.
        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revisions") == [(2,)]
        assert (await store.prune_expired_row_sets()).retired == 0


async def test_postgres_bound_periods_lose_their_bodies(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(30)
        revision = typed_revision(obligation, rows, suffix="pg")
        await store.commit_revision(revision, rows)
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_chunk_bodies") == [(1,)]
        await age(pool, days=500)
        assert (await store.prune_expired_row_sets()).retired == 1
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_chunk_bodies") == [(0,)]


async def test_a_longer_record_retention_keeps_the_period_a_pending_hold_keeps_it_too(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(3)
        revision = typed_revision(obligation, rows, suffix="held")
        await store.commit_revision(revision, rows)
        await age(pool, days=500)
        assert (await store.prune_expired_row_sets(ledger_record_retention_days=3650)).retired == 0
        assert await fetch_all(pool, "SELECT rows_state FROM adcp_reporting_row_sets") == [
            ("live",)
        ]
        assert (await store.prune_expired_row_sets(ledger_record_retention_days=30)).retired == 1


async def test_a_failed_delete_leaves_the_period_pruning_with_its_locators_and_resumes(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        configured = options(tmp_path)
        store = PgReportingLedgerStore(pool=pool, row_storage=configured)
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(10)
        revision = typed_revision(obligation, rows, suffix="resume")
        await store.stage_revision_rows(revision, rows)
        await store.commit_revision(revision, rows)
        await age(pool, days=500)

        # The host stopped configuring the binding: the delete cannot run.
        without = PgReportingLedgerStore(
            pool=pool, row_storage=type(configured)(deployment_namespace="test")
        )
        stuck = await without.prune_expired_row_sets()
        assert stuck.retired == 0 and stuck.failed and stuck.causes[0][1] == "STATE_UNAVAILABLE"
        assert await fetch_all(pool, "SELECT rows_state FROM adcp_reporting_row_sets") == [
            ("pruning",)
        ]
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_chunks") == [(1,)]
        assert len(list_objects(tmp_path / "rows")) == 1
        # While pruning, reads already answer as expired.
        with pytest.raises(LedgerConflictError) as expired:
            await store.read_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )
        assert expired.value.code == "REVISION_NOT_FOUND"

        resumed = await store.prune_expired_row_sets()
        assert resumed.retired == 1
        assert list_objects(tmp_path / "rows") == []
        assert await fetch_all(pool, "SELECT rows_state FROM adcp_reporting_row_sets") == [
            ("pruned",)
        ]


async def test_retention_requires_row_storage_and_sane_limits() -> None:
    async with isolated_reporting_pool() as pool:
        plain = PgReportingLedgerStore(pool=pool)
        await plain.create_schema()
        with pytest.raises(ReportingRowStoreError):
            await plain.prune_expired_row_sets()
        with pytest.raises(ReportingRowStoreError):
            await plain.sweep_row_write_intents()
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        for bad in (0, 10_001):
            with pytest.raises(ValueError):
                await store.sweep_row_write_intents(limit=bad)
            with pytest.raises(ValueError):
                await store.prune_expired_row_sets(limit=bad)


async def test_a_failed_sweep_reports_its_stable_cause(tmp_path: Path) -> None:
    import json

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        revision = await staged_revision(store, obligation, "corrupt", many_rows(10))
        # A corrupted locator outside the installation's owned prefix is never deleted.
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents SET created_objects = %s::jsonb",
                (
                    json.dumps(
                        [{"object_ref": "other/tenant/object", "native_version_ref": "1:2:3:4"}]
                    ),
                ),
            )
        await expire_intents(pool)
        result = await store.sweep_row_write_intents()
        assert (result.swept, result.failed) == (0, 1)
        assert result.causes == ((revision.reporting_revision_id, "UNSAFE_BINDING"),)
        assert await fetch_all(pool, "SELECT state FROM adcp_reporting_row_write_intents") == [
            ("sweeping",)
        ]


# -- head-of-line parity with the JavaScript SDK (adcp-client #3228) -------------------------


class SweepHarness:
    """Intents inserted directly, with provider deletes that fail for chosen keys."""

    def __init__(self, pool: Any, store: PgReportingLedgerStore, failing: set[str]) -> None:
        self.pool = pool
        self.store = store
        self.failing = failing
        storage = store._row_storage
        assert storage is not None
        self.namespace = storage.installation_identity[1]

    def key(self, name: str) -> str:
        return f"adcp-rows/{self.namespace}/hol/{name}.jsonl"

    async def add(
        self,
        row_set_id: str,
        *,
        binding: str = "reports",
        objects: tuple[str, ...] = (),
        expired_seconds: int = 3600,
        state: str = "open",
    ) -> None:
        created = [{"object_ref": self.key(name), "native_version_ref": "v1"} for name in objects]
        async with self.pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_row_write_intents (row_binding_id, account_id,"
                " row_set_id, content_sha256, state, owner_token, created_objects, expires_at)"
                " VALUES (%s, 'acct_hol', %s, %s, %s, 'owner', %s::jsonb,"
                " clock_timestamp() - make_interval(secs => %s))",
                (binding, row_set_id, "d" * 64, state, json.dumps(created), expired_seconds),
            )

    async def row(self, row_set_id: str) -> Any:
        rows = await fetch_all(
            self.pool,
            "SELECT state, sweep_attempts, next_sweep_at FROM adcp_reporting_row_write_intents"
            " WHERE row_set_id = %s",
            (row_set_id,),
        )
        return rows[0] if rows else None

    async def clear_backoff(self) -> None:
        async with self.pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents SET next_sweep_at = NULL"
            )

    async def sweep(self, **kwargs: Any) -> tuple[int, int, int, int, int, int]:
        result = await self.store.sweep_row_write_intents(**kwargs)
        return (
            result.swept,
            result.objects_deleted,
            result.failed,
            result.skipped_unconfigured,
            result.deferred,
            result.overdue,
        )


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    from dataclasses import replace

    failing: set[str] = set()
    inner = create_filesystem_reporting_row_object_provider_v1(roots={"rows": tmp_path / "rows"})

    class Failing:
        name = inner.name
        ranged_reads = inner.ranged_reads

        def validate_location(self, location: Any) -> None:
            inner.validate_location(location)

        async def probe(self, *args: Any, **kwargs: Any) -> None:
            await inner.probe(*args, **kwargs)

        async def put_if_absent(self, put: Any, context: Any) -> Any:
            return await inner.put_if_absent(put, context)

        async def get(self, get: Any, context: Any) -> Any:
            return await inner.get(get, context)

        async def delete(self, delete: Any, context: Any) -> Any:
            if delete.object_ref in failing:
                raise RuntimeError("provider unavailable")
            return await inner.delete(delete, context)

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(
            pool=pool, row_storage=replace(options(tmp_path), providers={"filesystem": Failing()})
        )
        await store.create_schema()
        # A binding another host configured and this one does not.
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_row_bindings (row_binding_id, kind, provider,"
                " identity_config, identity_sha256, namespace_key)"
                " VALUES ('ghost', 'object', 'filesystem', '{}', %s, %s)",
                ("e" * 64, "f" * 64),
            )
        yield SweepHarness(pool, store, failing)


async def test_with_limit_one_a_permanently_failing_intent_does_not_block_an_eligible_one(
    harness: SweepHarness,
) -> None:
    harness.failing.add(harness.key("failing"))
    await harness.add("rrev_failing", objects=("failing",), expired_seconds=3 * 3600)
    await harness.add("rrev_healthy", objects=("healthy",), expired_seconds=3600)

    # The oldest intent is tried first and fails.
    assert await harness.sweep(limit=1) == (0, 0, 1, 0, 1, 0)
    state, attempts, next_sweep_at = await harness.row("rrev_failing")
    assert attempts == 1 and next_sweep_at is not None
    # It backs off, so the healthy one is swept.
    assert await harness.sweep(limit=1) == (1, 1, 0, 0, 1, 0)
    assert await harness.row("rrev_healthy") is None
    # Not retried before its backoff elapses; once the provider recovers it is swept.
    assert await harness.sweep(limit=1) == (0, 0, 0, 0, 1, 0)
    harness.failing.clear()
    await harness.clear_backoff()
    assert await harness.sweep(limit=1) == (1, 1, 0, 0, 0, 0)
    assert await harness.row("rrev_failing") is None


async def test_a_retry_never_takes_the_slot_of_a_fresh_intent(harness: SweepHarness) -> None:
    harness.failing.add(harness.key("retry_first"))
    await harness.add("rrev_retry_first", objects=("retry_first",), expired_seconds=5 * 3600)
    await harness.sweep(limit=1)
    await harness.clear_backoff()
    await harness.add("rrev_fresh_after", objects=("fresh_after",), expired_seconds=3600)
    # The failed intent is ready again and older, but the fresh one goes first.
    result = await harness.sweep(limit=1)
    assert result[0] == 1
    assert await harness.row("rrev_fresh_after") is None
    assert await harness.row("rrev_retry_first") is not None


async def test_unconfigured_bindings_are_skipped_and_counted_unless_there_is_nothing_to_delete(
    harness: SweepHarness,
) -> None:
    await harness.add(
        "rrev_ghost", binding="ghost", objects=("ghost_object",), expired_seconds=3 * 86400
    )
    # Nothing to delete means no binding is needed.
    await harness.add("rrev_ghost_empty", binding="ghost", expired_seconds=7200)
    await harness.add("rrev_local", objects=("local",), expired_seconds=3600)

    # swept, objects_deleted, failed, skipped_unconfigured, deferred, overdue
    assert await harness.sweep(limit=10) == (2, 1, 0, 1, 0, 1)
    skipped = await harness.row("rrev_ghost")
    assert skipped == ("open", 0, None), "left untouched for a host that configures the binding"


async def test_an_unconfigured_binding_at_the_head_of_the_queue_does_not_take_the_slot(
    harness: SweepHarness,
) -> None:
    await harness.add(
        "rrev_ghost_head", binding="ghost", objects=("ghost_head",), expired_seconds=3 * 86400
    )
    await harness.add("rrev_local_next", objects=("local_next",), expired_seconds=3600)
    result = await harness.sweep(limit=1)
    assert result[0] == 1 and result[3] == 1
    assert await harness.row("rrev_local_next") is None


async def test_a_sweeping_intent_backs_off_like_an_open_one(harness: SweepHarness) -> None:
    harness.failing.add(harness.key("sweeping"))
    await harness.add(
        "rrev_sweeping", objects=("sweeping",), expired_seconds=7200, state="sweeping"
    )
    await harness.add("rrev_after_sweeping", objects=("after",), expired_seconds=3600)
    assert (await harness.sweep(limit=1))[2] == 1
    assert (await harness.sweep(limit=1))[0] == 1
    assert await harness.row("rrev_after_sweeping") is None


async def test_overdue_also_counts_a_configured_intent_that_keeps_failing(
    harness: SweepHarness,
) -> None:
    harness.failing.add(harness.key("provider_down"))
    await harness.add("rrev_provider_down", objects=("provider_down",), expired_seconds=2 * 86400)
    assert (await harness.sweep())[2] == 1
    # Backing off, still unswept after the threshold: deferred and overdue, not skipped, because
    # this host configures the binding and the provider is the fault.
    assert await harness.sweep() == (0, 0, 0, 0, 1, 1)


async def test_intents_expired_far_longer_than_the_sweep_interval_are_flagged(
    harness: SweepHarness,
) -> None:
    await harness.add("rrev_recent", binding="ghost", objects=("recent",), expired_seconds=7200)
    assert (await harness.sweep())[5] == 0
    assert (await harness.sweep(overdue_after_seconds=3600))[5] == 1
    with pytest.raises(ValueError):
        await harness.store.sweep_row_write_intents(overdue_after_seconds=0)


async def test_an_earlier_intent_table_gains_the_sweep_backoff_columns_in_place(
    harness: SweepHarness,
) -> None:
    async with harness.pool.connection() as connection:
        await connection.execute(
            "ALTER TABLE adcp_reporting_row_write_intents DROP COLUMN sweep_attempts,"
            " DROP COLUMN next_sweep_at"
        )
    await harness.store.create_schema()
    await harness.store.create_schema()
    await harness.store.ready_row_storage()  # the catalog check accepts the upgraded table
    await harness.add("rrev_after_upgrade", objects=("x",), expired_seconds=10)
    assert await harness.row("rrev_after_upgrade") == ("open", 0, None)


async def test_intents_written_before_the_field_alignment_are_still_swept_and_honoured(
    tmp_path: Path,
) -> None:
    """A writer from before the alignment, running during a rolling upgrade, records the old keys."""
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        await staged_revision(store, obligation, "legacy", many_rows(10))
        ((created,),) = await fetch_all(
            pool, "SELECT created_objects FROM adcp_reporting_row_write_intents"
        )
        assert set(created[0]) == {"object_ref", "native_version_ref"}
        legacy = [
            {"objectKey": item["object_ref"], "nativeVersion": item["native_version_ref"]}
            for item in created
        ]
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents SET created_objects = %s::jsonb",
                (json.dumps(legacy),),
            )
        storage = store._row_storage
        assert storage is not None
        binding = (
            await fetch_all(pool, "SELECT row_binding_id FROM adcp_reporting_row_write_intents")
        )[0][0]
        pair = (legacy[0]["objectKey"], legacy[0]["nativeVersion"])
        # An open legacy-format intent that created the object protects it from other deletes.
        assert await storage.delete_unreferenced(pool, binding, [pair]) == 0
        assert len(list_objects(tmp_path / "rows")) == 1
        # Once expired, the sweep reads the old keys and removes the object.
        await expire_intents(pool)
        result = await store.sweep_row_write_intents()
        assert (result.swept, result.objects_deleted, result.failed) == (1, 1, 0)
        assert list_objects(tmp_path / "rows") == []
