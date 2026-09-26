"""Bounded storage, real database winners and committed-seal restart recovery.

Set ADCP_PG_TEST_URL to an expendable database; each case owns a schema.
No provider, external endpoint, production data or service factory is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pytest

from adcp.reporting.conformance import validate_reporting_source_execution
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import InlineReportingSource, SealedSlice
from adcp.reporting.inline_storage import (
    INLINE_STAGING_MAX_BYTES,
    InlineStorageError,
    PgReportingSealStore,
    PgReportingStagingStore,
    PreparedBackendObjectV1,
    PreparedBackendSealV1,
)
from adcp.reporting.source import source_batch_manifest_reference_v1

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
ROW = {
    "media_buy_id": "media-buy-redacted",
    "campaign_id": "campaign-redacted-1",
    "impressions": 10,
    "spend": "1.25",
}


@pytest.fixture
async def database():
    dsn = os.environ.get("ADCP_PG_TEST_URL")
    if not dsn:
        pytest.skip("ADCP_PG_TEST_URL is not configured")
    psycopg = pytest.importorskip("psycopg")
    pools = pytest.importorskip("psycopg_pool")
    schema = "inline_test_" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema))
        )
        try:
            async with pools.AsyncConnectionPool(
                dsn,
                kwargs={"options": f"-c search_path={schema}"},
                min_size=1,
                max_size=1,
                open=False,
            ) as pool:
                yield pool, schema, dsn
        finally:
            await admin.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
            )


@pytest.fixture
async def stores(database):
    pool, _, _ = database
    staging = PgReportingStagingStore(pool=pool)
    seals = PgReportingSealStore(pool=pool)
    await staging.create_schema()
    return staging, seals, pool


def source(staging, seals, fetch=None):
    return InlineReportingSource(
        capabilities=redacted_capabilities(),
        fetch=fetch or (lambda _: [ROW]),
        staging=staging,
        seals=seals,
        clock=lambda: NOW,
    )


async def sample_seal(value=10):
    result = await InlineReportingSource(
        capabilities=redacted_capabilities(),
        fetch=lambda _: [{**ROW, "impressions": value}],
        clock=lambda: NOW,
    ).execute(redacted_snapshot_request(), cancel=asyncio.Event())
    assert result.manifest_bytes is not None
    return SealedSlice(reference=result.response.manifest, manifest_bytes=result.manifest_bytes)


def test_driver_absent_import_and_constructor() -> None:
    script = """
import importlib.abc, sys
class MissingDriver(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'psycopg', 'psycopg_pool'}:
            raise ModuleNotFoundError('driver deliberately absent')
sys.meta_path.insert(0, MissingDriver())
from adcp.reporting.inline_storage import PgReportingStagingStore, PgReportingSealStore
for cls in [PgReportingStagingStore, PgReportingSealStore]:
    try:
        cls(pool=object())
    except ImportError as error:
        assert str(error) == 'PostgreSQL inline storage requires adcp[pg]'
        assert error.__context__ is None
    else:
        raise AssertionError('construction requires the optional driver')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("limit", [True, 0, -1, INLINE_STAGING_MAX_BYTES + 1, "secret"])
def test_invalid_limits_are_closed_before_driver_or_pool_access(limit) -> None:
    with pytest.raises(InlineStorageError) as caught:
        PgReportingStagingStore(pool=object(), max_payload_bytes=limit)
    assert caught.value.code == "INVALID_INPUT"
    assert "secret" not in repr(caught.value)


async def test_readiness_missing_drift_and_unrelated_ddl(database) -> None:
    pool, _, _ = database
    store = PgReportingStagingStore(pool=pool)
    with pytest.raises(InlineStorageError, match="schema is not ready"):
        await store.check_ready()
    await store.create_schema()
    await store.create_schema()
    async with pool.connection() as conn:
        await conn.execute("CREATE TABLE adopter_extra (value TEXT)")
    await store.check_ready()
    async with pool.connection() as conn:
        await conn.execute(
            "ALTER TABLE reporting_inline_objects DROP CONSTRAINT reporting_inline_objects_bytes"
        )
    for operation in (store.check_ready, store.create_schema):
        with pytest.raises(InlineStorageError) as caught:
            await operation()
        assert caught.value.code == "SCHEMA_UNREADY"
        assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE reporting_inline_objects DISABLE TRIGGER reporting_inline_objects_immutable",
        "ALTER TABLE reporting_inline_seals ALTER COLUMN manifest DROP NOT NULL",
        "ALTER TABLE reporting_inline_seals DROP CONSTRAINT reporting_inline_seals_pk",
        "ALTER TABLE reporting_inline_objects SET UNLOGGED",
    ],
)
async def test_readiness_rejects_schema_drift(stores, ddl) -> None:
    store, _, pool = stores
    async with pool.connection() as conn:
        await conn.execute(ddl)
    with pytest.raises(InlineStorageError) as caught:
        await store.check_ready()
    assert caught.value.code == "SCHEMA_UNREADY"


@pytest.mark.parametrize("autocommit", [False, True])
async def test_borrowed_pool_with_serializable_default(database, autocommit) -> None:
    from psycopg_pool import AsyncConnectionPool

    _, schema, dsn = database
    async with AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=1,
        open=False,
        kwargs={
            "autocommit": autocommit,
            "options": f"-c search_path={schema} -c default_transaction_isolation=serializable",
        },
    ) as pool:
        staging = PgReportingStagingStore(pool=pool)
        await staging.create_schema()
        args = dict(account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"bytes")
        assert await staging.stage(**args) == await staging.stage(**args)
        async with pool.connection() as conn:
            assert (await (await conn.execute("SHOW default_transaction_isolation")).fetchone())[
                0
            ] == "serializable"


