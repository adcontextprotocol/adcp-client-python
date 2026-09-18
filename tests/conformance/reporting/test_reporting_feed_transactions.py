"""Snapshot faults and races cannot change the approved writer transactions."""

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.feed import PgReportingFeedStore, ReportingFeedError
from adcp.reporting.feed.snapshot import StoredFeedSnapshot

from ._durable_materializer_support import DurableHarness, durable_case
from ._feed_support import (
    MountedFeed,
    feed_harness,
    feed_request,
    feeds,
    mixed_case,
    restart,
    walk,
    without_feed,
)
from ._generation_support import isolated_reporting_pool
from ._receipt_support import receipt_case, request_for
from ._receipt_transport import error_code
from ._reconciliation_support import Clock

__all__ = ["feeds"]


@asynccontextmanager
async def paused_feed_projection(monkeypatch, *, failure=False):
    """Pause the real pure projector, without blocking the test event loop."""
    from adcp.reporting.feed import pg

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    state = SimpleNamespace(
        entered=asyncio.Event(),
        release=threading.Event(),
        finished=asyncio.Event(),
        threads=[],
    )
    original = pg.capture_feed

    def pause(*args, **kwargs):
        state.threads.append(threading.get_ident())
        loop.call_soon_threadsafe(state.entered.set)
        try:
            if threading.get_ident() == loop_thread:
                raise RuntimeError("feed projection must not block the writer event loop")
            assert state.release.wait(30), "projection pause was not released"
            if failure:
                raise RuntimeError("private-projection-provider-failure")
            return original(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(state.finished.set)

    with monkeypatch.context() as patch:
        patch.setattr(pg, "capture_feed", pause)
        try:
            yield state
        finally:
            state.release.set()
            if state.entered.is_set():
                await asyncio.wait_for(state.finished.wait(), 10)


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("pool_size", [1, 2])
@pytest.mark.parametrize("writer_kind", ["receipt", "materializer"])
async def test_postgres_writers_commit_while_projection_is_paused_and_stay_out_of_frozen_walk(
    monkeypatch, notifications, pool_size, writer_kind
):
    async with isolated_reporting_pool(autocommit=True) as outer:
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(
            outer.conninfo,
            kwargs=outer.kwargs,
            min_size=1,
            max_size=pool_size,
            open=False,
        ) as pool:
            store = PgReportingFeedStore(pool=pool, notifications=notifications)
            await store.create_schema()
            h = DurableHarness(store, Clock(), pool)
            s = await receipt_case(h) if writer_kind == "receipt" else await durable_case(store)
            caller = s.binding.principal
            req = feed_request(s)
            authorized = []

            async def reauthorize():
                # A real ACL can share this size-one pool without waiting for
                # the feed's capture or publication connection to be released.
                async with pool.connection() as connection:
                    row = await (
                        await connection.execute("SELECT count(*) FROM reporting_feed_snapshots")
                    ).fetchone()
                    assert row == (0,)
                authorized.append(caller)

            async with paused_feed_projection(monkeypatch) as pause:
                reader = asyncio.create_task(
                    store.read_reporting_feed(req, caller=caller, reauthorize=reauthorize)
                )
                try:
                    await asyncio.wait_for(pause.entered.wait(), 10)
                    assert len(pause.threads) == 1
                    assert pause.threads[0] != threading.get_ident()
                    assert not reader.done()
                    if writer_kind == "receipt":
                        result = await asyncio.wait_for(
                            store.ingest_receipt_batch(request_for(s), caller=caller), 10
                        )
                        assert result["results"][0]["result"] == "recorded"
                    else:
                        result = await asyncio.wait_for(s.service().run_once(), 10)
                        assert result.state == "verified"
                    # A mutable input changes after capture, before projection.
                    await asyncio.wait_for(
                        store.set_revision_readable(
                            account_id=caller.account_id,
                            reporting_revision_id=s.revision.reporting_revision_id,
                            readable=False,
                        ),
                        10,
                    )
                    written = await asyncio.wait_for(h.image(), 10)
                    assert written["reporting_feed_snapshots"] == []
                    assert authorized == []
                    assert not reader.done()
                    pause.release.set()
                    first = await asyncio.wait_for(reader, 10)
                    assert authorized == [caller]
                finally:
                    pause.release.set()
                    if not reader.done():
                        reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
            snapshot = await store.read_reporting_feed_snapshot(
                first["ledger_snapshot_id"], caller=caller
            )
            assert snapshot.inputs["core"]["revisions"][0]["readable"] is True
            assert snapshot.inputs["receipt_boundaries"] == []
            assert snapshot.inputs["materializer_boundaries"] == []
            _, records, checkpoint = await walk(store, req, caller, first=first)
            assert records["receipts"] == []
            assert len(records["materializations"]) == int(writer_kind == "receipt")
            assert without_feed(await h.image()) == without_feed(written)
            fresh = await restart(h)
            _, changes, _ = await walk(fresh, feed_request(s, changes_after=checkpoint), caller)
            assert len(changes["periods"]) == len(changes["revisions"]) == 1
            if writer_kind == "receipt":
                assert changes["receipts"] == [result["results"][0]["receipt"]]
                assert await fresh.ingest_receipt_batch(request_for(s), caller=caller) == result
            else:
                assert len(changes["materializations"]) == 1
                assert changes["receipts"] == []
                assert set((await h.queue())[1]) <= {"quarantined"}


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("revocation", ["account-grant", "registry", "account-remap"])
async def test_mounted_revocation_during_projection_denies_publication_and_preserves_continuation(
    monkeypatch, notifications, revocation
):
    async with feed_harness("postgres", notifications=notifications) as h:
        s, _, _ = await mixed_case(h)
        other = await receipt_case(h, account_id="feed-remapped-account")
        mounted = MountedFeed(h, registry_kind="oauth")
        mounted.authorize(s)
        mounted.authorize(other, token="token-other")
        req = feed_request(s)
        async with mounted.client() as client:
            _, first = await mounted.mcp(client, req)
            old = feed_request(s, pagination={"cursor": first["pagination"]["cursor"]})
            before = await h.image()
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                async with paused_feed_projection(monkeypatch) as pause:
                    call = (
                        mounted.mcp(client, req)
                        if transport == "mcp"
                        else mounted.a2a(client, req, v1=transport == "a2a-1.0")
                    )
                    reader = asyncio.create_task(call)
                    try:
                        await asyncio.wait_for(pause.entered.wait(), 10)
                        assert pause.threads[0] != threading.get_ident()
                        assert not reader.done()
                        if revocation == "account-grant":
                            mounted.grants.remove((s.obligation.account_id, s.binding.consumer_id))
                        elif revocation == "registry":
                            mounted.registry.agents.clear()
                        else:
                            mounted.accounts[s.obligation.account_id] = other.obligation.account_id
                        pause.release.set()
                        _, denied = await asyncio.wait_for(reader, 10)
                        assert error_code(denied) == "UNAUTHORIZED", denied
                        assert "ledger_snapshot_id" not in denied
                    finally:
                        pause.release.set()
                        if not reader.done():
                            reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                assert await h.image() == before
                _, denied = await mounted.a2a(client, old)
                # The remapped account is authorized, but cannot restore the
                # original account's snapshot. Revoked grants fail earlier.
                expected = "INVALID_CHECKPOINT" if revocation == "account-remap" else "UNAUTHORIZED"
                assert error_code(denied) == expected, denied
                assert "ledger_snapshot_id" not in denied
                mounted.authorize(s)
                _, resumed = await mounted.a2a(client, old)
                assert resumed["ledger_snapshot_id"] == first["ledger_snapshot_id"]
                assert resumed["changes_checkpoint"] == first["changes_checkpoint"]
                assert await h.image() == before


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("fault", ["projection", "cancelled", "insert"])
async def test_postgres_projection_failure_or_cancellation_cannot_undo_concurrent_receipt(
    monkeypatch, notifications, fault
):
    async with feed_harness("postgres", notifications=notifications) as h:
        s = await receipt_case(h)
        caller = s.binding.principal
        if fault == "insert":
            save = h.store._save_feed_snapshot_on

            async def fail_after_insert(*args, **kwargs):
                await save(*args, **kwargs)
                raise RuntimeError("private-snapshot-insert-failure")

            monkeypatch.setattr(h.store, "_save_feed_snapshot_on", fail_after_insert)
        async with paused_feed_projection(monkeypatch, failure=fault == "projection") as pause:
            reader = asyncio.create_task(
                h.store.read_reporting_feed(feed_request(s), caller=caller)
            )
            try:
                await asyncio.wait_for(pause.entered.wait(), 10)
                assert pause.threads[0] != threading.get_ident()
                response = await asyncio.wait_for(
                    h.store.ingest_receipt_batch(request_for(s), caller=caller), 10
                )
                written = await h.image()
                if fault == "cancelled":
                    reader.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await reader
                    pause.release.set()
                else:
                    pause.release.set()
                    with pytest.raises(ReportingFeedError) as error:
                        await asyncio.wait_for(reader, 10)
                    assert error.value.code == "REPORTING_FEED_STORAGE_UNAVAILABLE"
                    assert error.value.__context__ is None
                    assert "private-" not in repr(error.value)
            finally:
                pause.release.set()
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        assert await h.image() == written
        await restart(h)
        assert await h.store.ingest_receipt_batch(request_for(s), caller=caller) == response
        assert await h.image() == written


@pytest.mark.parametrize("notifications", [False, True])
async def test_postgres_feed_refuses_caller_transaction_before_reading_uncommitted_history(
    notifications,
):
    async with feed_harness("postgres", notifications=notifications) as h:
        s = await receipt_case(h)
        before = await h.image()
        with pytest.raises(RuntimeError, match="rollback caller write"):
            async with h.store.transaction():
                await h.store.set_revision_readable(
                    account_id=s.obligation.account_id,
                    reporting_revision_id=s.revision.reporting_revision_id,
                    readable=False,
                )
                with pytest.raises(ReportingFeedError) as error:
                    await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
                assert error.value.code == "REPORTING_FEED_TRANSACTION_UNAVAILABLE"
                raise RuntimeError("rollback caller write")
        assert await h.image() == before


@pytest.mark.parametrize("failure", ["denied", "cancelled", "provider"])
async def test_failed_reauthorization_leaves_no_snapshot_or_changed_history(feeds, failure):
    h = feeds
    s, request, response = await mixed_case(h)
    before = await h.image()
    calls = []

    async def reauthorize():
        calls.append(s.binding.principal)
        if failure == "denied":
            raise ReportingFeedError("UNAUTHORIZED")
        if failure == "cancelled":
            raise asyncio.CancelledError
        raise RuntimeError("private-authorization-provider-failure")

    expected = asyncio.CancelledError if failure == "cancelled" else ReportingFeedError
    with pytest.raises(expected) as error:
        await h.store.read_reporting_feed(
            feed_request(s), caller=s.binding.principal, reauthorize=reauthorize
        )
    assert calls == [s.binding.principal]
    if failure != "cancelled":
        assert error.value.code == (
            "UNAUTHORIZED" if failure == "denied" else "REPORTING_FEED_STORAGE_UNAVAILABLE"
        )
        assert error.value.__context__ is None
        assert "private-" not in repr(error.value)
    assert await h.image() == before
    await restart(h)
    assert await h.store.ingest_receipt_batch(request, caller=s.binding.principal) == response
    assert await h.image() == before


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


@pytest.mark.parametrize("fault", ["capture", "insert", "assembly", "context"])
async def test_every_snapshot_fault_rolls_back_all_collections_heads_and_upstream_history(
    feeds, monkeypatch, fault
):
    h = feeds
    s, request, response = await mixed_case(h)
    before = await h.image()
    cls = type(h.store)
    if fault in {"assembly", "context"}:

        def failing(*args, **kwargs):
            raise RuntimeError("injected feed assembly failure")

        if fault == "assembly":
            monkeypatch.setattr(StoredFeedSnapshot, "page", failing)
        else:
            from adcp.reporting.feed import memory, pg

            monkeypatch.setattr(memory if h.pool is None else pg, "inject_context", failing)
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
    async with isolated_reporting_pool(autocommit=True) as outer:
        from psycopg_pool import AsyncConnectionPool

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
            transactions = []

            async def capture(connection, *args, **kwargs):
                connections.append(connection)
                transactions.append(
                    (await (await connection.execute("SELECT txid_current()")).fetchone())[0]
                )
                async with outer.connection() as observer, observer.transaction():
                    acquired = (
                        await (
                            await observer.execute(
                                "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                                (f"adcp.reporting:{s.obligation.account_id}",),
                            )
                        ).fetchone()
                    )[0]
                    assert acquired is False
                return await original(connection, *args, **kwargs)

            save = store._save_feed_snapshot_on

            async def persist(connection, *args, **kwargs):
                connections.append(connection)
                transactions.append(
                    (await (await connection.execute("SELECT txid_current()")).fetchone())[0]
                )
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
            assert transactions[0] != transactions[1]


async def test_same_account_receipt_writer_waits_only_for_database_capture_then_is_deferred(
    monkeypatch,
):
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
