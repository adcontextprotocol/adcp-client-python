"""Shared fixture for the orphan collector and its ownership fence.

A real PostgreSQL schema and the filesystem provider, with hooks on the provider: fail a delete,
fail or replace a listing page, make every object look older, or count fence reads. A "clone" is a
point-in-time copy of every table in the live schema, which is how a database clone or restore looks
to the collector: the same installation identity, namespace key, bindings and fence row.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore, ReportingRowStorageOptionsV1
from adcp_reporting_storage.ledger.orphan_collector import (
    ReportingOrphanBindingResultV1,
    ReportingOrphanCollectionResultV1,
)
from adcp_reporting_storage.ledger.row_storage import (
    ReportingObjectRowBindingV1,
    ReportingPostgresRowBindingV1,
)
from adcp_reporting_storage.ledger.row_storage_object import (
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectListEntryV1,
    ReportingRowObjectListInputV1,
    ReportingRowObjectListPageV1,
    ReportingRowObjectPutInputV1,
    create_filesystem_reporting_row_object_provider_v1,
    format_row_instant,
    parse_row_instant,
)

from ._generation_support import isolated_reporting_pool
from .test_reporting_row_storage_pg import fetch_all, many_rows, seeded, typed_revision

DAY = timedelta(days=1)
CONTEXT = ReportingRowObjectContextV1(timeout_seconds=10.0)
LOCATION = {"root": "rows"}
# In foreign-key order.
OWNED_TABLES = (
    "adcp_persistence_installation",
    "adcp_reporting_row_bindings",
    "adcp_reporting_row_sets",
    "adcp_reporting_row_chunks",
    "adcp_reporting_chunk_bodies",
    "adcp_reporting_row_write_intents",
    "adcp_reporting_orphan_collector_fences",
)


def later() -> datetime:
    """A clock past the age threshold, so every object written by the test is old enough."""
    return datetime.now(timezone.utc) + 8 * DAY


class HookedProvider:
    """The filesystem provider with test hooks on ``delete`` and ``list``."""

    name = "filesystem"
    ranged_reads = True

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.failing_keys: set[str] = set()
        self.list_failures: list[BaseException] = []
        self.delete_calls: list[str] = []
        self.list_calls: list[str] = []
        self.list_override: (
            Callable[
                [ReportingRowObjectListInputV1, Callable[..., Awaitable[Any]]],
                Awaitable[ReportingRowObjectListPageV1],
            ]
            | None
        ) = None
        self.age_shift: timedelta = timedelta(0)
        self.put_failures: list[BaseException] = []
        self.on_put: Callable[[ReportingRowObjectPutInputV1], Awaitable[None]] | None = None
        self.on_delete: Callable[[str], Awaitable[None]] | None = None
        self.fence_list_failures: list[BaseException] = []
        self.fence_list_override: ReportingRowObjectListPageV1 | None = None

    def validate_location(self, location: Any) -> None:
        self.inner.validate_location(location)

    async def probe(self, *args: Any, **kwargs: Any) -> None:
        await self.inner.probe(*args, **kwargs)

    async def put_if_absent(self, put: ReportingRowObjectPutInputV1, context: Any) -> Any:
        if self.put_failures:
            raise self.put_failures.pop(0)
        if self.on_put is not None:
            await self.on_put(put)
        return await self.inner.put_if_absent(put, context)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return await self.inner.get(*args, **kwargs)

    async def stat(self, *args: Any, **kwargs: Any) -> Any:
        return await self.inner.stat(*args, **kwargs)

    async def delete(self, delete: ReportingRowObjectDeleteInputV1, context: Any) -> Any:
        if self.on_delete is not None:
            await self.on_delete(delete.object_ref)
        if delete.object_ref in self.failing_keys:
            raise RuntimeError("provider unavailable")
        self.delete_calls.append(f"{delete.object_ref}@{delete.native_version_ref}")
        return await self.inner.delete(delete, context)

    async def list(
        self, request: ReportingRowObjectListInputV1, context: Any
    ) -> ReportingRowObjectListPageV1:
        self.list_calls.append(request.prefix)
        # The hooks aim at the collector's object listing, not at the fence chain it also lists.
        collecting = "_collector-fence" not in request.prefix
        if not collecting and self.fence_list_failures:
            raise self.fence_list_failures.pop(0)
        if not collecting and self.fence_list_override is not None:
            return self.fence_list_override
        if collecting and self.list_failures:
            raise self.list_failures.pop(0)

        async def real(page: ReportingRowObjectListInputV1) -> ReportingRowObjectListPageV1:
            listed = await self.inner.list(page, context)
            if not self.age_shift:
                return listed  # type: ignore[no-any-return]
            shifted = []
            for entry in listed.objects:
                moment = parse_row_instant(entry.created_at)
                assert moment is not None
                shifted.append(
                    replace(entry, created_at=format_row_instant(moment - self.age_shift))
                )
            return replace(listed, objects=tuple(shifted))

        if collecting and self.list_override is not None:
            return await self.list_override(request, real)
        return await real(request)


class NoListProvider:
    """The same provider without ``list``: a binding that cannot be collected."""

    name = "filesystem"
    ranged_reads = True

    def __init__(self, inner: Any) -> None:
        for method in ("validate_location", "probe", "put_if_absent", "get", "stat", "delete"):
            setattr(self, method, getattr(inner, method))


@dataclass
class Fixture:
    pool: Any
    store: PgReportingLedgerStore
    root: Path
    provider: HookedProvider
    namespace_key: str
    options: ReportingRowStorageOptionsV1
    obligation: Any = None
    revisions: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def key(self, name: str, prefix: str = "orph") -> str:
        return f"{prefix}/{self.namespace_key}/{name}"

    def on_disk(self, object_ref: str) -> bool:
        return (self.root / "rows" / object_ref).exists()

    async def put_object(self, object_ref: str, body: bytes = b'{"a":1}\n') -> str:
        created = await self.provider.inner.put_if_absent(
            ReportingRowObjectPutInputV1(
                LOCATION,
                object_ref,
                body,
                "application/x-ndjson",
                {"adcp-installation": "x", "adcp-intent": "y"},
            ),
            CONTEXT,
        )
        return str(created.native_version_ref)

    async def add_intent(
        self,
        row_set_id: str,
        object_ref: str | None,
        native_version_ref: str | None,
        *,
        binding: str = "objects",
        state: str = "open",
        legacy_names: bool = False,
    ) -> None:
        import json

        created = []
        if object_ref is not None:
            created = [
                (
                    {"objectKey": object_ref, "nativeVersion": native_version_ref}
                    if legacy_names
                    else {"object_ref": object_ref, "native_version_ref": native_version_ref}
                )
            ]
        async with self.pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_row_write_intents (row_binding_id, account_id,"
                " row_set_id, content_sha256, state, owner_token, created_objects, expires_at)"
                " VALUES (%s, 'acct_orphans', %s, %s, %s, 'owner', %s::jsonb,"
                " clock_timestamp() + INTERVAL '1 hour')",
                (binding, row_set_id, "e" * 64, state, json.dumps(created)),
            )

    async def commit_revision(self, suffix: str) -> tuple[str, str]:
        """Commit an object-bound revision; returns the chunk's ``(object_ref, native_version)``."""
        if self.obligation is None:
            self.obligation = await seeded(self.store)
            async with self.pool.connection() as connection:
                await connection.execute(
                    "UPDATE reporting_configurations SET lease_worker_id = 'worker-1',"
                    " lease_expires_at = clock_timestamp() + INTERVAL '10 minutes'"
                )
        rows = many_rows(4)
        revision = typed_revision(self.obligation, rows, suffix=suffix)
        assert await self.store.stage_revision_rows(revision, rows) is True
        await self.store.commit_revision(revision, rows)
        found = await fetch_all(
            self.pool,
            "SELECT object_ref, native_version_ref FROM adcp_reporting_row_chunks"
            " WHERE row_set_id = %s",
            (revision.reporting_revision_id,),
        )
        return str(found[0][0]), str(found[0][1])

    def fence_dir(self) -> Path:
        return self.root / "rows" / "orph" / self.namespace_key / "_collector-fence"

    def fence_files(self) -> list[str]:
        directory = self.fence_dir()
        return sorted(item.name for item in directory.iterdir()) if directory.exists() else []

    def write_fence(
        self,
        epoch: int,
        *,
        token: str | None = None,
        previous: str | None = None,
        namespace: str | None = None,
        raw: bytes | None = None,
    ) -> str:
        """Put a fence object in the bucket by hand, as another database would have."""
        import uuid

        from adcp_reporting_storage.ledger.orphan_fence import (
            OrphanFenceRecord,
            encode_orphan_fence_record,
            orphan_fence_key,
        )

        key = orphan_fence_key(f"orph/{self.namespace_key}/", epoch)
        path = self.root / "rows" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        chosen = token or str(uuid.uuid4())
        path.write_bytes(
            raw
            if raw is not None
            else encode_orphan_fence_record(
                namespace or self.namespace_key,
                OrphanFenceRecord(epoch, chosen, previous, "2026-01-01T00:00:00.000Z"),
            )
        )
        return chosen

    async def fence_row(self, pool: Any = None) -> tuple[Any, ...] | None:
        rows = await fetch_all(
            pool or self.pool,
            "SELECT epoch, token, previous_token FROM adcp_reporting_orphan_collector_fences",
        )
        return rows[0] if rows else None

    async def collect(self, **overrides: Any) -> ReportingOrphanCollectionResultV1:
        arguments: dict[str, Any] = {"now": later(), "acknowledge_exclusive_namespace": True}
        arguments.update(overrides)
        return await self.store.collect_orphaned_row_objects(**arguments)