async def test_account_content_identity_exact_bytes_and_bounds(stores) -> None:
    staging, _, pool = stores
    first = await staging.stage(
        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"\x00\xff\n"
    )
    same = await staging.stage(
        account_id="a", source_execution_key="execution-2", ordinal=99999, payload=b"\x00\xff\n"
    )
    other = await staging.stage(
        account_id="b", source_execution_key="execution-1", ordinal=0, payload=b"\x00\xff\n"
    )
    assert first == same and other[0] != first[0] and other[1] == first[1]
    assert first[1] == hashlib.sha256(b"\x00\xff\n").hexdigest()
    assert "execution" not in first[0]
    assert (
        await staging.read(
            object_ref=first[0],
            object_generation=first[1],
            account_id="a",
            source_scope={"ignored": "secret"},
            cancel=asyncio.Event(),
        )
        == b"\x00\xff\n"
    )
    with pytest.raises(InlineStorageError) as caught:
        await staging.read(
            object_ref=first[0],
            object_generation=first[1],
            account_id="b",
            source_scope={},
            cancel=asyncio.Event(),
        )
    assert caught.value.code == "NOT_FOUND"
    async with pool.connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_objects")).fetchone()
        )[0] == 2
    bounded = PgReportingStagingStore(pool=pool, max_payload_bytes=3)
    await bounded.stage(
        account_id="a", source_execution_key="execution-3", ordinal=0, payload=b"123"
    )
    with pytest.raises(InlineStorageError) as caught:
        await bounded.stage(
            account_id="a", source_execution_key="execution-3", ordinal=0, payload=b"1234"
        )
    assert caught.value.code == "INVALID_INPUT"
    empty = await staging.stage(
        account_id="a", source_execution_key="execution-4", ordinal=0, payload=b""
    )
    assert (
        await staging.read(
            object_ref=empty[0],
            object_generation=empty[1],
            account_id="a",
            source_scope={},
            cancel=asyncio.Event(),
        )
        == b""
    )


async def test_hard_payload_cap_and_content_winner_verification(stores, monkeypatch) -> None:
    staging, _, pool = stores
    args = dict(account_id="a", source_execution_key="execution-1", ordinal=0)
    with pytest.raises(InlineStorageError) as caught:
        await staging.stage(**args, payload=b"x" * (INLINE_STAGING_MAX_BYTES + 1))
    assert caught.value.code == "INVALID_INPUT"
    original = staging._read_on

    async def inconsistent_winner(*args, **kwargs):
        await original(*args, **kwargs)
        return b"different-winning-content"

    with monkeypatch.context() as patch:
        patch.setattr(staging, "_read_on", inconsistent_winner)
        with pytest.raises(InlineStorageError) as caught:
            await staging.stage(**args, payload=b"candidate")
    assert caught.value.code == "INTEGRITY_FAILED"
    async with pool.connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_objects")).fetchone()
        )[0] == 0


@pytest.mark.parametrize(
    "update",
    [
        {"account_id": ""},
        {"account_id": "a" * 256},
        {"account_id": "\ud800"},
        {"account_id": "a\x00"},
        {"source_execution_key": "short"},
        {"source_execution_key": "invalid/key"},
        {"ordinal": True},
        {"ordinal": -1},
        {"ordinal": 100000},
        {"payload": bytearray(b"secret")},
    ],
)
async def test_staging_invalid_input_is_redacted(stores, update) -> None:
    staging, _, _ = stores
    arguments = dict(
        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"payload"
    )
    arguments.update(update)
    with pytest.raises(InlineStorageError) as caught:
        await staging.stage(**arguments)
    assert caught.value.code == "INVALID_INPUT" and caught.value.__context__ is None
    assert "secret" not in repr(caught.value)


async def test_seal_winner_validation_and_detachment(stores) -> None:
    _, seals, _ = stores
    identity = redacted_snapshot_request().identity
    args = dict(account_id=identity.account_id, source_execution_key=identity.source_execution_key)
    first, second = await sample_seal(10), await sample_seal(11)
    winner = await seals.put(**args, sealed=first)
    assert winner == first and first == winner
    assert winner.reference == first.reference and winner.reference is not first.reference
    assert winner.manifest_bytes == first.manifest_bytes
    assert (await seals.put(**args, sealed=second)).manifest_bytes == first.manifest_bytes
    assert (await seals.get(**args)).manifest_bytes == first.manifest_bytes
    assert (
        await seals.get(
            account_id="other-account", source_execution_key=identity.source_execution_key
        )
        is None
    )
    assert "redacted" in repr(winner) and "manifest_version" not in repr(winner)
    with pytest.raises(Exception):
        winner.reference.byte_count = 1
    object.__setattr__(winner.reference, "byte_count", 1)
    assert (await seals.get(**args)).reference.byte_count == len(first.manifest_bytes)


@pytest.mark.parametrize(
    "fault", ["digest", "count", "bytes", "canonical", "oversize", "account", "key", "constructed"]
)
async def test_seal_refuses_invalid_candidate_even_when_winner_exists(stores, fault) -> None:
    _, seals, _ = stores
    identity = redacted_snapshot_request().identity
    args = dict(account_id=identity.account_id, source_execution_key=identity.source_execution_key)
    good = await sample_seal()
    await seals.put(**args, sealed=good)
    raw, ref = good.manifest_bytes, good.reference
    if fault == "digest":
        ref = ref.model_copy(update={"manifest_sha256": "0" * 64})
    elif fault == "count":
        ref = ref.model_copy(update={"byte_count": 1})
    elif fault == "bytes":
        raw = b"secret"
    elif fault == "canonical":
        raw = json.dumps(json.loads(raw), indent=2).encode()
        ref = source_batch_manifest_reference_v1("reference", raw)
    elif fault == "oversize":
        raw = b"x" * (1048576 + 1)
    elif fault in {"account", "key"}:
        args["account_id" if fault == "account" else "source_execution_key"] = "other-identity"
    else:
        ref = ref.model_copy(update={"encoding": "secret"})
    with pytest.raises(InlineStorageError) as caught:
        await seals.put(**args, sealed=SealedSlice(reference=ref, manifest_bytes=raw))
    assert caught.value.code == "INVALID_INPUT" and caught.value.__context__ is None
    assert "secret" not in str(caught.value)


