"""The per-object advisory lock is kept only where nothing else orders the two sides.

Ported from the JavaScript SDK (adcp-client #3606, ``reporting-row-shared-object-pg``). Retirement,
the intent sweep and a commit that adopts an object its own intent recorded take no object lock; the
orphan collector's delete and a commit that adopts an object no record names pair on it.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
from adcp_reporting_storage.ledger.row_storage import (
    ReportingRowChunkLocatorV1,
    ReportingRowStoreError,
    recorded_intent_objects,
)

from ._orphan_support import Fixture, orphan_fixture
from .test_reporting_row_storage_pg import fetch_all

BODY = b'{"a":1}\n'


async def lock_holder(fx: Fixture, binding: str, object_ref: str) -> Any:
    """A connection holding the per-object lock of ``binding`` until it commits."""
    storage = fx.store._row_storage
    assert storage is not None
    holder = await fx.pool.getconn()
    await holder.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (storage._object_lock_text(binding, object_ref),),
    )
    return holder


async def release(fx: Fixture, holder: Any) -> None:
    await holder.commit()
    await fx.pool.putconn(holder)


def locator(object_ref: str, version: str) -> ReportingRowChunkLocatorV1:
    return ReportingRowChunkLocatorV1(
        object_ref, version, "gzip", hashlib.sha256(BODY).hexdigest(), len(BODY), created=False
    )


async def verify(
    fx: Fixture, binding: str, loc: ReportingRowChunkLocatorV1, recorded: Any = ()
) -> None:
    storage = fx.store._row_storage
    assert storage is not None
    async with fx.pool.connection() as connection, connection.transaction():
        await storage.lock_and_verify_adopted(connection, binding, [loc], recorded)


async def blocks(
    fx: Fixture, binding: str, loc: ReportingRowChunkLocatorV1, recorded: Any = ()
) -> bool:
    """Whether the adoption waits on the object lock (and then completes once it is released)."""
    task = asyncio.create_task(verify(fx, binding, loc, recorded))
    done, _ = await asyncio.wait({task}, timeout=0.6)
    waited = not done
    if waited:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return waited


async def idle_in_transaction(fx: Fixture) -> int:
    rows = await fetch_all(
        fx.pool,
        "SELECT count(*) FROM pg_stat_activity WHERE state LIKE 'idle in transaction%%'"
        " AND datname = current_database() AND pid <> pg_backend_pid()",
    )
    return int(rows[0][0])


async def test_a_delete_of_a_referenced_object_takes_no_lock_and_holds_no_connection_across_the_provider_delete(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref, version = await fx.commit_revision("lock_free_retire")
        storage = fx.store._row_storage
        assert storage is not None
        holder = await lock_holder(fx, "objects", ref)  # the lock is held elsewhere throughout
        seen: list[int] = []

        async def during_delete(object_ref: str) -> None:
            seen.append(await idle_in_transaction(fx))

        fx.provider.on_delete = during_delete
        try:
            # Retirement scopes the delete to its own obligation, so its own references do not count.
            removed = await asyncio.wait_for(
                storage.delete_unreferenced(
                    fx.pool,
                    "objects",
                    [(ref, version)],
                    obligation_id=fx.obligation.reporting_obligation_id,
                ),
                10,
            )
            assert removed == 1 and not fx.on_disk(ref)
            # Only the lock holder is idle in a transaction: nothing of the deleter's is open.
            assert seen == [1]
        finally:
            await release(fx, holder)


async def test_a_referenced_object_is_kept_and_a_pruning_row_set_does_not_count(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref, version = await fx.commit_revision("kept")
        storage = fx.store._row_storage
        assert storage is not None
        assert await storage.delete_unreferenced(fx.pool, "objects", [(ref, version)]) == 0
        assert fx.on_disk(ref)
        async with fx.pool.connection() as connection:
            await connection.execute("UPDATE adcp_reporting_row_sets SET rows_state = 'pruning'")
        # Each deleter commits ``pruning`` before its check, so for any two one of them deletes.
        assert await storage.delete_unreferenced(fx.pool, "objects", [(ref, version)]) == 1
        assert not fx.on_disk(ref)


async def test_the_intent_sweep_takes_no_object_lock_while_the_lock_is_held_elsewhere(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/swept.jsonl.gz")
        version = await fx.put_object(ref)
        await fx.add_intent("rrev_expired", ref, version)
        async with fx.pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents"
                " SET expires_at = clock_timestamp() - INTERVAL '1 hour'"
            )
        holder = await lock_holder(fx, "objects", ref)
        try:
            swept = await asyncio.wait_for(fx.store.sweep_row_write_intents(), 15)
            assert swept.swept == 1 and swept.objects_deleted == 1 and not fx.on_disk(ref)
        finally:
            await release(fx, holder)


async def test_an_adoption_of_an_object_the_intent_recorded_takes_no_lock(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/own.jsonl.gz")
        version = await fx.put_object(ref, BODY)
        holder = await lock_holder(fx, "objects", ref)
        try:
            recorded = {(ref, version)}
            await asyncio.wait_for(verify(fx, "objects", locator(ref, version), recorded), 5)
        finally:
            await release(fx, holder)


async def test_the_pre_alignment_intent_keys_are_read_so_an_older_writers_object_takes_no_lock() -> (
    None
):
    pairs = recorded_intent_objects(
        [
            {"object_ref": "a", "native_version_ref": "1"},
            {"objectKey": "b", "nativeVersion": "2"},
            {"object_ref": "c", "nativeVersion": "3"},  # mixed spellings are tolerated
            {"object_ref": "d"},
            {"unrelated": True},
            "not an object",
            {"object_ref": 5, "native_version_ref": "9"},
        ]
    )
    assert pairs == {("a", "1"), ("b", "2"), ("c", "3")}
    assert recorded_intent_objects(None) == set() and recorded_intent_objects({}) == set()


async def test_an_adoption_takes_the_lock_for_a_different_version_or_an_object_the_intent_does_not_name(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/adopted.jsonl.gz")
        version = await fx.put_object(ref, BODY)
        other = fx.key("acct/other.jsonl.gz")
        holder = await lock_holder(fx, "objects", ref)
        try:
            # The intent names the key at another native version: that is not this object.
            assert await blocks(fx, "objects", locator(ref, version), {(ref, "9:9:9:9")})
            # The intent names another object, or none.
            assert await blocks(fx, "objects", locator(ref, version), {(other, version)})
            assert await blocks(fx, "objects", locator(ref, version))
        finally:
            await release(fx, holder)
        # Once the lock is free the adoption proves the object and succeeds.
        await asyncio.wait_for(verify(fx, "objects", locator(ref, version)), 5)


async def test_an_orphan_delete_in_flight_blocks_an_adopter_of_the_unrecorded_object_which_then_fails_cleanly(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/raced.jsonl.gz")
        version = await fx.put_object(ref, BODY)
        storage = fx.store._row_storage
        assert storage is not None
        deleting = asyncio.Event()
        proceed = asyncio.Event()

        async def pause(object_ref: str) -> None:
            deleting.set()
            await proceed.wait()

        fx.provider.on_delete = pause
        orphan = asyncio.create_task(
            storage.delete_orphaned_object(fx.pool, "objects", ref, version)
        )
        await asyncio.wait_for(deleting.wait(), 10)
        adopter = asyncio.create_task(verify(fx, "objects", locator(ref, version)))
        done, _ = await asyncio.wait({adopter}, timeout=0.6)
        assert not done, "the adoption waits for the delete that holds the object lock"
        proceed.set()
        assert await asyncio.wait_for(orphan, 10) is True
        with pytest.raises(ReportingRowStoreError) as failure:
            await asyncio.wait_for(adopter, 10)
        assert failure.value.code == "STATE_UNAVAILABLE", "the version-pinned stat fails it cleanly"


async def test_an_orphan_delete_takes_every_bindings_lock_for_the_object(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/shared.jsonl.gz")
        version = await fx.put_object(ref, BODY)
        storage = fx.store._row_storage
        assert storage is not None
        # An adopter through the twin binding (same bucket and prefix) holds only the twin's lock.
        holder = await lock_holder(fx, "twin", ref)
        task = asyncio.create_task(storage.delete_orphaned_object(fx.pool, "objects", ref, version))
        done, _ = await asyncio.wait({task}, timeout=0.6)
        assert not done and fx.on_disk(ref)
        await release(fx, holder)
        assert await asyncio.wait_for(task, 10) is True and not fx.on_disk(ref)


async def test_an_orphan_delete_keeps_an_object_a_record_in_another_binding_names(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        ref = fx.key("acct/named.jsonl.gz")
        version = await fx.put_object(ref, BODY)
        await fx.add_intent("rrev_twin", ref, version, binding="twin")
        storage = fx.store._row_storage
        assert storage is not None
        assert await storage.delete_orphaned_object(fx.pool, "objects", ref, version) is False
        assert fx.on_disk(ref)


async def test_a_writer_whose_intent_expired_before_its_commit_cannot_commit_the_swept_objects(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        from .test_reporting_row_storage_pg import many_rows, seeded, typed_revision

        fx.obligation = await seeded(fx.store)
        async with fx.pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_configurations SET lease_worker_id = 'worker-1',"
                " lease_expires_at = clock_timestamp() + INTERVAL '10 minutes'"
            )
        rows = many_rows(4)
        revision = typed_revision(fx.obligation, rows, suffix="swept_writer")
        assert await fx.store.stage_revision_rows(revision, rows) is True
        async with fx.pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents"
                " SET expires_at = clock_timestamp() - INTERVAL '1 hour'"
            )
        swept = await fx.store.sweep_row_write_intents()
        assert swept.swept == 1 and swept.objects_deleted == 1
        # Exactly one of the sweep claim and the commit's intent close wins; the writer lost.
        with pytest.raises(ReportingRowStoreError) as failure:
            await fx.store.commit_revision(revision, rows)
        assert failure.value.code == "STATE_UNAVAILABLE"
        assert await fetch_all(fx.pool, "SELECT count(*) FROM adcp_reporting_row_sets") == [(0,)]


def test_the_lock_text_is_unchanged_and_shared_with_the_javascript_sdk() -> None:
    from adcp_reporting_storage.ledger.row_storage import ReportingRowStorageV1

    assert (
        ReportingRowStorageV1._object_lock_text("reports", "adcp-rows/ns/a/b")
        == "adcp-reporting-object:reports:adcp-rows/ns/a/b"
    )
