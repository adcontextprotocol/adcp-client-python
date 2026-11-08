"""The opt-in listing collector for orphaned row objects, on PostgreSQL and the filesystem provider.

Ported from the JavaScript SDK (adcp-client #3291, ``reporting-row-orphan-collector-pg``).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from adcp_reporting_storage.ledger.row_storage import ReportingRowStoreError
from adcp_reporting_storage.ledger.row_storage_object import (
    ReportingRowObjectListEntryV1,
    ReportingRowObjectListInputV1,
    ReportingRowObjectListPageV1,
)

from ._orphan_support import binding_result, orphan_fixture
from .test_reporting_row_storage_pg import fetch_all


async def test_deletes_an_orphan_and_keeps_committed_intent_named_and_young_objects(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        committed, _ = await fx.commit_revision("orphans_commit")
        # An upload that succeeded and was never recorded anywhere.
        orphan = fx.key("acct/orphan.jsonl.gz")
        await fx.put_object(orphan)
        # Named by an open upload that has not committed yet.
        in_flight = fx.key("acct/in-flight.jsonl.gz")
        await fx.add_intent("rrev_in_flight", in_flight, await fx.put_object(in_flight))
        young = fx.key("acct/young.jsonl")
        await fx.put_object(young)

        everything = [orphan, committed, in_flight, young]
        dry = await fx.collect(dry_run=True, bindings=["objects"])
        assert dry.dry_run is True
        assert sorted(c.object_ref for c in dry.candidates) == sorted([orphan, young])
        assert {c.binding_id for c in dry.candidates} == {"objects"}
        assert dry.would_delete == 2 and dry.deleted == 0
        assert all(fx.on_disk(ref) for ref in everything), "a dry run deletes nothing"

        # At the real clock nothing is old enough.
        early = await fx.store.collect_orphaned_row_objects(acknowledge_exclusive_namespace=True)
        assert early.deleted == 0
        assert binding_result(early, "objects").too_young == 4
        assert all(fx.on_disk(ref) for ref in everything)

        real = await fx.collect(bindings=["objects"])
        assert real.deleted == 2
        assert not fx.on_disk(orphan) and not fx.on_disk(young)
        assert fx.on_disk(committed) and fx.on_disk(in_flight)
        assert binding_result(real, "objects").referenced == 2
        assert binding_result(real, "objects").status == "complete"
        assert real.completed == ("objects",)


async def test_never_touches_keys_outside_the_owned_prefix_or_objects_that_are_not_rows(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        for name in ("a.txt", "b.json", "nested/c.bin"):
            await fx.put_object(fx.key(name))
        elsewhere = "orph/not-the-namespace/x.jsonl"
        sibling = f"orphan-extra/{fx.namespace_key}/y.jsonl"
        for ref in (elsewhere, sibling):
            await fx.put_object(ref)
        result = await fx.collect(bindings=["objects"])
        assert result.deleted == 0 and result.foreign >= 3
        assert fx.on_disk(elsewhere) and fx.on_disk(sibling)
        assert all(fx.on_disk(fx.key(name)) for name in ("a.txt", "b.json", "nested/c.bin"))


async def test_skips_read_only_bindings_unless_asked_and_reports_a_provider_that_cannot_list(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        legacy = fx.key("acct/legacy.jsonl", "legacy")
        await fx.put_object(legacy)
        skipped = await fx.collect()
        assert "legacy" in skipped.skipped_read_only
        assert binding_result(skipped, "legacy").status == "read_only"
        assert fx.on_disk(legacy)
        assert skipped.unsupported == ("nolist",)
        assert binding_result(skipped, "nolist").status == "unsupported"
        assert skipped.fenced == ()

        included = await fx.collect(include_read_only=True, bindings=["legacy"])
        assert binding_result(included, "legacy").deleted == 1
        assert not fx.on_disk(legacy)


async def test_pages_with_a_per_pass_budget_and_resumes_from_the_returned_cursors(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        refs = [fx.key(f"acct/o{index}.jsonl") for index in range(7)]
        for ref in refs:
            await fx.put_object(ref)
        cursors: dict[str, str] = {}
        deleted = 0
        for _ in range(10):
            result = await fx.collect(bindings=["objects"], limit=3, page_size=2, cursors=cursors)
            deleted += result.deleted
            cursors = dict(result.cursors)
            if not cursors:
                break
        assert deleted == 7 and not any(fx.on_disk(ref) for ref in refs)


async def test_reports_the_bindings_whose_listing_finished_and_skips_the_ones_it_is_told_to(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.put_object(fx.key("acct/a.jsonl"))
        first = await fx.collect(bindings=["objects", "legacy"], include_read_only=True)
        assert set(first.completed) == {"objects", "legacy"}
        fx.provider.list_calls.clear()
        again = await fx.collect(
            bindings=["objects", "legacy"], include_read_only=True, skip_bindings=["objects"]
        )
        assert again.completed == ("legacy",)
        assert [b.binding_id for b in again.bindings] == ["legacy"]
        assert all(
            "/legacy/" in call or call.startswith("legacy/") for call in fx.provider.list_calls
        )


async def test_a_delete_that_fails_is_counted_and_does_not_stop_the_rest(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        stuck, fine = fx.key("acct/a-stuck.jsonl"), fx.key("acct/b-fine.jsonl")
        await fx.put_object(stuck)
        await fx.put_object(fine)
        fx.provider.failing_keys.add(stuck)
        result = await fx.collect(bindings=["objects"])
        assert result.deleted == 1 and result.failed == 1
        assert fx.on_disk(stuck) and not fx.on_disk(fine)
        assert binding_result(result, "objects").status == "complete"
        # It is retried on the next full listing.
        fx.provider.failing_keys.clear()
        retried = await fx.collect(bindings=["objects"])
        assert retried.deleted == 1 and not fx.on_disk(stuck)


async def test_a_listing_failure_keeps_its_cursor_and_names_the_cause(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        for index in range(4):
            await fx.put_object(fx.key(f"acct/o{index}.jsonl"))
        partial = await fx.collect(bindings=["objects"], limit=2, page_size=2)
        assert binding_result(partial, "objects").status == "partial"
        cursor = partial.cursors["objects"]
        fx.provider.list_failures.append(ReportingRowStoreError("PROVIDER_UNAVAILABLE"))
        # The first page of a resumed cursor fails: the stale cursor is dropped.
        failed = await fx.collect(bindings=["objects"], cursors={"objects": cursor})
        outcome = binding_result(failed, "objects")
        assert outcome.status == "failed" and outcome.error == "PROVIDER_UNAVAILABLE"
        assert "objects" not in failed.cursors
        # A failure after the first page keeps the position it reached.
        calls = 0
        real_list = fx.provider.list

        async def second_page_fails(
            request: ReportingRowObjectListInputV1, real: Any
        ) -> ReportingRowObjectListPageV1:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ReportingRowStoreError("DEADLINE_EXCEEDED")
            return await real(request)  # type: ignore[no-any-return]

        fx.provider.list_override = second_page_fails
        mid = await fx.collect(bindings=["objects"], limit=100, page_size=2)
        assert binding_result(mid, "objects").error == "DEADLINE_EXCEEDED"
        assert "objects" in mid.cursors
        assert real_list is not None


async def test_a_record_naming_the_key_at_another_version_or_an_unpruned_row_set_keeps_it(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref, version = await fx.commit_revision("named_other_version")
        # The same key, now at a different version, is still named by a record at the old one.
        await fx.provider.inner.delete(
            __import__(
                "adcp_reporting_storage.ledger.row_storage_object", fromlist=["x"]
            ).ReportingRowObjectDeleteInputV1({"root": "rows"}, ref, version),
            __import__(
                "adcp_reporting_storage.ledger.row_storage_object", fromlist=["x"]
            ).ReportingRowObjectContextV1(),
        )
        await fx.put_object(ref)  # a new version at the recorded key
        result = await fx.collect(bindings=["objects"])
        assert result.deleted == 0
        assert binding_result(result, "objects").referenced == 1
        assert fx.on_disk(ref)


async def test_a_row_set_that_is_pruning_or_unavailable_keeps_its_object_and_a_pruned_one_does_not(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref, _ = await fx.commit_revision("states")
        for state in ("pruning", "unavailable"):
            async with fx.pool.connection() as connection:
                await connection.execute(
                    "UPDATE adcp_reporting_row_sets SET rows_state = %s", (state,)
                )
            kept = await fx.collect(bindings=["objects"])
            assert kept.deleted == 0 and fx.on_disk(ref), state
        async with fx.pool.connection() as connection:
            await connection.execute("UPDATE adcp_reporting_row_sets SET rows_state = 'pruned'")
        freed = await fx.collect(bindings=["objects"])
        assert freed.deleted == 1 and not fx.on_disk(ref)


async def test_an_intent_written_by_an_older_sdk_keeps_its_object(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/legacy-intent.jsonl.gz")
        await fx.add_intent("rrev_old", ref, await fx.put_object(ref), legacy_names=True)
        result = await fx.collect(bindings=["objects"])
        assert result.deleted == 0 and fx.on_disk(ref)
        for state in ("sweeping",):
            async with fx.pool.connection() as connection:
                await connection.execute(
                    "UPDATE adcp_reporting_row_write_intents SET state = %s", (state,)
                )
            # The pre-check is blind to the intent state: an intent in any state names its key.
            assert (await fx.collect(bindings=["objects"])).deleted == 0 and fx.on_disk(ref)


async def test_a_record_under_another_binding_sharing_the_bucket_and_prefix_keeps_the_object(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/shared.jsonl.gz")
        version = await fx.put_object(ref)
        # An open upload under the twin binding (same bucket and prefix) names the object.
        await fx.add_intent("rrev_twin", ref, version, binding="twin")
        result = await fx.collect(bindings=["objects"])
        assert result.deleted == 0 and fx.on_disk(ref)


async def test_a_hostile_provider_cannot_make_it_delete_outside_the_owned_prefix(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        outside = "other-tenant/data.jsonl"
        await fx.put_object(outside)
        owned = fx.key("acct/owned.jsonl")
        await fx.put_object(owned)
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        stamp = old.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        def entry(ref: Any, **changes: Any) -> Any:
            base = ReportingRowObjectListEntryV1(ref, "1:2:3:4", 5, stamp)
            return replace(base, **changes)

        hostile = [
            entry(outside),
            entry(f"{fx.key('x')}/../../{outside}"),
            entry(fx.key("a/./b.jsonl")),
            entry(fx.key("a//b.jsonl")),
            entry(fx.key("bad key.jsonl")),
            entry(fx.key("acct/newline.jsonl\n")),
            entry(fx.key("acct/no-version.jsonl"), native_version_ref=""),
            entry(fx.key("acct/long-version.jsonl"), native_version_ref="v" * 1025),
            entry(fx.key("acct/negative.jsonl"), size_bytes=-1),
            entry(fx.key("acct/bad-time.jsonl"), created_at="yesterday"),
            entry(fx.key("acct/offset-time.jsonl"), created_at="2020-01-01T00:00:00+02:00"),
            entry("x" * 1500 + ".jsonl"),
            entry(owned),
        ]

        async def hostile_page(
            request: ReportingRowObjectListInputV1, real: Any
        ) -> ReportingRowObjectListPageV1:
            return ReportingRowObjectListPageV1(tuple(hostile), None)

        fx.provider.list_override = hostile_page
        result = await fx.collect(bindings=["objects"])
        outcome = binding_result(result, "objects")
        assert outcome.status == "complete"
        assert outcome.foreign == len(hostile) - 1, "every malformed or foreign entry is dropped"
        assert fx.on_disk(outside), "nothing outside the owned prefix is ever touched"
        assert all(call.startswith(fx.key("")) for call in fx.provider.delete_calls)
        assert len(fx.provider.delete_calls) == 1
        # An entry whose version is not the stored one deletes nothing: the provider pins versions.
        assert fx.on_disk(owned)


async def test_a_provider_that_repeats_a_token_or_returns_too_many_objects_fails_the_binding(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.put_object(fx.key("acct/a.jsonl"))

        async def repeat(
            request: ReportingRowObjectListInputV1, real: Any
        ) -> ReportingRowObjectListPageV1:
            return ReportingRowObjectListPageV1((), "same-token")

        fx.provider.list_override = repeat
        looped = await fx.collect(bindings=["objects"], limit=100, page_size=10)
        outcome = binding_result(looped, "objects")
        assert outcome.status == "failed" and outcome.error == "PROVIDER_UNAVAILABLE"
        assert "objects" not in looped.cursors, "a repeated token is not a cursor to resume"

        async def too_many(
            request: ReportingRowObjectListInputV1, real: Any
        ) -> ReportingRowObjectListPageV1:
            entry = ReportingRowObjectListEntryV1(
                fx.key("a/x.jsonl"), "v", 1, "2020-01-01T00:00:00.000Z"
            )
            return ReportingRowObjectListPageV1((entry,) * (request.page_size + 1), None)

        fx.provider.list_override = too_many
        many = await fx.collect(bindings=["objects"], page_size=3)
        assert binding_result(many, "objects").status == "failed"
        assert fx.provider.delete_calls == []


async def test_it_validates_its_inputs_and_requires_the_acknowledgement_to_delete(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        store = fx.store
        with pytest.raises(ValueError, match="acknowledge_exclusive_namespace"):
            await store.collect_orphaned_row_objects()
        for bad in (0, 3600, 86_399, 1.5, "7"):
            with pytest.raises(ValueError, match="min_age_seconds"):
                await store.collect_orphaned_row_objects(
                    min_age_seconds=bad, acknowledge_exclusive_namespace=True  # type: ignore[arg-type]
                )
        for name in ("limit", "page_size"):
            for bad in (0, -1, 1.5):
                with pytest.raises(ValueError, match=name):
                    await store.collect_orphaned_row_objects(
                        dry_run=True, **{name: bad}  # type: ignore[arg-type]
                    )
        with pytest.raises(ValueError, match="not a configured object binding"):
            await store.collect_orphaned_row_objects(dry_run=True, bindings=["inline"])
        with pytest.raises(ValueError, match="timezone-aware"):
            await store.collect_orphaned_row_objects(dry_run=True, now=datetime(2030, 1, 1))
        # A dry run needs no acknowledgement.
        assert (await store.collect_orphaned_row_objects(dry_run=True)).dry_run is True


async def test_a_store_without_row_storage_refuses_the_collector() -> None:
    from ._generation_support import isolated_reporting_pool

    async with isolated_reporting_pool() as pool:
        from adcp_reporting_storage import PgReportingLedgerStore

        store = PgReportingLedgerStore(pool=pool)
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.collect_orphaned_row_objects(dry_run=True)
        assert failure.value.code == "INVALID_INPUT"


async def test_the_collector_takes_the_per_object_lock_and_rechecks_under_it(
    tmp_path: Path,
) -> None:
    """An intent committed while the collector waits on the lock keeps the object."""
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/raced.jsonl.gz")
        version = await fx.put_object(ref)
        storage = fx.store._row_storage
        assert storage is not None
        lock_text = storage._object_lock_text("objects", ref)
        holder = await fx.pool.getconn()
        try:
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_text,)
            )
            task = asyncio.create_task(fx.collect(bindings=["objects"]))
            await _until_blocked(fx.pool, task)
            assert not task.done(), "the collector waits on the object's lock"
            # While it waits, an upload that names the object commits.
            await holder.execute(
                "INSERT INTO adcp_reporting_row_write_intents (row_binding_id, account_id,"
                " row_set_id, content_sha256, state, owner_token, created_objects, expires_at)"
                " VALUES ('objects', 'acct', 'rrev_raced', %s, 'open', 'o', %s::jsonb,"
                " clock_timestamp() + INTERVAL '1 hour')",
                (
                    "e" * 64,
                    f'[{{"object_ref": "{ref}", "native_version_ref": "{version}"}}]',
                ),
            )
            await holder.commit()
        finally:
            await fx.pool.putconn(holder)
        result = await asyncio.wait_for(task, 20)
        # The pre-check ran before the intent existed; the recheck under the lock saw it.
        assert result.deleted == 0 and fx.on_disk(ref)
        assert binding_result(result, "objects").referenced == 1


async def test_it_serializes_with_an_adoption_through_another_binding_sharing_the_prefix(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/adopted-by-twin.jsonl.gz")
        version = await fx.put_object(ref)
        storage = fx.store._row_storage
        assert storage is not None
        # A writer adopting through the twin binding holds the twin's lock for this object.
        holder = await fx.pool.getconn()
        try:
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (storage._object_lock_text("twin", ref),),
            )
            task = asyncio.create_task(fx.collect(bindings=["objects"]))
            await _until_blocked(fx.pool, task)
            assert not task.done(), "the delete waits for an adoption through any binding"
            await holder.execute(
                "INSERT INTO adcp_reporting_row_write_intents (row_binding_id, account_id,"
                " row_set_id, content_sha256, state, owner_token, created_objects, expires_at)"
                " VALUES ('twin', 'acct', 'rrev_twin', %s, 'open', 'o', %s::jsonb,"
                " clock_timestamp() + INTERVAL '1 hour')",
                (
                    "e" * 64,
                    f'[{{"object_ref": "{ref}", "native_version_ref": "{version}"}}]',
                ),
            )
            await holder.commit()
        finally:
            await fx.pool.putconn(holder)
        result = await asyncio.wait_for(task, 20)
        assert result.deleted == 0 and fx.on_disk(ref)


async def _until_blocked(pool: Any, task: asyncio.Task[Any]) -> None:
    """Wait until some backend is blocked on an advisory lock, proven with ``pg_locks``."""
    for _ in range(400):
        if task.done():
            return
        rows = await fetch_all(
            pool,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted",
        )
        if rows[0][0]:
            return
        await asyncio.sleep(0.025)
    pytest.fail("the collector never blocked on the object lock")


async def test_dry_run_through_the_store_option_reports_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    from adcp_reporting_storage.ledger.orphan_collector import ReportingOrphanCollectionOptionsV1

    async with orphan_fixture(
        tmp_path, orphan_collection=ReportingOrphanCollectionOptionsV1(dry_run=True)
    ) as fx:
        ref = fx.key("acct/maintained.jsonl")
        await fx.put_object(ref)
        operations = dict(fx.store.maintenance_operations())
        assert "orphan_collection" in operations
        # Too young at the real clock: nothing is a candidate yet.
        result = await operations["orphan_collection"]()
        assert result.dry_run is True and result.would_delete == 0
        assert fx.on_disk(ref)
