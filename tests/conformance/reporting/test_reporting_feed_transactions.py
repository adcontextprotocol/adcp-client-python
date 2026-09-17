"""Snapshot faults and races cannot change the approved writer transactions."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.feed import PgReportingFeedStore, ReportingFeedError
from adcp.reporting.feed.snapshot import StoredFeedSnapshot

from ._durable_materializer_support import DurableHarness, durable_case
from ._feed_support import feed_request, feeds, mixed_case, restart, walk, without_feed
from ._generation_support import isolated_reporting_pool
from ._receipt_support import receipt_case, request_for
from ._reconciliation_support import Clock

__all__ = ["feeds"]


async def test_snapshot_identity_collision_cannot_replace_an_open_walk(feeds, monkeypatch):
    from adcp.reporting.feed import projection

    h = feeds
    s, _, _ = await mixed_case(h)
    req = feed_request(s)
    first = await h.store.read_reporting_feed(req, caller=s.binding.principal)
    expected = await walk(h.store, req, s.binding.principal, first=first)
    monkeypatch.setattr(projection, "uuid4", lambda: UUID(first["ledger_snapshot_id"][5:]))
    before = await h.image()
    with pytest.raises(ReportingFeedError) as error:
        await h.store.read_reporting_feed(req, caller=s.binding.principal)
    assert error.value.code == "REPORTING_FEED_STORAGE_UNAVAILABLE"
    assert await h.image() == before
    assert await walk(h.store, req, s.binding.principal, first=first) == expected


@pytest.mark.parametrize("damage", ["missing", "record-kind", "signature"])
async def test_lost_or_corrupt_snapshot_fails_without_reconstruction(feeds, damage, monkeypatch):
    h = feeds
    s, _, _ = await mixed_case(h)
    first = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    snapshot_id = first["ledger_snapshot_id"]
    if h.pool is None:
        document, _, signing_key = h.store._reporting_feed_snapshots[snapshot_id]
    else:
        async with h.pool.connection() as c:
            document, signing_key = await (
                await c.execute(
                    "SELECT document, signing_key FROM reporting_feed_snapshots"
                    " WHERE snapshot_id=%s",
                    (snapshot_id,),
                )
            ).fetchone()
    if damage == "record-kind":
        value = json.loads(document)
        value["records"][0]["kind"] = "destination_binding"
        document = canonical_json_utf8_v1(value)
    elif isinstance(document, str):
        document = document.encode()
    if damage == "signature":
        signing_key = b"x" * 32
    digest = hashlib.sha256(document).hexdigest()
    if h.pool is None:
        if damage == "missing":
            del h.store._reporting_feed_snapshots[snapshot_id]
        else:
            h.store._reporting_feed_snapshots[snapshot_id] = (document, digest, signing_key)

        def forbidden_capture(*args, **kwargs):
            raise AssertionError("a damaged continuation cannot open a new snapshot")

        monkeypatch.setattr(h.store, "_capture_feed", forbidden_capture)
    else:
        # Simulate operator loss/corruption without changing the schema guards.
        async with h.pool.connection() as c, c.transaction():
            await c.execute("SET LOCAL session_replication_role=replica")
            if damage == "missing":
                await c.execute(
                    "DELETE FROM reporting_feed_snapshots WHERE snapshot_id=%s", (snapshot_id,)
                )
            else:
                await c.execute(
                    "UPDATE reporting_feed_snapshots SET document=%s,content_sha256=%s,"
                    "signing_key=%s WHERE snapshot_id=%s",
                    (document.decode(), digest, signing_key, snapshot_id),
                )

        async def forbidden_capture(*args, **kwargs):
            raise AssertionError("a damaged continuation cannot open a new snapshot")

        monkeypatch.setattr(h.store, "_capture_feed_on", forbidden_capture)
    before = await h.image()
    for req in (
        feed_request(s, pagination={"cursor": first["pagination"]["cursor"]}),
        feed_request(s, changes_after=first["changes_checkpoint"]),
    ):
        with pytest.raises(ReportingFeedError) as error:
            await h.store.read_reporting_feed(req, caller=s.binding.principal)
        assert error.value.code == (
            "REPORTING_FEED_HISTORY_CORRUPT" if damage == "record-kind" else "INVALID_CHECKPOINT"
        )
    assert await h.image() == before


@pytest.mark.parametrize("fault", ["capture", "insert", "assembly"])
async def test_every_snapshot_fault_rolls_back_all_collections_heads_and_upstream_history(
    feeds, monkeypatch, fault
):
    h = feeds
    s, request, response = await mixed_case(h)
    before = await h.image()
    cls = type(h.store)
    if fault == "assembly":

        def failing(*args, **kwargs):
            raise RuntimeError("injected feed assembly failure")

        monkeypatch.setattr(StoredFeedSnapshot, "page", failing)
    elif h.pool is None:
        name = "_capture_feed" if fault == "capture" else "_save_feed_snapshot"
        original = getattr(cls, name)

        def failing(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected feed persistence failure")

        monkeypatch.setattr(cls, name, failing)
    else:
        name = "_capture_feed_on" if fault == "capture" else "_save_feed_snapshot_on"
        original = getattr(cls, name)

        async def failing(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("injected feed persistence failure")

        monkeypatch.setattr(cls, name, failing)
    with pytest.raises((RuntimeError, ReportingFeedError)):
        await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    assert await h.image() == before
    monkeypatch.undo()
    await restart(h)
    assert await h.store.ingest_receipt_batch(request, caller=s.binding.principal) == response
    assert (await walk(h.store, feed_request(s), s.binding.principal))[0][-1]["pagination"][
        "total_count"
    ] == 6
    assert without_feed(await h.image()) == without_feed(before)


@pytest.mark.parametrize("missing", ["obligation", "revision", "adjustment", "attempt"])
async def test_missing_references_fail_new_snapshot_but_never_rebuild_persisted_continuation(
    feeds, missing
):
    h = feeds
    s, _, _ = await mixed_case(h)
    request = feed_request(s)
    first = await h.store.read_reporting_feed(request, caller=s.binding.principal)
    expected = await walk(h.store, request, s.binding.principal, first=first)
    if h.pool is None:
        if missing == "obligation":
            del h.store._obligations[s.obligation.reporting_obligation_id]
        elif missing == "revision":
            del h.store._revisions[s.revision.reporting_revision_id]
        elif missing == "adjustment":
            del h.store._adjustments["adjustment-1"]
        else:
            h.store._delivery_records = [
                (seq, who, record)
                for seq, who, record in h.store._delivery_records
                if record.kind != "materialization_attempt"
            ]
    else:
        # An operator can bypass triggers. The persisted snapshot is independent
        # of lost current rows; opening a new snapshot must diagnose the loss.
        statements = {
            "obligation": (
                "DELETE FROM reporting_obligations WHERE reporting_obligation_id=%s",
                s.obligation.reporting_obligation_id,
            ),
            "revision": (
                "DELETE FROM reporting_revisions WHERE reporting_revision_id=%s",
                s.revision.reporting_revision_id,
            ),
            "adjustment": (
                "DELETE FROM reporting_adjustments WHERE reporting_adjustment_id=%s",
                "adjustment-1",
            ),
            "attempt": (
                "DELETE FROM reporting_reconciliation_records"
                " WHERE record_kind='materialization_attempt' AND reporting_materialization_id=%s",
                s.outcome.reporting_materialization_id,
            ),
        }
        async with h.pool.connection() as c, c.transaction():
            await c.execute("SET LOCAL session_replication_role=replica")
            query, value = statements[missing]
            await c.execute(query, (value,))
    store = await restart(h)
    before = await h.image()
    assert await walk(store, request, s.binding.principal, first=first) == expected
    with pytest.raises(ReportingFeedError) as error:
        await store.read_reporting_feed(request, caller=s.binding.principal)
    assert error.value.code == "REPORTING_FEED_HISTORY_CORRUPT"
    assert await h.image() == before


async def test_verified_finish_capture_and_epoch_zero_queue_are_frozen_without_read_side_effects(
    feeds,
):
    h = feeds
    case = await durable_case(h.store, count=3)
    assert (await case.service().run_once()).state == "verified"
    boundaries = await h.store.read_materializer_boundaries(caller=case.scope.principal)
    assert len(boundaries) == 1
    request = {
        "account": {"account_id": case.config.account_id},
        "view": "periods",
        "pagination": {"max_results": 1},
    }
    before = without_feed(await h.image())
    queue = await h.queue()
    page = await h.store.read_reporting_feed(request, caller=case.scope.principal)
    snapshot = await h.store.read_reporting_feed_snapshot(
        page["ledger_snapshot_id"], caller=case.scope.principal
    )
    assert snapshot.inputs["materializer_boundaries"] == [b.to_storage() for b in boundaries]
    assert snapshot.inputs["ownership_mode"] == snapshot.ownership_mode == "absent"
    assert without_feed(await h.image()) == before
    assert await h.queue() == queue
    assert set(queue[1]) <= {"quarantined"}
    await restart(h)
    assert await h.store.read_materializer_boundaries(caller=case.scope.principal) == boundaries
    assert await h.queue() == queue


async def test_postgres_captures_on_one_connection_under_account_lock_and_uses_database_time(
    monkeypatch,
):
    from psycopg_pool import AsyncConnectionPool

    async with isolated_reporting_pool(autocommit=True) as outer:
        async with AsyncConnectionPool(
            outer.conninfo, kwargs=outer.kwargs, min_size=1, max_size=1, open=False
        ) as pool:
            store = PgReportingFeedStore(pool=pool, clock=Clock())
            await store.create_schema()
            h = DurableHarness(store, store._clock, pool)
            s, _, _ = await mixed_case(h)
            h.clock.now = datetime(2000, 1, 1, tzinfo=timezone.utc)
            original = store._capture_feed_on
            connections = []

            async def capture(connection, *args, **kwargs):
                connections.append(connection)
                return await original(connection, *args, **kwargs)

            save = store._save_feed_snapshot_on

            async def persist(connection, *args, **kwargs):
                connections.append(connection)
                return await save(connection, *args, **kwargs)

            monkeypatch.setattr(store, "_capture_feed_on", capture)
            monkeypatch.setattr(store, "_save_feed_snapshot_on", persist)
            async with pool.connection() as c:
                before = (await (await c.execute("SELECT clock_timestamp()")).fetchone())[0]
            page = await asyncio.wait_for(
                store.read_reporting_feed(feed_request(s), caller=s.binding.principal), 10
            )
            async with pool.connection() as c:
                after = (await (await c.execute("SELECT clock_timestamp()")).fetchone())[0]
            assert (
                before
                <= datetime.fromisoformat(page["ledger_as_of"].replace("Z", "+00:00"))
                <= after
            )
            assert len(connections) == 2 and connections[0] is connections[1]


async def test_same_account_receipt_writer_waits_for_complete_snapshot_then_is_deferred(
    monkeypatch,
):
    from ._feed_support import feed_harness

    async with feed_harness("postgres", notifications=True) as h:
        s = await receipt_case(h)
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.store._capture_feed_on

        async def pause(connection, *args, **kwargs):
            result = await original(connection, *args, **kwargs)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(h.store, "_capture_feed_on", pause)
        reader = asyncio.create_task(
            h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
        )
        writer = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            writer = asyncio.create_task(
                h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(writer), 0.1)
            release.set()
            first = await asyncio.wait_for(reader, 10)
            receipt = await asyncio.wait_for(writer, 10)
        finally:
            release.set()
            for task in (reader, writer):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        monkeypatch.undo()
        _, rows, checkpoint = await walk(h.store, feed_request(s), s.binding.principal, first=first)
        assert rows["receipts"] == []
        _, later, _ = await walk(
            h.store, feed_request(s, changes_after=checkpoint), s.binding.principal
        )
        assert later["receipts"] == [receipt["results"][0]["receipt"]]
        assert (
            len(later["periods"]) == len(later["revisions"]) == len(later["materializations"]) == 1
        )