async def test_corrupt_seal_refused_with_unchanged_valid_schema(stores) -> None:
    _, seals, pool = stores
    identity = redacted_snapshot_request().identity
    args = dict(account_id=identity.account_id, source_execution_key=identity.source_execution_key)
    good = await sample_seal()
    await seals.put(**args, sealed=good)
    # An administrative corruption preserves all SQL checks and restores the
    # immutable trigger, but destroys the source manifest's semantic validity.
    raw = json.dumps(
        {
            "identity": {
                "account_id": identity.account_id,
                "source_execution_key": identity.source_execution_key,
            }
        }
    ).encode()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "ALTER TABLE reporting_inline_seals DISABLE TRIGGER reporting_inline_seals_immutable"
        )
        await conn.execute(
            "UPDATE reporting_inline_seals SET manifest=%s, manifest_sha256=%s, byte_count=%s",
            (raw, hashlib.sha256(raw).hexdigest(), len(raw)),
        )
        await conn.execute(
            "ALTER TABLE reporting_inline_seals ENABLE TRIGGER reporting_inline_seals_immutable"
        )
    await seals.check_ready()
    for operation in (lambda: seals.get(**args), lambda: seals.put(**args, sealed=good)):
        with pytest.raises(InlineStorageError) as caught:
            await operation()
        assert caught.value.code == "INTEGRITY_FAILED"
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert "account-redacted" not in repr(caught.value)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM reporting_inline_objects",
        "UPDATE reporting_inline_objects SET payload=payload",
        "TRUNCATE reporting_inline_objects",
        "DELETE FROM reporting_inline_seals",
    ],
)
async def test_sql_write_guards_are_immutable(stores, sql) -> None:
    _, _, pool = stores
    with pytest.raises(Exception, match="storage is immutable"):
        async with pool.connection() as conn:
            await conn.execute(sql)
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT 1")).fetchone())[0] == 1


async def test_fault_before_commit_rolls_back_and_after_commit_resumes(stores, monkeypatch) -> None:
    staging, _, pool = stores
    args = dict(
        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"secret-payload"
    )
    original_read = staging._read_on

    async def fault(*args, **kwargs):
        await original_read(*args, **kwargs)
        await args[0].execute("SELECT 'secret-dsn-and-query'::integer")

    with monkeypatch.context() as patch:
        patch.setattr(staging, "_read_on", fault)
        with pytest.raises(InlineStorageError) as caught:
            await staging.stage(**args)
    assert caught.value.code == "RESOURCE_UNAVAILABLE" and caught.value.__context__ is None
    assert "secret" not in repr(caught.value) + repr(staging)
    async with pool.connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_objects")).fetchone()
        )[0] == 0
    original_transaction = staging._transaction

    @asynccontextmanager
    async def fail_after_commit():
        async with original_transaction() as conn:
            yield conn
        raise RuntimeError("secret-commit-acknowledgement-lost")

    with monkeypatch.context() as patch:
        patch.setattr(staging, "_transaction", fail_after_commit)
        with pytest.raises(InlineStorageError) as caught:
            await staging.stage(**args)
    assert caught.value.code == "RESOURCE_UNAVAILABLE" and caught.value.__context__ is None
    assert "secret" not in repr(caught.value)
    await staging.stage(**args)
    async with pool.connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_objects")).fetchone()
        )[0] == 1


async def test_cancellation_while_waiting_for_sql_settles_pool_one(stores, database) -> None:
    import psycopg

    staging, _, pool = stores
    _, schema, dsn = database
    async with await psycopg.AsyncConnection.connect(
        dsn, options=f"-c search_path={schema}"
    ) as blocker:
        await blocker.execute("LOCK TABLE reporting_inline_objects IN ACCESS EXCLUSIVE MODE")
        sdk_errors = []

        async def stage_and_capture():
            try:
                await staging.stage(
                    account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"bytes"
                )
            except asyncio.CancelledError as error:
                sdk_errors.append(error)
                raise

        task = asyncio.create_task(stage_and_capture())
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            for _ in range(200):
                row = await (
                    await observer.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND query LIKE 'LOCK TABLE reporting_inline_objects,%'"
                    )
                ).fetchone()
                if row[0]:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("store never reached the blocked SQL statement")
        task.cancel("secret-cancellation-message")
        done, pending = await asyncio.wait({task}, timeout=5)
        if pending:
            await blocker.rollback()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pytest.fail("canceled storage operation did not settle within five seconds")
        assert done == {task}
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        # Inspect the SDK exception before Python 3.10's Task boundary adds
        # another empty CancelledError around it. Neither boundary may retain
        # the caller's cancellation message or a driver exception.
        assert len(sdk_errors) == 1
        assert sdk_errors[0].__context__ is None and sdk_errors[0].__cause__ is None
        assert not sdk_errors[0].args
        error = caught.value
        seen = []
        while error is not None:
            assert type(error) is asyncio.CancelledError and not error.args
            assert error.__cause__ is None and error not in seen
            seen.append(error)
            error = error.__context__
    # The borrowed pool is still open, its only connection reusable, and no
    # detached operation commits after the canceled call returns.
    async with pool.connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_objects")).fetchone()
        )[0] == 0
    await staging.stage(
        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"bytes"
    )


