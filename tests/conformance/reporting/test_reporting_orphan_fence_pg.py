"""The orphan collector's ownership fence against a cloned or restored ledger database.

Ported from the JavaScript SDK (adcp-client #3593, ``reporting-orphan-collector-fence-pg``). A
clone is a point-in-time copy of every row-storage table in the live schema: the same installation
identity, namespace key, bindings and fence row, sharing the live database's bucket.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from adcp_reporting_storage.ledger.orphan_collector import (
    ReportingOrphanCollectionOptionsV1,
    ReportingOrphanCollectionStateV1,
    run_orphan_collection_pass,
)
from adcp_reporting_storage.ledger.row_storage import ReportingRowStoreError

from ._orphan_support import (
    HookedProvider,
    binding_result,
    clone_schema,
    cloned_store,
    orphan_fixture,
)
from .test_reporting_row_storage_pg import fetch_all


def fenced_cause(result: Any, binding_id: str = "objects") -> str | None:
    outcome = binding_result(result, binding_id)
    return outcome.fence.cause if outcome.fence else None


async def test_the_first_real_pass_claims_epoch_one_and_each_later_pass_the_next(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        assert await fx.fence_row() is None
        first = await fx.collect(bindings=["objects"])
        assert first.fenced == ()
        assert fx.fence_files() == ["0000000000000001.json"]
        assert (await fx.fence_row())[:1] == (1,)  # type: ignore[index]
        await fx.collect(bindings=["objects"])
        # A claim keeps its predecessor (an unfinished claim is recognised by it) and prunes the
        # rest.
        assert fx.fence_files() == ["0000000000000001.json", "0000000000000002.json"]
        await fx.collect(bindings=["objects"])
        assert fx.fence_files() == ["0000000000000002.json", "0000000000000003.json"]
        row = await fx.fence_row()
        assert row is not None and row[0] == 3
        assert row[2] is not None, "a later claim records the token it superseded"


async def test_the_fence_chain_is_not_a_row_object_it_is_neither_listed_foreign_nor_deleted(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        assert fx.fence_files()
        storage = fx.store._row_storage
        assert storage is not None
        listing = await storage.list_objects("objects", page_size=100)
        assert listing.objects == () and listing.foreign == 0
        result = await fx.collect(bindings=["objects"])
        assert result.deleted == 0 and result.foreign == 0
        assert fx.fence_files(), "the collector never deletes its own fence"


async def test_a_dry_run_checks_the_fence_without_claiming_it(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        orphan = fx.key("acct/orphan.jsonl")
        await fx.put_object(orphan)
        dry = await fx.collect(dry_run=True, bindings=["objects"])
        assert dry.would_delete == 1 and dry.fenced == ()
        assert fx.fence_files() == [] and await fx.fence_row() is None, "no claim, no object"
        # It reports a fenced namespace the same way a real pass does.
        fx.write_fence(4)
        refused = await fx.collect(dry_run=True, bindings=["objects"])
        assert refused.fenced == ("objects",)
        assert fenced_cause(refused) == "claimed_elsewhere"
        assert refused.would_delete == 0


async def test_a_clone_taken_before_the_original_last_collected_is_fenced_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])  # epoch 1
        schema = await clone_schema(fx.pool)
        committed, _ = await fx.commit_revision("clone_live")  # written after the copy
        await fx.collect(bindings=["objects"])  # the original claims epoch 2

        async with cloned_store(fx, schema) as clone:
            result = await clone.collect_orphaned_row_objects(
                now=_later(), acknowledge_exclusive_namespace=True, bindings=["objects"]
            )
            assert result.fenced == ("objects",) and result.deleted == 0
            assert fenced_cause(result) == "fence_advanced"
            outcome = binding_result(result, "objects")
            assert outcome.fence is not None
            assert (outcome.fence.fence_epoch, outcome.fence.recorded_epoch) == (2, 1)
            assert "take_over_orphan_collector_fence" in outcome.fence.detail
            assert fx.on_disk(committed), "the clone cannot delete an object the original wrote"
            assert result.cursors == {}

            # Control: the same clone, once it has deliberately taken over, deletes it. The fence
            # is the only thing standing between the clone and the live object.
            await clone.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)
            deleted = await clone.collect_orphaned_row_objects(
                now=_later(), acknowledge_exclusive_namespace=True, bindings=["objects"]
            )
            assert deleted.deleted >= 1 and not fx.on_disk(committed)


async def test_a_clone_that_collects_first_claims_the_namespace_and_fences_the_original(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        schema = await clone_schema(fx.pool)  # neither side has claimed yet
        async with cloned_store(fx, schema) as clone:
            first = await clone.collect_orphaned_row_objects(
                now=_later(), acknowledge_exclusive_namespace=True, bindings=["objects"]
            )
            assert first.fenced == ()
        original = await fx.collect(bindings=["objects"])
        assert original.fenced == ("objects",)
        assert fenced_cause(original) == "claimed_elsewhere", "first claim wins"


async def test_a_restore_to_an_older_database_state_is_fenced_until_an_operator_takes_over(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        await fx.collect(bindings=["objects"])  # bucket epoch 2
        async with fx.pool.connection() as connection:
            # The restore: the database's own record goes back to epoch 1.
            await connection.execute(
                "UPDATE adcp_reporting_orphan_collector_fences SET epoch = 1,"
                " token = %s, previous_token = NULL",
                (str(uuid.uuid4()),),
            )
        restored = await fx.collect(bindings=["objects"])
        assert fenced_cause(restored) == "fence_advanced"
        taken = await fx.store.take_over_orphan_collector_fence(
            acknowledge_previous_owner_gone=True
        )
        assert [(t.binding_ids, t.epoch, t.previous_epoch) for t in taken] == [(("objects",), 3, 2)]
        after = await fx.collect(bindings=["objects"])
        assert after.fenced == ()


async def test_a_takeover_fences_out_the_previous_owner(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        schema = await clone_schema(fx.pool)
        async with cloned_store(fx, schema) as clone:
            await clone.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)
            out = await fx.collect(bindings=["objects"])
            assert fenced_cause(out) == "fence_advanced"
            with pytest.raises(ValueError, match="acknowledge_previous_owner_gone"):
                await clone.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=False)


async def test_a_takeover_validates_its_bindings_and_covers_a_shared_fence_once(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        store = fx.store
        for bad, message in (
            (["inline"], "not a configured object binding"),
            (["nolist"], "cannot list"),
            (["nope"], "not a configured object binding"),
        ):
            with pytest.raises(ValueError, match=message):
                await store.take_over_orphan_collector_fence(
                    acknowledge_previous_owner_gone=True, bindings=bad
                )
        # ``objects`` and its read-only ``twin`` share one bucket and prefix, so one fence.
        taken = await store.take_over_orphan_collector_fence(
            acknowledge_previous_owner_gone=True, include_read_only=True
        )
        shared = [t for t in taken if "objects" in t.binding_ids]
        assert len(shared) == 1 and set(shared[0].binding_ids) == {"objects", "twin"}
        # Read-only bindings are skipped unless asked for.
        default = await store.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)
        assert all("legacy" not in t.binding_ids for t in default)
        only = await store.take_over_orphan_collector_fence(
            acknowledge_previous_owner_gone=True, bindings=["legacy"]
        )
        assert [t.binding_ids for t in only] == [("legacy",)]


async def test_bindings_that_share_a_bucket_and_prefix_share_one_fence_and_one_claim_per_pass(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        result = await fx.collect(bindings=["objects", "twin"], include_read_only=True)
        assert result.fenced == ()
        assert fx.fence_files() == ["0000000000000001.json"], "one claim for both bindings"
        rows = await fetch_all(
            fx.pool, "SELECT count(*) FROM adcp_reporting_orphan_collector_fences"
        )
        assert rows == [(1,)]


async def test_a_database_with_no_recorded_claim_refuses_a_namespace_that_already_has_one(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        fx.write_fence(3)
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "claimed_elsewhere"
        outcome = binding_result(result, "objects")
        assert outcome.fence is not None and outcome.fence.fence_epoch == 3
        assert outcome.fence.recorded_epoch is None
        assert await fx.fence_row() is None, "a refusal never records a claim"


async def test_a_recorded_claim_with_no_fence_in_the_bucket_is_refused_as_missing(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        await fx.collect(bindings=["objects"])
        for name in fx.fence_files():
            (fx.fence_dir() / name).unlink()
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "fence_missing"
        assert fx.fence_files() == [], "never silently re-claimed"


async def test_a_database_newer_than_its_bucket_is_refused_as_regressed(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        async with fx.pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_orphan_collector_fences SET epoch = 5, token = %s,"
                " previous_token = %s",
                (str(uuid.uuid4()), str(uuid.uuid4())),
            )
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "fence_regressed"
        outcome = binding_result(result, "objects")
        assert outcome.fence is not None
        assert (outcome.fence.fence_epoch, outcome.fence.recorded_epoch) == (1, 5)


async def test_two_claims_of_one_epoch_the_loser_is_fenced_as_a_token_mismatch(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        (fx.fence_dir() / "0000000000000001.json").unlink()
        fx.write_fence(1)  # another claim of epoch 1, under another token
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "token_mismatch"


async def test_a_lost_race_for_the_next_epoch_is_a_lost_claim(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        raced = False

        async def other_database_claims_first(put: Any) -> None:
            nonlocal raced
            if "_collector-fence/0000000000000002" in put.object_ref and not raced:
                raced = True
                fx.write_fence(2)  # another database wins the create-only write

        fx.provider.on_put = other_database_claims_first
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "claim_lost"
        outcome = binding_result(result, "objects")
        assert outcome.fence is not None and outcome.fence.fence_epoch == 2


@pytest.mark.parametrize(
    "damage",
    ["garbage", "other_namespace", "oversized", "extra_field", "bad_token", "wrong_epoch"],
)
async def test_malformed_foreign_namespace_or_oversized_fence_objects_fail_closed(
    tmp_path: Path, damage: str
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.put_object(fx.key("acct/orphan.jsonl"))
        good = str(uuid.uuid4())
        if damage == "garbage":
            fx.write_fence(1, raw=b"not json\n")
        elif damage == "other_namespace":
            fx.write_fence(1, namespace="f" * 64)
        elif damage == "oversized":
            fx.write_fence(1, raw=b"x" * 5000)
        elif damage == "extra_field":
            fx.write_fence(
                1,
                raw=json.dumps(
                    {
                        "v": 1,
                        "namespace_key": fx.namespace_key,
                        "epoch": 1,
                        "token": good,
                        "previous_token": None,
                        "claimed_at": "2026-01-01T00:00:00.000Z",
                        "extra": 1,
                    }
                ).encode(),
            )
        elif damage == "bad_token":
            fx.write_fence(1, token="not-a-uuid")
        else:
            fx.write_fence(1)
            (fx.fence_dir() / "0000000000000001.json").rename(
                fx.fence_dir() / "0000000000000002.json"
            )
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "fence_unreadable", damage
        assert result.deleted == 0 and fx.on_disk(fx.key("acct/orphan.jsonl"))


async def test_two_versions_of_one_epoch_are_ambiguous_and_fail_closed(tmp_path: Path) -> None:
    from adcp_reporting_storage.ledger.row_storage_object import (
        ReportingRowObjectListEntryV1,
        ReportingRowObjectListPageV1,
    )

    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        key = f"orph/{fx.namespace_key}/_collector-fence/0000000000000001.json"
        duplicate = ReportingRowObjectListPageV1(
            tuple(
                ReportingRowObjectListEntryV1(key, f"{index}:1:1:1", 10, "2026-01-01T00:00:00.000Z")
                for index in (1, 2)
            ),
            None,
        )
        fx.provider.fence_list_override = duplicate
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "fence_unreadable"


async def test_an_unfinished_claim_is_completed_by_the_next_pass(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        fx.provider.put_failures.append(RuntimeError("bucket unavailable"))
        crashed = await fx.collect(bindings=["objects"])
        outcome = binding_result(crashed, "objects")
        assert outcome.status == "failed" and outcome.fence is None, "a failure is not a fence"
        assert fx.fence_files() == [], "the object was never written"
        row = await fx.fence_row()
        assert row is not None and row[0] == 1, "the intent was committed first"
        recovered = await fx.collect(bindings=["objects"])
        assert recovered.fenced == () and binding_result(recovered, "objects").status == "complete"
        assert fx.fence_files() == ["0000000000000001.json"]
        done = await fx.fence_row()
        assert done is not None and done[1] != row[1], "a finished claim mints a fresh token"


async def test_a_copy_taken_while_a_claim_was_unfinished_cannot_adopt_the_same_token(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        fx.provider.put_failures.append(RuntimeError("bucket unavailable"))
        await fx.collect(bindings=["objects"])  # intent committed, object never written
        schema = await clone_schema(fx.pool)  # the copy carries the same unfinished intent
        original = await fx.collect(bindings=["objects"])
        assert original.fenced == ()
        async with cloned_store(fx, schema) as clone:
            result = await clone.collect_orphaned_row_objects(
                now=_later(), acknowledge_exclusive_namespace=True, bindings=["objects"]
            )
            assert fenced_cause(result) == "token_mismatch"
            assert result.deleted == 0


async def test_two_hosts_of_the_same_database_collecting_at_once_do_not_fence_each_other(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        for index in range(6):
            await fx.put_object(fx.key(f"acct/o{index}.jsonl"))
        first, second = await asyncio.gather(
            fx.collect(bindings=["objects"]), fx.collect(bindings=["objects"])
        )
        assert first.fenced == () and second.fenced == ()
        assert first.deleted + second.deleted >= 6
        row = await fx.fence_row()
        assert row is not None and row[0] == 2


async def test_another_database_claiming_mid_pass_stops_the_deletes_at_the_next_check(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        refs = [fx.key(f"acct/o{index:03d}.jsonl") for index in range(60)]
        for ref in refs:
            await fx.put_object(ref)
        deleted = 0

        async def other_database_claims(object_ref: str) -> None:
            nonlocal deleted
            deleted += 1
            if deleted == 10:
                fx.write_fence(99)  # a newer claim appears while this pass is running

        fx.provider.on_delete = other_database_claims
        result = await fx.collect(bindings=["objects"])
        outcome = binding_result(result, "objects")
        assert outcome.status == "fenced" and fenced_cause(result) == "fence_advanced"
        assert outcome.deleted == 25, "a check before the first delete and every 25 after it"
        assert sum(fx.on_disk(ref) for ref in refs) == 35
        assert result.deleted == 25, "a mid-pass refusal keeps what went before"


async def test_the_fence_is_claimed_before_the_first_delete_of_a_pass(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.put_object(fx.key("acct/o.jsonl"))
        seen: list[list[str]] = []

        async def look(object_ref: str) -> None:
            seen.append(fx.fence_files())

        fx.provider.on_delete = look
        await fx.collect(bindings=["objects"])
        assert seen == [["0000000000000001.json"]]


async def test_without_the_fence_table_a_real_pass_and_a_dry_run_refuse_and_delete_nothing(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        orphan = fx.key("acct/o.jsonl")
        await fx.put_object(orphan)
        async with fx.pool.connection() as connection:
            await connection.execute("DROP TABLE adcp_reporting_orphan_collector_fences")
        for dry_run in (False, True):
            result = await fx.collect(bindings=["objects"], dry_run=dry_run)
            assert fenced_cause(result) == "migration_required"
            assert result.deleted == 0 and result.would_delete == 0 and fx.on_disk(orphan)
        with pytest.raises(ReportingRowStoreError, match="fence table does not exist"):
            await fx.store.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)


async def test_the_fence_table_is_part_of_the_row_storage_ddl_and_reapplying_it_changes_nothing(
    tmp_path: Path,
) -> None:
    from adcp_reporting_storage.ledger.row_storage import REPORTING_ROW_STORAGE_DDL_PATH

    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        before = await fx.fence_row()
        async with fx.pool.connection() as connection:
            await connection.execute(REPORTING_ROW_STORAGE_DDL_PATH.read_text())
        assert await fx.fence_row() == before
        columns = await fetch_all(
            fx.pool,
            "SELECT column_name FROM information_schema.columns WHERE table_schema ="
            " current_schema() AND table_name = 'adcp_reporting_orphan_collector_fences'"
            " ORDER BY column_name",
        )
        assert [row[0] for row in columns] == [
            "claimed_at",
            "epoch",
            "fence_scope",
            "previous_token",
            "token",
        ]


async def test_a_provider_failure_while_reading_or_writing_the_fence_is_failed_not_fenced(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.put_object(fx.key("acct/o.jsonl"))
        fx.provider.fence_list_failures.append(ReportingRowStoreError("PROVIDER_UNAVAILABLE"))
        read = await fx.collect(bindings=["objects"])
        outcome = binding_result(read, "objects")
        assert outcome.status == "failed" and outcome.error == "PROVIDER_UNAVAILABLE"
        assert read.fenced == () and read.deleted == 0
        fx.provider.put_failures.append(RuntimeError("down"))
        write = await fx.collect(bindings=["objects"])
        assert binding_result(write, "objects").status == "failed" and write.fenced == ()
        ok = await fx.collect(bindings=["objects"])
        assert ok.deleted == 1


async def test_a_failed_claim_keeps_the_resume_position_of_a_listing(tmp_path: Path) -> None:
    async with orphan_fixture(tmp_path) as fx:
        for index in range(4):
            await fx.put_object(fx.key(f"acct/o{index}.jsonl"))
        partial = await fx.collect(bindings=["objects"], limit=2, page_size=2)
        cursor = partial.cursors["objects"]
        fx.provider.fence_list_failures.append(ReportingRowStoreError("PROVIDER_UNAVAILABLE"))
        again = await fx.collect(
            bindings=["objects"], limit=2, page_size=2, cursors={"objects": cursor}
        )
        assert binding_result(again, "objects").status == "failed"
        assert again.cursors == {"objects": cursor}, "the listing never started"


async def test_a_takeover_recovers_every_refusal_and_moves_past_a_database_ahead_of_its_bucket(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        # claimed_elsewhere
        fx.write_fence(3)
        assert fenced_cause(await fx.collect(bindings=["objects"])) == "claimed_elsewhere"
        await fx.store.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)
        assert (await fx.collect(bindings=["objects"])).fenced == ()
        row = await fx.fence_row()
        assert row is not None and row[0] >= 4
        # fence_regressed: the database is ahead of its bucket
        async with fx.pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_orphan_collector_fences SET epoch = 50, token = %s",
                (str(uuid.uuid4()),),
            )
        assert fenced_cause(await fx.collect(bindings=["objects"])) == "fence_regressed"
        await fx.store.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)
        assert (await fx.collect(bindings=["objects"])).fenced == ()
        row = await fx.fence_row()
        assert row is not None and row[0] > 50, "a takeover moves past a database that is ahead"
        # fence_unreadable
        fx.write_fence(row[0] + 1, raw=b"damaged")
        assert fenced_cause(await fx.collect(bindings=["objects"])) == "fence_unreadable"
        await fx.store.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)
        assert (await fx.collect(bindings=["objects"])).fenced == ()
        assert len(fx.fence_files()) == 2 or len(fx.fence_files()) == 1


async def test_a_fenced_clone_refuses_every_binding_that_shares_the_fence_with_one_check(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        fx.write_fence(7)
        fx.provider.list_calls.clear()
        result = await fx.collect(bindings=["objects", "twin"], include_read_only=True)
        assert set(result.fenced) == {"objects", "twin"}
        assert result.cursors == {}
        # The fence chain was read once for the shared scope, and no object listing began.
        listings = fx.provider.list_calls
        assert all("_collector-fence" in prefix for prefix in listings), listings


async def test_a_fenced_namespace_never_rests_a_whole_interval_in_the_maintenance_cycle(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        options = ReportingOrphanCollectionOptionsV1(
            acknowledge_exclusive_namespace=True, interval_seconds=86_400
        )
        state = ReportingOrphanCollectionStateV1()
        fx.write_fence(4)
        first = await run_orphan_collection_pass(fx.store, options, state)
        assert first.fenced == ("objects",)
        assert state.next_cycle_at is not None
        assert state.next_cycle_at - time.monotonic() <= 3_600 + 1, "looks again within the hour"
        # The operator fixes it; the next cycle (the hour elapsing) resumes collection.
        await fx.store.take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True)
        state.next_cycle_at = time.monotonic() - 1
        resumed = await run_orphan_collection_pass(fx.store, options, state)
        assert resumed.fenced == ()
        assert state.next_cycle_at is not None
        assert state.next_cycle_at - time.monotonic() > 3_600, "a clean cycle rests the interval"


def _later() -> Any:
    from ._orphan_support import later

    return later()


def test_the_hook_provider_is_a_real_provider_shape() -> None:
    assert HookedProvider.name == "filesystem"


async def test_a_listing_that_skipped_a_fence_entry_fails_closed_rather_than_reading_no_claim(
    tmp_path: Path,
) -> None:
    from adcp_reporting_storage.ledger.row_storage_object import ReportingRowObjectListPageV1

    async with orphan_fixture(tmp_path) as fx:
        await fx.put_object(fx.key("acct/orphan.jsonl"))
        # The provider could not address an entry in the fence directory: it might be the newest
        # claim, and an empty-looking chain would otherwise read as "never claimed".
        fx.provider.fence_list_override = ReportingRowObjectListPageV1((), None, skipped=1)
        result = await fx.collect(bindings=["objects"])
        assert fenced_cause(result) == "fence_unreadable"
        assert result.deleted == 0 and fx.on_disk(fx.key("acct/orphan.jsonl"))


async def test_a_cancelled_claim_leaves_no_advisory_lock_on_a_pooled_connection(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        await fx.collect(bindings=["objects"])
        storage = fx.store._row_storage
        assert storage is not None
        scope, _, _ = await storage.collector_fence_location("objects")
        holder = await fx.pool.getconn()
        try:
            await holder.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"adcp.orphan_fence:{scope}",)
            )
            task = asyncio.create_task(fx.collect(bindings=["objects"]))
            for _ in range(200):
                rows = await fetch_all(
                    fx.pool,
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted",
                )
                if rows[0][0]:
                    break
                await asyncio.sleep(0.025)
            task.cancel()
            (outcome,) = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(outcome, asyncio.CancelledError)
            await holder.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (f"adcp.orphan_fence:{scope}",),
            )
            await holder.commit()
        finally:
            await fx.pool.putconn(holder)
        held = await fetch_all(
            fx.pool,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND pid <> pg_backend_pid()",
        )
        assert held == [(0,)], "no pooled connection kept a session lock"
        again = await asyncio.wait_for(fx.collect(bindings=["objects"]), 20)
        assert again.fenced == ()


async def test_binding_name_arguments_must_be_sequences_of_ids_not_strings(
    tmp_path: Path,
) -> None:
    async with orphan_fixture(tmp_path) as fx:
        for name in ("bindings", "skip_bindings"):
            for bad in ("objects", "", b"objects", [1], ["objects", None]):
                with pytest.raises(ValueError, match=name):
                    await fx.collect(dry_run=True, **{name: bad})
        with pytest.raises(ValueError, match="bindings"):
            await fx.store.take_over_orphan_collector_fence(
                acknowledge_previous_owner_gone=True, bindings="objects"  # type: ignore[arg-type]
            )