def binding_result(
    result: ReportingOrphanCollectionResultV1, binding_id: str
) -> ReportingOrphanBindingResultV1:
    return next(item for item in result.bindings if item.binding_id == binding_id)


def row_storage_options(tmp_path: Path, provider: HookedProvider) -> ReportingRowStorageOptionsV1:
    return ReportingRowStorageOptionsV1(
        deployment_namespace="orphans",
        bindings={
            "inline": ReportingPostgresRowBindingV1(),
            "objects": ReportingObjectRowBindingV1(
                provider="filesystem", location=LOCATION, prefix="orph"
            ),
            "legacy": ReportingObjectRowBindingV1(
                provider="filesystem", location=LOCATION, prefix="legacy", read_only=True
            ),
            "nolist": ReportingObjectRowBindingV1(
                provider="nolist", location=LOCATION, prefix="nolist"
            ),
            # Shares ``objects``' root and prefix, as a renamed or successor binding does.
            "twin": ReportingObjectRowBindingV1(
                provider="filesystem",
                location=LOCATION,
                prefix="orph",
                key_template="{prefix}/{namespace_key}/twin/{revision_key}/{content_sha256}/{chunk_index}",
                read_only=True,
            ),
        },
        providers={
            "filesystem": provider,  # type: ignore[dict-item]
            "nolist": NoListProvider(provider.inner),  # type: ignore[dict-item]
        },
        select_row_binding=lambda selection: "objects",
    )