async def test_read_cancel_event(stores) -> None:
    staging, _, _ = stores
    ref, generation = await staging.stage(
        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"bytes"
    )
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await staging.read(
            object_ref=ref,
            object_generation=generation,
            account_id="a",
            source_scope={},
            cancel=cancel,
        )


async def worker(database, tmp_path, mode, name, value=10):
    _, schema, dsn = database
    output = tmp_path / f"{name}.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("_inline_storage_worker.py")),
        mode,
        schema,
        str(output),
        str(tmp_path / "dispatches"),
        "--value",
        str(value),
        env={**os.environ, "ADCP_INLINE_TEST_DSN": dsn},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    return json.loads(output.read_text())


async def test_independent_process_schema_boot_race(database, tmp_path) -> None:
    await asyncio.gather(*(worker(database, tmp_path, "create", f"boot-{i}") for i in range(2)))
    await PgReportingStagingStore(pool=database[0]).check_ready()


async def test_independent_process_staging_deduplicates(stores, database, tmp_path) -> None:
    outcomes = await asyncio.gather(
        *(worker(database, tmp_path, "stage", f"stage-{i}", i) for i in range(2))
    )
    assert outcomes[0] == outcomes[1]
    async with database[0].connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_objects")).fetchone()
        )[0] == 1


async def test_fresh_process_replay_without_fetch_and_concurrent_winners(
    stores, database, tmp_path
) -> None:
    outcomes = await asyncio.gather(
        *(worker(database, tmp_path, "publish", f"publish-{i}", i + 10) for i in range(2))
    )
    assert outcomes[0] == outcomes[1]
    before = (tmp_path / "dispatches").read_bytes()
    replay = await worker(database, tmp_path, "replay", "restart")
    assert replay == outcomes[0]
    assert (tmp_path / "dispatches").read_bytes() == before
    assert before.count(b"dispatch\n") == 2


async def test_committed_seal_replays_after_cancel_without_dispatch(stores) -> None:
    staging, seals, _ = stores
    calls = []

    def fetch(_):
        calls.append(1)
        return [ROW]

    inline = source(staging, seals, fetch)
    request = redacted_snapshot_request()
    original = await inline.execute(request, cancel=asyncio.Event())
    cancel = asyncio.Event()
    cancel.set()
    replay = await inline.execute(request, cancel=cancel)
    assert replay.manifest_bytes == original.manifest_bytes
    assert len(calls) == 1


async def test_changed_request_remains_a_conformance_responsibility(stores) -> None:
    staging, seals, _ = stores
    request = redacted_snapshot_request()
    inline = source(staging, seals)
    await inline.execute(request, cancel=asyncio.Event())
    changed = request.model_copy(update={"currency": "EUR"})
    replay = await inline.execute(changed, cancel=asyncio.Event())
    with pytest.raises(Exception, match="currency"):
        await validate_reporting_source_execution(
            capabilities=inline.capabilities, request=changed, result=replay, object_reader=staging
        )


async def test_stage_before_seal_failure_can_refetch(stores, monkeypatch) -> None:
    staging, seals, pool = stores
    calls = []

    def fetch(_):
        calls.append(1)
        return [ROW]

    inline = source(staging, seals, fetch)
    request = redacted_snapshot_request()

    async def fail_seal(**kwargs):
        raise InlineStorageError("RESOURCE_UNAVAILABLE")

    with monkeypatch.context() as patch:
        patch.setattr(seals, "put", fail_seal)
        with pytest.raises(InlineStorageError):
            await inline.execute(request, cancel=asyncio.Event())
    async with pool.connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_objects")).fetchone()
        )[0] == 1
        assert (
            await (await conn.execute("SELECT count(*) FROM reporting_inline_seals")).fetchone()
        )[0] == 0
    await inline.execute(request, cancel=asyncio.Event())
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["stage", "read", "get", "put"])
async def test_unmigrated_operations_are_schema_unready(database, operation) -> None:
    pool, _, _ = database
    staging, seals = PgReportingStagingStore(pool=pool), PgReportingSealStore(pool=pool)
    identity = redacted_snapshot_request().identity
    args = dict(account_id=identity.account_id, source_execution_key=identity.source_execution_key)
    digest = hashlib.sha256(b"payload").hexdigest()
    reference = f"pg-inline-v1.{hashlib.sha256(identity.account_id.encode()).hexdigest()}.{digest}"
    calls = {
        "stage": lambda: staging.stage(**args, ordinal=0, payload=b"payload"),
        "read": lambda: staging.read(
            account_id=identity.account_id,
            object_ref=reference,
            object_generation=digest,
            source_scope={},
            cancel=asyncio.Event(),
        ),
        "get": lambda: seals.get(**args),
        "put": lambda: seals.put(**args, sealed=sealed),
    }
    sealed = await sample_seal()
    with pytest.raises(InlineStorageError) as caught:
        await calls[operation]()
    assert caught.value.code == "SCHEMA_UNREADY"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    await staging.create_schema()
    await staging.check_ready()


async def test_catalog_additive_metadata_does_not_change_readiness(stores, monkeypatch) -> None:
    import adcp.reporting.inline_storage as module

    staging, _, _ = stores
    original = module.schema_objects

    async def with_metadata(connection):
        return {
            key: {**value, "adopter_metadata": "secret"}
            for key, value in (await original(connection)).items()
        }

    monkeypatch.setattr(module, "schema_objects", with_metadata)
    await staging.check_ready()
    await staging.stage(account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"x")


