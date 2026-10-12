"""Object-bound row storage: lease-budgeted, fenced, create-only uploads and verified reads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore, ReportingRowStorageOptionsV1
from adcp.reporting.ledger.row_storage import (
    ReportingObjectRowBindingV1,
    ReportingPostgresRowBindingV1,
)
from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError
from adcp.reporting.ledger.row_storage_object import (
    ReportingRowObjectContextV1,
    ReportingRowObjectPutInputV1,
    create_filesystem_reporting_row_object_provider_v1,
)
from adcp.reporting.ledger.row_write import prepare_revision_row_set

from ._generation_support import isolated_reporting_pool
from .test_reporting_row_storage_pg import fetch_all, many_rows, seeded, typed_revision, walk


def options(tmp_path: Path, **overrides: Any) -> ReportingRowStorageOptionsV1:
    return ReportingRowStorageOptionsV1(
        deployment_namespace="test",
        bindings={
            "inline": ReportingPostgresRowBindingV1(),
            "reports": ReportingObjectRowBindingV1(
                provider="filesystem", location={"root": "rows"}, prefix="adcp-rows"
            ),
        },
        providers={
            "filesystem": create_filesystem_reporting_row_object_provider_v1(
                roots={"rows": tmp_path / "rows"}
            )
        },
        select_row_binding=lambda selection: (
            "reports" if selection.canonical_byte_count > 0 else "inline"
        ),
        **overrides,
    )


async def lease(pool: Any, *, seconds: float | None = 600) -> None:
    """Hold the worker lease the producer would hold while it publishes."""
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE reporting_configurations SET lease_worker_id = 'worker-1',"
            " lease_expires_at = CASE WHEN %s::float8 IS NULL THEN NULL"
            " ELSE clock_timestamp() + (%s::float8 * INTERVAL '1 second') END",
            (seconds, seconds),
        )


def list_objects(root: Path) -> list[str]:
    return sorted(
        str(Path(directory, name).relative_to(root))
        for directory, _, names in os.walk(root)
        for name in names
    )


async def test_an_object_bound_revision_uploads_create_only_chunks_and_serves_verified_pages(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(1234)
        revision = typed_revision(obligation, rows, suffix="obj")

        assert await store.stage_revision_rows(revision, rows) is True
        # The intent is open and names every object this upload created.
        (intent,) = await fetch_all(
            pool,
            "SELECT state, jsonb_array_length(created_objects) FROM adcp_reporting_row_write_intents",
        )
        assert intent == ("open", 1)
        await store.commit_revision(revision, rows)

        # The commit closed the intent and recorded the locator.
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_write_intents") == [
            (0,)
        ]
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_chunk_bodies") == [(0,)]
        (chunk,) = await fetch_all(
            pool,
            "SELECT object_key, native_version, compression, physical_byte_count"
            " FROM adcp_reporting_row_chunks",
        )
        key, version, compression, physical = chunk
        _, namespace = store._row_storage.installation_identity  # type: ignore[union-attr]
        assert key.startswith(f"adcp-rows/{namespace}/") and key.endswith("/000000.jsonl.gz")
        assert obligation.account_id not in key
        assert compression == "gzip" and version.count(":") == 3
        assert list_objects(tmp_path / "rows") == [key]
        assert (tmp_path / "rows" / key).stat().st_size == physical

        for limit in (7, 500):
            walked, _ = await walk(
                store, obligation.account_id, revision.reporting_revision_id, limit
            )
            assert walked == rows
        verified = await store.read_verified_revision_rows(
            account_id=obligation.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        assert list(verified) == rows
        binding = await fetch_all(
            pool,
            "SELECT provider, identity_config, operational_config FROM adcp_reporting_row_bindings"
            " WHERE row_binding_id = 'reports'",
        )
        # The identity names are the ones the JavaScript SDK hashes.
        assert binding[0][0] == "filesystem"
        assert set(binding[0][1]) == {"location", "prefix", "keyTemplate"}
        assert binding[0][2] == {"compression": "gzip"}


async def test_commit_revision_uploads_for_itself_outside_a_ledger_transaction(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(30)
        revision = typed_revision(obligation, rows, suffix="direct")
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.commit_revision(revision, rows)
        assert failure.value.code == "STATE_UNAVAILABLE"
        assert "stage" in str(failure.value)
        assert list_objects(tmp_path / "rows") == []
        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revisions") == [(0,)]


async def test_an_upload_the_lease_cannot_cover_is_refused_before_any_write(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(10)
        revision = typed_revision(obligation, rows, suffix="lease")

        await lease(pool, seconds=None)
        with pytest.raises(ReportingRowStoreError) as unleased:
            await store.stage_revision_rows(revision, rows)
        assert unleased.value.code == "STATE_UNAVAILABLE"

        # Thirty second deadline plus the five second margin needs more than 6 seconds.
        await lease(pool, seconds=6)
        with pytest.raises(ReportingRowStoreError) as short:
            await store.stage_revision_rows(revision, rows)
        assert short.value.code == "DEADLINE_EXCEEDED"
        assert list_objects(tmp_path / "rows") == []
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_write_intents") == [
            (0,)
        ]


async def test_identical_bytes_at_a_planned_key_are_adopted_and_never_swept(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(12)
        revision = typed_revision(obligation, rows, suffix="adopt")
        prepared = await prepare_revision_row_set(
            store, revision, rows, period_start="2026-09-01T00:00:00Z"
        )
        planned = prepared.objects[0]
        provider = create_filesystem_reporting_row_object_provider_v1(
            roots={"rows": tmp_path / "rows"}
        )
        await provider.put_if_absent(
            ReportingRowObjectPutInputV1(
                {"root": "rows"}, planned.key, planned.physical_bytes, "application/gzip", {}
            ),
            ReportingRowObjectContextV1(),
        )
        assert await store.stage_revision_rows(revision, rows) is True
        # Adopted objects are never recorded as created, so a sweep can never delete them.
        assert await fetch_all(
            pool, "SELECT created_objects FROM adcp_reporting_row_write_intents"
        ) == [([],)]
        await store.commit_revision(revision, rows)
        walked, _ = await walk(store, obligation.account_id, revision.reporting_revision_id, 500)
        assert walked == rows


async def test_different_bytes_at_a_planned_key_are_a_content_conflict(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(12)
        revision = typed_revision(obligation, rows, suffix="conflict")
        prepared = await prepare_revision_row_set(
            store, revision, rows, period_start="2026-09-01T00:00:00Z"
        )
        provider = create_filesystem_reporting_row_object_provider_v1(
            roots={"rows": tmp_path / "rows"}
        )
        await provider.put_if_absent(
            ReportingRowObjectPutInputV1(
                {"root": "rows"},
                prepared.objects[0].key,
                b"someone else's bytes",
                "application/gzip",
                {},
            ),
            ReportingRowObjectContextV1(),
        )
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.stage_revision_rows(revision, rows)
        assert failure.value.code == "CONTENT_CONFLICT"
        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revisions") == [(0,)]


async def test_a_commit_whose_intent_was_swept_never_commits_a_header(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(12)
        revision = typed_revision(obligation, rows, suffix="swept")
        await store.stage_revision_rows(revision, rows)
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents SET state = 'sweeping'"
            )
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.commit_revision(revision, rows)
        assert failure.value.code == "STATE_UNAVAILABLE"
        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revisions") == [(0,)]
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_sets") == [(0,)]


async def test_a_second_writer_cannot_take_over_a_live_intent_but_can_take_over_an_expired_one(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(12)
        revision = typed_revision(obligation, rows, suffix="takeover")
        await store.stage_revision_rows(revision, rows)
        first = await fetch_all(pool, "SELECT owner_token FROM adcp_reporting_row_write_intents")

        rival = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await rival.ready_row_storage()
        with pytest.raises(ReportingRowStoreError) as live:
            await rival.stage_revision_rows(revision, rows)
        assert live.value.code == "STATE_UNAVAILABLE"
        assert (
            await fetch_all(pool, "SELECT owner_token FROM adcp_reporting_row_write_intents")
            == first
        )

        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents"
                " SET expires_at = clock_timestamp() - INTERVAL '1 second'"
            )
        assert await rival.stage_revision_rows(revision, rows) is True
        second = await fetch_all(pool, "SELECT owner_token FROM adcp_reporting_row_write_intents")
        assert second != first
        # The original owner lost the fence: its commit cannot land.
        with pytest.raises(ReportingRowStoreError):
            await store.commit_revision(revision, rows)
        await rival.commit_revision(revision, rows)
        walked, _ = await walk(rival, obligation.account_id, revision.reporting_revision_id, 500)
        assert walked == rows


async def test_unchanged_rows_share_the_previous_revisions_chunks(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(40)
        first = typed_revision(obligation, rows, suffix="a")
        second = typed_revision(
            obligation, rows, suffix="b", supersedes=first.reporting_revision_id
        )
        for revision in (first, second):
            await store.stage_revision_rows(revision, rows)
            await store.commit_revision(revision, rows)

        # One upload serves both revisions; the second is a header-only revision.
        assert len(list_objects(tmp_path / "rows")) == 1
        assert await fetch_all(
            pool,
            "SELECT row_set_id, rows_shared_from_row_set_id FROM adcp_reporting_row_sets ORDER BY 1",
        ) == [
            (first.reporting_revision_id, None),
            (second.reporting_revision_id, first.reporting_revision_id),
        ]
        assert await fetch_all(
            pool,
            "SELECT count(DISTINCT object_key), count(*) FROM adcp_reporting_row_chunks",
        ) == [(1, 2)]
        for revision in (first, second):
            walked, _ = await walk(
                store, obligation.account_id, revision.reporting_revision_id, 500
            )
            assert walked == rows
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_write_intents") == [
            (0,)
        ]


async def test_reads_fail_closed_on_missing_tampered_and_unconfigured_objects(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(50)
        revision = typed_revision(obligation, rows, suffix="victim")
        await store.stage_revision_rows(revision, rows)
        await store.commit_revision(revision, rows)
        (key,) = [
            row[0]
            for row in await fetch_all(pool, "SELECT object_key FROM adcp_reporting_row_chunks")
        ]
        target = tmp_path / "rows" / key

        async def read(reader: Any = store) -> Any:
            return await reader.read_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )

        # Same size, same version, different bytes: only the digests can catch it.
        original = target.read_bytes()
        stat = target.stat()
        target.write_bytes(bytes(b ^ 1 for b in original))
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with pytest.raises(ReportingRowStoreError) as tampered:
            await read()
        assert tampered.value.code == "ROWS_INTEGRITY_FAILED"
        target.write_bytes(original)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert len((await read()).rows) == 50

        # A binding the host did not configure is never read from.
        bare = PgReportingLedgerStore(pool=pool)
        with pytest.raises(ReportingRowStoreError) as unconfigured:
            await read(bare)
        assert unconfigured.value.code == "STATE_UNAVAILABLE"
        different = PgReportingLedgerStore(
            pool=pool,
            row_storage=ReportingRowStorageOptionsV1(
                deployment_namespace="test",
                bindings={
                    "reports": ReportingObjectRowBindingV1(
                        provider="filesystem", location={"root": "rows"}, prefix="elsewhere"
                    )
                },
                providers=options(tmp_path).providers,
            ),
        )
        with pytest.raises(ReportingRowStoreError) as redirected:
            await different.ready_row_storage()
        assert redirected.value.code == "UNSAFE_BINDING"

        target.unlink()
        with pytest.raises(ReportingRowStoreError) as lost:
            await read()
        assert lost.value.code == "ROWS_UNAVAILABLE"


def test_bindings_are_validated_before_any_write(tmp_path: Path) -> None:
    providers = options(tmp_path).providers

    def make(**binding: Any) -> None:
        from adcp.reporting.ledger.row_storage import ReportingRowStorageV1

        defaults: dict[str, Any] = {
            "provider": "filesystem",
            "location": {"root": "rows"},
            "prefix": "adcp-rows",
        }
        ReportingRowStorageV1(
            ReportingRowStorageOptionsV1(
                bindings={"reports": ReportingObjectRowBindingV1(**{**defaults, **binding})},
                providers=providers,
            )
        )

    make()
    for bad in (
        {"provider": "unknown"},
        {"location": {"root": "missing"}},
        {"location": {"root": "rows", "path": "/etc"}},
        {"prefix": "../up"},
        {"key_template": "{content_sha256}/{chunk_index}"},
        {"compression": "zstd"},
        {"credential_ref": "has space"},
    ):
        with pytest.raises(ReportingRowStoreError) as failure:
            make(**bad)
        assert failure.value.code == "INVALID_INPUT", bad
    with pytest.raises(ReportingRowStoreError):
        storage_with_bindings({"bad id": ReportingPostgresRowBindingV1()})


def storage_with_bindings(bindings: Any) -> Any:
    from adcp.reporting.ledger.row_storage import ReportingRowStorageV1

    return ReportingRowStorageV1(ReportingRowStorageOptionsV1(bindings=bindings))


async def test_the_producer_stages_object_uploads_outside_its_publication_transaction(
    tmp_path: Path,
) -> None:
    from adcp.reporting.ledger import ReportingProducer
    from tests.test_reporting_settling import _capabilities, _harness, _revisions

    async with isolated_reporting_pool() as pool:
        producer, store, _, _ = await _harness(
            _capabilities(restatement_window="P3D"),
            store_factory=lambda clock: PgReportingLedgerStore(
                pool=pool, clock=clock, row_storage=options(tmp_path)
            ),
        )
        assert isinstance(producer, ReportingProducer)
        turn = await producer.run_worker()
        assert turn.revisions_committed
        (revision,) = await _revisions(store)
        assert len(list_objects(tmp_path / "rows")) == 1
        page = await store.read_revision_rows(
            account_id=revision.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        assert len(page.rows) == revision.row_count
        verified = await store.read_verified_revision_rows(
            account_id=revision.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        assert len(verified) == revision.row_count
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_write_intents") == [
            (0,)
        ]


async def test_object_deletes_keep_referenced_objects_and_adopters_fail_closed(
    tmp_path: Path,
) -> None:
    from adcp.reporting.ledger.row_storage import ReportingRowChunkLocatorV1

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(10)
        revision = typed_revision(obligation, rows, suffix="shared")
        assert await store.stage_revision_rows(revision, rows) is True
        await store.commit_revision(revision, rows)
        storage = store._row_storage
        assert storage is not None
        binding, key, version, physical_sha, physical_size = (
            await fetch_all(
                pool,
                "SELECT s.row_binding_id, c.object_key, c.native_version, c.physical_sha256,"
                " c.physical_byte_count FROM adcp_reporting_row_chunks c"
                " JOIN adcp_reporting_row_sets s USING (row_set_id)",
            )
        )[0]

        # A live chunk references the object: no delete path may take it.
        assert await storage.delete_unreferenced(pool, binding, [(key, version)]) == 0
        assert len(list_objects(tmp_path / "rows")) == 1
        # Retirement scopes the delete to its own obligation, so those references do not count.
        assert (
            await storage.delete_unreferenced(
                pool, binding, [(key, version)], obligation_id=obligation.reporting_obligation_id
            )
            == 1
        )
        assert list_objects(tmp_path / "rows") == []

        # A commit that adopted the object now finds it gone and fails closed.
        locator = ReportingRowChunkLocatorV1(
            key, version, "gzip", physical_sha, int(physical_size), created=False
        )
        async with pool.connection() as connection, connection.transaction():
            with pytest.raises(ReportingRowStoreError) as gone:
                await storage.lock_and_verify_adopted(connection, binding, [locator])
        assert gone.value.code == "STATE_UNAVAILABLE"


async def test_the_object_lock_uses_the_text_the_javascript_sdk_hashes(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path))
        await store.create_schema()
        storage = store._row_storage
        assert storage is not None
        text = storage._object_lock_text("reports", "adcp-rows/ns/a/b")
        assert text == "adcp-reporting-object:reports:adcp-rows/ns/a/b"
        # Hashing in SQL is what guarantees both SDKs derive one lock id from that text.
        async with pool.connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (text,)
            )
            (held,) = await (
                await connection.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                    " AND pid = pg_backend_pid()"
                )
            ).fetchone()
            assert held == 1


async def test_a_failed_intent_record_leaves_the_object_and_never_deletes_it(
    tmp_path: Path,
) -> None:
    """The intent may have been taken over by a writer that already adopted and committed the
    object, so the failing upload must leak storage rather than delete a committed version."""
    from dataclasses import replace

    deletes: list[Any] = []
    inner = create_filesystem_reporting_row_object_provider_v1(roots={"rows": tmp_path / "rows"})

    class Spy:
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
            deletes.append(delete)
            return await inner.delete(delete, context)

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(
            pool=pool, row_storage=replace(options(tmp_path), providers={"filesystem": Spy()})
        )
        await store.create_schema()
        obligation = await seeded(store)
        await lease(pool)
        rows = many_rows(12)
        revision = typed_revision(obligation, rows, suffix="taken")
        prepared = await prepare_revision_row_set(
            store,
            revision,
            rows,
            period_start="2026-09-01T00:00:00Z",
        )
        storage = store._row_storage
        assert storage is not None

        async def taken_over(_key: str, _version: str) -> None:
            raise ReportingRowStoreError("STATE_UNAVAILABLE", "the intent was taken over")

        with pytest.raises(ReportingRowStoreError) as failed:
            await storage.upload(prepared, "intent", taken_over)
        assert failed.value.code == "STATE_UNAVAILABLE"
        assert deletes == []
        assert len(list_objects(tmp_path / "rows")) == 1