@asynccontextmanager
async def orphan_fixture(tmp_path: Path, **store_options: Any) -> AsyncIterator[Fixture]:
    async with isolated_reporting_pool() as pool:
        provider = HookedProvider(
            create_filesystem_reporting_row_object_provider_v1(roots={"rows": tmp_path / "rows"})
        )
        options = row_storage_options(tmp_path, provider)
        store = PgReportingLedgerStore(pool=pool, row_storage=options, **store_options)
        await store.create_schema()
        assert store._row_storage is not None
        namespace_key = store._row_storage.installation_identity[1]
        yield Fixture(pool, store, tmp_path, provider, namespace_key, options)


async def clone_schema(pool: Any) -> str:
    """Copy every row-storage table into a new schema: a point-in-time clone of the ledger.

    Returns the schema name; ``cloned_store`` opens a store on it. The copy keeps the installation
    identity, the namespace key, the bindings and the fence row the live database had when it was
    taken. The row-storage DDL is applied first so the clone carries the same triggers and
    constraints the catalog check requires.
    """
    from adcp_reporting_storage.ledger.row_storage import REPORTING_ROW_STORAGE_DDL_PATH
    from psycopg import AsyncConnection, sql

    url = os.environ["ADCP_PG_TEST_URL"]
    async with pool.connection() as live:
        source = (await (await live.execute("SELECT current_schema()")).fetchone())[0]
    target = f"adcp_reporting_clone_{secrets.token_hex(6)}"
    async with await AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(target)))
        await admin.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(target)))
        await admin.execute(REPORTING_ROW_STORAGE_DDL_PATH.read_text())
        await admin.execute("DELETE FROM adcp_persistence_installation")
        for table in OWNED_TABLES:
            await admin.execute(
                sql.SQL("INSERT INTO {}.{} SELECT * FROM {}.{}").format(
                    sql.Identifier(target),
                    sql.Identifier(table),
                    sql.Identifier(source),
                    sql.Identifier(table),
                )
            )
    return target


@asynccontextmanager
async def cloned_store(fixture: Fixture, schema: str) -> AsyncIterator[PgReportingLedgerStore]:
    """A second store on the clone: same configuration, its own database state."""
    url = os.environ["ADCP_PG_TEST_URL"]
    from psycopg import AsyncConnection, sql
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(
        url,
        kwargs={
            "options": f"-csearch_path={schema} -cstatement_timeout=15000",
            "autocommit": False,
        },
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.wait(timeout=10)
        store = PgReportingLedgerStore(pool=pool, row_storage=fixture.options)
        await store.ready_row_storage()
        try:
            yield store
        finally:
            async with await AsyncConnection.connect(url, autocommit=True) as admin:
                await admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )


def require_database() -> None:
    if not os.environ.get("ADCP_PG_TEST_URL"):
        pytest.skip("ADCP_PG_TEST_URL not set — requires real PostgreSQL")


async def count_changes(fixture: Fixture) -> int:
    rows = await fetch_all(fixture.pool, "SELECT count(*) FROM adcp_reporting_row_write_intents")
    return int(rows[0][0])


__all__ = [
    "CONTEXT",
    "DAY",
    "LOCATION",
    "Fixture",
    "HookedProvider",
    "NoListProvider",
    "binding_result",
    "clone_schema",
    "cloned_store",
    "later",
    "orphan_fixture",
    "ReportingRowObjectListEntryV1",
]