@pytest.mark.parametrize("field", ["fingerprint", "enabled"])
async def test_catalog_required_fields_still_fail_closed(stores, monkeypatch, field) -> None:
    import adcp.reporting.inline_storage as module

    staging, _, _ = stores
    original = module.schema_objects

    async def with_missing_field(connection):
        objects = await original(connection)
        objects[next(iter(module.REQUIRED_OBJECTS))].pop(field)
        return objects

    monkeypatch.setattr(module, "schema_objects", with_missing_field)
    with pytest.raises(InlineStorageError) as caught:
        await staging.check_ready()
    assert caught.value.code == "SCHEMA_UNREADY" and caught.value.__context__ is None


def assert_framework_cancellation(error) -> None:
    assert error.args in (
        ("Cancelled by cancel scope [redacted]",),
        ("Cancelled via cancel scope [redacted]",),
    )
    assert error.__context__ is None and error.__cause__ is None


@pytest.mark.parametrize("kind", ["fail_after", "move_on_after"])
@pytest.mark.parametrize("blocked_at", ["pool", "sql"])
async def test_anyio_deadline_contains_redacted_cancellation(
    stores, database, kind, blocked_at, caplog
) -> None:
    import psycopg

    staging, _, pool = stores
    _, schema, dsn = database
    calls = 0

    async def operation():
        nonlocal calls
        try:
            await staging.stage(
                account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"x"
            )
        except asyncio.CancelledError as error:
            calls += 1
            assert_framework_cancellation(error)
            raise

    async def timed():
        if kind == "fail_after":
            with pytest.raises(TimeoutError):
                with anyio.fail_after(0.025) as scope:
                    await operation()
        else:
            with anyio.move_on_after(0.025) as scope:
                await operation()
        assert scope.cancel_called and scope.cancelled_caught

    async with pool.connection() as connection:
        original_backend = connection.info.backend_pid
    # Repeat using the same size-one pool to expose unsettled borrowers.
    for _ in range(2):
        if blocked_at == "pool":
            async with pool.connection():
                await timed()
        else:
            async with await psycopg.AsyncConnection.connect(
                dsn, options=f"-c search_path={schema}"
            ) as blocker:
                await blocker.execute(
                    "LOCK TABLE reporting_inline_objects IN ACCESS EXCLUSIVE MODE"
                )
                await timed()
                await blocker.rollback()
        async with pool.connection() as connection:
            assert (await (await connection.execute("SELECT 1")).fetchone())[0] == 1
            assert connection.info.backend_pid == original_backend
    assert calls == 2
    assert not [record for record in caplog.records if record.name.startswith("psycopg")]


async def test_anyio_nested_deadline_reaches_owning_scope(stores) -> None:
    staging, _, pool = stores
    async with pool.connection():
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.025) as outer:
                with anyio.move_on_after(1) as inner:
                    await staging.stage(
                        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"x"
                    )
        assert outer.cancelled_caught and not inner.cancelled_caught
    async with pool.connection() as connection:
        assert (await (await connection.execute("SELECT 1")).fetchone())[0] == 1


async def test_anyio_cancel_reason_and_task_name_are_not_exposed(stores) -> None:
    staging, _, pool = stores
    task = asyncio.current_task()
    previous = task.get_name()
    task.set_name("secret-task-name")
    captured = False
    try:
        async with pool.connection():
            with anyio.CancelScope() as scope:
                if "reason" in inspect.signature(scope.cancel).parameters:
                    scope.cancel("secret-cancellation-reason")
                else:
                    scope.cancel()
                try:
                    await staging.stage(
                        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"x"
                    )
                except asyncio.CancelledError as error:
                    captured = True
                    assert_framework_cancellation(error)
                    assert "secret" not in repr(error)
                    raise
            assert captured and scope.cancelled_caught
    finally:
        task.set_name(previous)
    async with pool.connection() as connection:
        assert (await (await connection.execute("SELECT 1")).fetchone())[0] == 1


@pytest.mark.parametrize("prefix", ["Cancelled by cancel scope ", "Cancelled via cancel scope "])
async def test_forged_framework_marker_without_cancelled_scope_stays_argless(
    stores, monkeypatch, prefix
) -> None:
    staging, _, _ = stores

    @asynccontextmanager
    async def forged_transaction():
        raise asyncio.CancelledError(prefix + "secret-provider-text")
        yield

    monkeypatch.setattr(staging, "_transaction", forged_transaction)
    with anyio.CancelScope():
        with pytest.raises(asyncio.CancelledError) as caught:
            await staging.stage(
                account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"x"
            )
        assert not caught.value.args
        assert caught.value.__context__ is None and caught.value.__cause__ is None


async def test_large_payload_hash_yields_and_matches_exact_bytes(stores, monkeypatch) -> None:
    import adcp.reporting.inline_storage as module

    staging, _, _ = stores
    payload = b"x" * INLINE_STAGING_MAX_BYTES
    original_digest = module._payload_digest
    checked = 0

    async def observe_digest(payload):
        nonlocal checked
        advanced = asyncio.Event()
        asyncio.get_running_loop().call_soon(advanced.set)
        result = await original_digest(payload)
        assert advanced.is_set(), "a scheduled callback must run during payload hashing"
        checked += 1
        return result

    monkeypatch.setattr(module, "_payload_digest", observe_digest)
    reference, digest = await staging.stage(
        account_id="a", source_execution_key="execution-1", ordinal=0, payload=payload
    )
    assert digest == hashlib.sha256(payload).hexdigest()
    assert (
        await staging.read(
            account_id="a",
            object_ref=reference,
            object_generation=digest,
            source_scope={},
            cancel=asyncio.Event(),
        )
        == payload
    )
    assert checked == 3


@pytest.mark.parametrize("during", ["before_transaction", "winner_verification"])
async def test_cancellation_during_payload_hashing_settles_without_partial_write(
    stores, monkeypatch, during
) -> None:
    staging, _, pool = stores
    payload = b"x" * INLINE_STAGING_MAX_BYTES
    import adcp.reporting.inline_storage as module

    original_digest = module._payload_digest
    original_transaction = staging._transaction
    entered = False
    canceled_in_hash = False
    digest_calls = 0

    @asynccontextmanager
    async def track_transaction():
        nonlocal entered
        entered = True
        async with original_transaction() as connection:
            yield connection

    async def hash_with_cancellation(payload):
        nonlocal canceled_in_hash, digest_calls
        digest_calls += 1
        if digest_calls == (1 if during == "before_transaction" else 2):
            # The winning row is already read inside the write transaction; cancel
            # at the real cooperative hash checkpoint before commit.
            asyncio.get_running_loop().call_soon(
                asyncio.current_task().cancel, "secret-hash-cancel"
            )
            canceled_in_hash = True
        return await original_digest(payload)

    monkeypatch.setattr(staging, "_transaction", track_transaction)
    monkeypatch.setattr(module, "_payload_digest", hash_with_cancellation)
    with pytest.raises(asyncio.CancelledError) as caught:
        await staging.stage(
            account_id="a", source_execution_key="execution-1", ordinal=0, payload=payload
        )
    assert not caught.value.args and caught.value.__context__ is None
    assert entered == (during == "winner_verification")
    assert canceled_in_hash
    async with pool.connection() as connection:
        assert (
            await (
                await connection.execute("SELECT count(*) FROM reporting_inline_objects")
            ).fetchone()
        )[0] == 0


@pytest.mark.parametrize("phase", ["in_transaction", "after_commit"])
async def test_repeated_task_cancel_waits_for_owned_settlement(
    stores, monkeypatch, caplog, phase
) -> None:
    staging, _, pool = stores
    entered, cancel_seen, release, settled = (asyncio.Event() for _ in range(4))
    cancellations = 0
    original = staging._transaction
    loop = asyncio.get_running_loop()
    async with pool.connection() as connection:
        backend_pid = connection.info.backend_pid

    async def hold():
        nonlocal cancellations
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellations += 1
            cancel_seen.set()
            await release.wait()
            raise

    @asynccontextmanager
    async def delayed_settlement():
        try:
            async with original() as connection:
                assert asyncio.get_running_loop() is loop
                assert connection.info.backend_pid == backend_pid
                yield connection
                if phase == "in_transaction":
                    await hold()
            if phase == "after_commit":
                await hold()
        finally:
            settled.set()

    monkeypatch.setattr(staging, "_transaction", delayed_settlement)

    async def call():
        try:
            await staging.stage(
                account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"payload"
            )
        except asyncio.CancelledError as error:
            assert not error.args and error.__context__ is None and error.__cause__ is None
            assert settled.is_set()
            raise

    acquired = asyncio.Event()

    async def borrower():
        async with pool.connection() as connection:
            acquired.set()
            assert connection.info.backend_pid == backend_pid

    operation = asyncio.create_task(call())
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        operation.cancel("secret-first-cancel")
        await asyncio.wait_for(cancel_seen.wait(), 5)
        waiting = asyncio.create_task(borrower())
        for reason in ("secret-second-cancel", "secret-third-cancel"):
            operation.cancel(reason)
            await asyncio.sleep(0)
            assert not operation.done() and not settled.is_set()
        assert acquired.is_set() == (phase == "after_commit")
        release.set()
        _, pending = await asyncio.wait({operation, waiting}, timeout=5)
        assert not pending
        assert operation.cancelled() and waiting.exception() is None
        assert cancellations == 1 and settled.is_set()
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, *([waiting] if waiting else []), return_exceptions=True)
    async with pool.connection() as connection:
        assert connection.info.backend_pid == backend_pid
        count = (
            await (
                await connection.execute("SELECT count(*) FROM reporting_inline_objects")
            ).fetchone()
        )[0]
        assert count == (1 if phase == "after_commit" else 0)
    assert not [record for record in caplog.records if record.name.startswith("psycopg")]


@pytest.mark.parametrize("factory", ["native", "pure_python"])
@pytest.mark.parametrize("outcome", ["driver_error", "completed_cancel_race"])
async def test_task_boundary_never_transports_raw_exception_context(
    stores, monkeypatch, factory, outcome
) -> None:
    staging, _, pool = stores
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    marker = "private-" + uuid.uuid4().hex
    original_read = staging._read_on
    original_transaction = staging._transaction
    caller = None
    committed = False

    async def driver_error(*args, **kwargs):
        await original_read(*args, **kwargs)
        query = "SELECT '" + marker + "'::integer"
        await args[0].execute(query)

    @asynccontextmanager
    async def completed_cancel_race():
        nonlocal committed
        async with original_transaction() as connection:
            yield connection
        committed = True
        # Schedule cancellation before the task's done callback wakes its caller.
        # The operation has returned its borrowed connection and will be done by
        # the time the caller receives cancellation: joining it need not suspend.
        loop.call_soon(caller.cancel, marker)

    async def call_public_boundary():
        nonlocal caller
        caller = asyncio.current_task()
        try:
            await staging.stage(
                account_id="a",
                source_execution_key="execution-1",
                ordinal=0,
                payload=marker.encode(),
            )
        except BaseException as error:
            assert error.__context__ is None and error.__cause__ is None
            chain = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            assert marker not in chain
            if outcome == "driver_error":
                assert isinstance(error, InlineStorageError)
                assert error.code == "RESOURCE_UNAVAILABLE"
            else:
                assert isinstance(error, asyncio.CancelledError) and not error.args
                assert committed
            return
        raise AssertionError("the public call must fail or propagate cancellation")

    def make_task(loop, coroutine, **kwargs):
        if factory == "pure_python":
            return asyncio.tasks._PyTask(coroutine, loop=loop)
        return asyncio.Task(coroutine, loop=loop)

    if outcome == "driver_error":
        monkeypatch.setattr(staging, "_read_on", driver_error)
    else:
        monkeypatch.setattr(staging, "_transaction", completed_cancel_race)
    task = None
    try:
        loop.set_task_factory(make_task)
        task = asyncio.create_task(call_public_boundary())
        _, pending = await asyncio.wait({task}, timeout=5)
        assert not pending
        task.result()
    finally:
        loop.set_task_factory(previous_factory)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with pool.connection() as connection:
        count = (
            await (
                await connection.execute("SELECT count(*) FROM reporting_inline_objects")
            ).fetchone()
        )[0]
        assert count == (1 if outcome == "completed_cancel_race" else 0)


def test_prepared_object_is_immutable_and_redacted() -> None:
    payload = b"private-prepared-payload"
    prepared = PreparedBackendObjectV1(
        "private-account", "execution-1", 0, payload, hashlib.sha256(payload).hexdigest()
    )
    assert prepared.payload is payload
    assert repr(prepared) == "PreparedBackendObjectV1(<redacted>)"
    with pytest.raises(FrozenInstanceError):
        prepared.ordinal = 1


@pytest.mark.parametrize(
    "fault", ["account", "key", "bool", "negative", "ordinal_cap", "mutable", "digest", "cap"]
)
def test_prepared_object_rejects_invalid_inputs_without_raw_context(fault) -> None:
    args = dict(
        account_id="a",
        source_execution_key="execution-1",
        ordinal=0,
        payload=b"bytes",
        payload_sha256=hashlib.sha256(b"bytes").hexdigest(),
    )
    if fault == "account":
        args["account_id"] = "private-\ud800"
    elif fault == "key":
        args["source_execution_key"] = "private/key"
    elif fault in {"bool", "negative", "ordinal_cap"}:
        args["ordinal"] = {"bool": True, "negative": -1, "ordinal_cap": 100_000}[fault]
    elif fault == "mutable":
        args["payload"] = bytearray(b"private-bytes")
    elif fault == "digest":
        args["payload_sha256"] = "private-digest"
    else:
        args["payload"] = b"x" * (INLINE_STAGING_MAX_BYTES + 1)
    with pytest.raises(InlineStorageError) as caught:
        PreparedBackendObjectV1(**args)
    assert caught.value.code == "INVALID_INPUT"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert "private" not in repr(caught.value)


async def test_prepared_seal_detaches_reference_and_preserves_neutral_winner(stores) -> None:
    _, seals, _ = stores
    identity = redacted_snapshot_request().identity
    original = await sample_seal()
    prepared = seals._prepare_seal(
        account_id=identity.account_id,
        source_execution_key=identity.source_execution_key,
        sealed=original,
    )
    assert type(prepared) is PreparedBackendSealV1
    assert repr(prepared) == "PreparedBackendSealV1(<redacted>)"
    with pytest.raises(FrozenInstanceError):
        prepared.byte_count = 1
    object.__setattr__(original.reference, "byte_count", 1)
    assert prepared.byte_count == len(original.manifest_bytes)
    async with seals._transaction() as connection:
        winner = await seals._put_on(connection, prepared)
        assert isinstance(winner, SealedSlice)
        assert winner.reference.byte_count == prepared.byte_count
        assert winner.manifest_bytes == prepared.manifest_bytes


@pytest.mark.parametrize("finish", ["commit", "rollback"])
async def test_participants_share_caller_connection_task_and_transaction(
    stores, monkeypatch, finish
) -> None:
    staging, seals, pool = stores
    identity = redacted_snapshot_request().identity
    args = dict(account_id=identity.account_id, source_execution_key=identity.source_execution_key)
    await staging.stage(**args, ordinal=0, payload=b"preexisting")
    old_object = await staging._prepare_object(**args, ordinal=0, payload=b"preexisting")
    new_object = await staging._prepare_object(**args, ordinal=1, payload=b"new-object")
    first = seals._prepare_seal(**args, sealed=await sample_seal(10))
    second = seals._prepare_seal(**args, sealed=await sample_seal(11))
    assert first.manifest_sha256 != second.manifest_sha256
    owner = asyncio.current_task()
    original_transaction = staging._transaction
    original_read, original_get = staging._read_on, seals._get_on
    seen = []

    def forbidden_checkout(*args, **kwargs):
        raise AssertionError("participants must use the supplied transaction")

    async def read_on(connection, *args):
        assert connection is owned_connection and asyncio.current_task() is owner
        seen.append("object")
        return await original_read(connection, *args)

    async def get_on(connection, *args):
        assert connection is owned_connection and asyncio.current_task() is owner
        seen.append("seal")
        return await original_get(connection, *args)

    class CallerRollbackError(Exception):
        pass

    try:
        # This fixture owns its account. A composed caller must additionally
        # hold its account-order lock; these neutral participants do not do so.
        async with original_transaction() as owned_connection:
            with monkeypatch.context() as patch:
                patch.setattr(pool, "connection", forbidden_checkout)
                patch.setattr(staging, "_transaction", forbidden_checkout)
                patch.setattr(seals, "_transaction", forbidden_checkout)
                patch.setattr(staging, "_read_on", read_on)
                patch.setattr(seals, "_get_on", get_on)
                await staging._stage_on(owned_connection, old_object)
                ref, generation = await staging._stage_on(owned_connection, new_object)
                winner = await seals._put_on(owned_connection, first)
                assert await seals._put_on(owned_connection, second) == winner
                assert winner.manifest_bytes == first.manifest_bytes
                assert winner.reference.manifest_sha256 == first.manifest_sha256
                isolation = await (
                    await owned_connection.execute("SHOW transaction_isolation")
                ).fetchone()
                assert isolation[0] == "read committed"
                if finish == "rollback":
                    raise CallerRollbackError
    except CallerRollbackError:
        assert finish == "rollback"
    assert seen == ["object", "object", "seal", "seal"]
    async with pool.connection() as connection:
        count = (
            await (
                await connection.execute("SELECT count(*) FROM reporting_inline_objects")
            ).fetchone()
        )[0]
        assert count == (2 if finish == "commit" else 1)
    stored = await seals.get(**args)
    if finish == "commit":
        assert stored == winner
        assert (
            await staging.read(
                account_id=identity.account_id,
                object_ref=ref,
                object_generation=generation,
                source_scope={},
                cancel=asyncio.Event(),
            )
            == b"new-object"
        )
    else:
        assert stored is None


@pytest.mark.parametrize("kind", ["stage", "put"])
async def test_public_writes_delegate_inside_existing_owned_transaction(
    stores, monkeypatch, kind
) -> None:
    staging, seals, _ = stores
    identity = redacted_snapshot_request().identity
    args = dict(account_id=identity.account_id, source_execution_key=identity.source_execution_key)
    store = staging if kind == "stage" else seals
    participant_name = "_stage_on" if kind == "stage" else "_put_on"
    original_transaction = store._transaction
    original_participant = getattr(store, participant_name)
    caller = asyncio.current_task()
    delegated = []
    settled = False
    connection = owner = None

    @asynccontextmanager
    async def transaction():
        nonlocal connection, owner, settled
        owner = asyncio.current_task()
        assert owner is not caller
        async with original_transaction() as connection:
            yield connection
        settled = True

    async def participant(supplied_connection, prepared):
        assert supplied_connection is connection and asyncio.current_task() is owner
        assert not settled
        delegated.append(prepared)
        return await original_participant(supplied_connection, prepared)

    monkeypatch.setattr(store, "_transaction", transaction)
    monkeypatch.setattr(store, participant_name, participant)
    if kind == "stage":
        result = await staging.stage(**args, ordinal=0, payload=b"delegated")
        assert result[1] == hashlib.sha256(b"delegated").hexdigest()
        assert type(delegated[0]) is PreparedBackendObjectV1
    else:
        candidate = await sample_seal()
        result = await seals.put(**args, sealed=candidate)
        assert result == candidate
        assert type(delegated[0]) is PreparedBackendSealV1
    assert settled and len(delegated) == 1


@pytest.mark.parametrize("fault", ["digest", "canonical", "account", "cap"])
async def test_seal_participant_revalidates_prepared_candidate_before_sql(stores, fault) -> None:
    _, seals, _ = stores
    identity = redacted_snapshot_request().identity
    args = dict(account_id=identity.account_id, source_execution_key=identity.source_execution_key)
    candidate = await sample_seal()
    await seals.put(**args, sealed=candidate)
    prepared = seals._prepare_seal(**args, sealed=candidate)
    if fault == "digest":
        prepared = replace(prepared, manifest_sha256="0" * 64)
    elif fault == "canonical":
        raw = json.dumps(json.loads(prepared.manifest_bytes), indent=2).encode()
        prepared = replace(
            prepared,
            manifest_bytes=raw,
            byte_count=len(raw),
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
        )
    elif fault == "account":
        prepared = replace(prepared, account_id="other-account")
    else:
        # Frozen records deter normal mutation; the participant still validates
        # a deliberately forged instance rather than treating it as authority.
        object.__setattr__(prepared, "byte_count", 1048577)

    class NoSql:
        async def execute(self, *args, **kwargs):
            raise AssertionError("invalid candidate must be refused before SQL")

    with pytest.raises(InlineStorageError) as caught:
        await seals._put_on(NoSql(), prepared)
    assert caught.value.code == "INVALID_INPUT"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert (await seals.get(**args)).manifest_bytes == candidate.manifest_bytes


async def test_stage_participant_enforces_local_cap_and_stored_byte_winner(
    stores, monkeypatch
) -> None:
    staging, _, pool = stores
    prepared = await staging._prepare_object(
        account_id="a", source_execution_key="execution-1", ordinal=0, payload=b"bytes"
    )
    smaller = PgReportingStagingStore(pool=pool, max_payload_bytes=4)

    class NoSql:
        async def execute(self, *args, **kwargs):
            raise AssertionError("local payload cap must be checked before SQL")

    with pytest.raises(InlineStorageError) as caught:
        await smaller._stage_on(NoSql(), prepared)
    assert caught.value.code == "INVALID_INPUT"
    original_read = staging._read_on

    async def wrong_winner(*args):
        await original_read(*args)
        return b"different-bytes"

    monkeypatch.setattr(staging, "_read_on", wrong_winner)
    with pytest.raises(InlineStorageError) as caught:
        async with staging._transaction() as connection:
            await staging._stage_on(connection, prepared)
    assert caught.value.code == "INTEGRITY_FAILED"
    async with pool.connection() as connection:
        assert (
            await (
                await connection.execute("SELECT count(*) FROM reporting_inline_objects")
            ).fetchone()
        )[0] == 0
