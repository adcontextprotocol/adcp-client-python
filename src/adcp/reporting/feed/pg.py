"""Connection-bound frozen reads over the unchanged receipt/materializer store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.feed._errors import storage_errors
from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.feed.projection import capture_feed
from adcp.reporting.feed.request import FeedRequest
from adcp.reporting.feed.schema import validate_feed_schema
from adcp.reporting.feed.snapshot import (
    ReportingFeedSnapshot,
    StoredFeedSnapshot,
    decode_snapshot,
    token_position,
)
from adcp.reporting.ledger.delivery_changes import ReportingReconciliationChange
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.pg import _BOUND_CONNECTION
from adcp.reporting.ledger.status_projection import ReportingStatusSnapshot
from adcp.reporting.ledger.status_snapshot import read_snapshot_on
from adcp.reporting.materializer.capture import decode_materializer_boundary
from adcp.reporting.materializer.pg import _now
from adcp.reporting.receipts.capture import decode_receipt_boundary
from adcp.reporting.receipts.pg import PgReportingReceiptStore
from adcp.server.helpers import inject_context


@dataclass(frozen=True, repr=False)
class _CapturedFeed:
    core: ReportingStatusSnapshot
    changes: tuple[ReportingReconciliationChange, ...]
    materializer: tuple[dict[str, Any], ...]
    receipts: tuple[dict[str, Any], ...]
    representation_version: Literal[1, 2] = 1
    revision_ownership: bool = False
    activated_consumer_status_enabled: bool | None = None


@dataclass(frozen=True, repr=False)
class _PreparedFeed:
    snapshot: ReportingFeedSnapshot
    signing_key: bytes
    document: str
    content_sha256: str
    page: dict[str, Any]


def _prepare_feed(
    captured: _CapturedFeed,
    caller: ReportingDeliveryPrincipal,
    request: FeedRequest,
    after: tuple[int, int],
    consumer_status_enabled: bool,
) -> _PreparedFeed:
    """Pure work over detached capture; never owns a connection or writer lock."""
    snapshot = capture_feed(
        captured.core,
        captured.changes,
        caller=caller,
        request=request,
        after=after,
        consumer_status_enabled=consumer_status_enabled,
        materializer_boundaries=tuple(
            decode_materializer_boundary(r) for r in captured.materializer
        ),
        receipt_boundaries=tuple(decode_receipt_boundary(r) for r in captured.receipts),
        representation_version=captured.representation_version,
        revision_ownership=captured.revision_ownership,
        activated_consumer_status_enabled=captured.activated_consumer_status_enabled,
    )
    stored = StoredFeedSnapshot(snapshot, secrets.token_bytes(32))
    document = canonical_json_utf8_v1(snapshot.to_storage())
    decode_snapshot(json.loads(document))
    return _PreparedFeed(
        snapshot,
        stored.signing_key,
        document.decode(),
        hashlib.sha256(document).hexdigest(),
        stored.page(0, request.limit),
    )


class PgReportingFeedStore(PgReportingReceiptStore):
    @storage_errors
    async def create_schema(self) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._create_schema_on(connection)
            root = files("adcp.reporting.ledger")
            for name in (
                "reporting_materializer.sql",
                "reporting_receipt_ingestion.sql",
                "reporting_feed.sql",
            ):
                await connection.execute(root.joinpath(name).read_text())

    @storage_errors
    async def reporting_feed_ready(self) -> bool:
        async with self._connection() as connection:
            await validate_feed_schema(connection, notifications=self._notifications_enabled)
        return True

    async def _feed_snapshot_on(
        self, connection: Any, snapshot_id: str, caller: ReportingDeliveryPrincipal
    ) -> StoredFeedSnapshot | None:
        row = await (
            await connection.execute(
                "SELECT document, content_sha256, signing_key FROM reporting_feed_snapshots"
                " WHERE account_id=%s AND consumer_id=%s AND snapshot_id=%s",
                (caller.account_id, caller.consumer_id, snapshot_id),
            )
        ).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1] or len(row[2]) != 32:
            raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
        snapshot = decode_snapshot(json.loads(row[0]))
        if snapshot.caller != caller or snapshot.snapshot_id != snapshot_id:
            raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
        return StoredFeedSnapshot(snapshot, bytes(row[2]))

    async def _save_feed_snapshot_on(self, connection: Any, stored: _PreparedFeed) -> None:
        snapshot = stored.snapshot
        await connection.execute(
            "INSERT INTO reporting_feed_snapshots"
            " (account_id,consumer_id,snapshot_id,as_of,representation_version,ownership_mode,"
            " document,content_sha256,signing_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                snapshot.caller.account_id,
                snapshot.caller.consumer_id,
                snapshot.snapshot_id,
                snapshot.as_of,
                snapshot.representation_version,
                snapshot.ownership_mode,
                stored.document,
                stored.content_sha256,
                stored.signing_key,
            ),
        )

    async def _capture_feed_on(
        self,
        connection: Any,
        caller: ReportingDeliveryPrincipal,
    ) -> _CapturedFeed:
        # Database time and both histories share the writer's account lock. No
        # public reader, second connection, lifecycle settlement, or queue write.
        as_of = await _now(connection)
        core = await read_snapshot_on(connection, account_id=caller.account_id, as_of=as_of)
        await self._validate_feed(connection, caller)
        changes = await self._changes(connection, caller)
        materializer = await (
            await connection.execute(
                "SELECT input,content_sha256=reporting_payload_sha256(input)"
                " FROM reporting_materializer_status_boundaries"
                " WHERE account_id=%s AND consumer_id=%s ORDER BY sequence",
                (caller.account_id, caller.consumer_id),
            )
        ).fetchall()
        receipts = await (
            await connection.execute(
                "SELECT input,content_sha256=reporting_receipt_ingestion_sha256(input)"
                " FROM reporting_receipt_ingestion_boundaries"
                " WHERE account_id=%s AND consumer_id=%s ORDER BY sequence",
                (caller.account_id, caller.consumer_id),
            )
        ).fetchall()
        if any(not r[1] for r in (*materializer, *receipts)):
            raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
        return _CapturedFeed(
            core,
            changes,
            tuple(r[0] for r in materializer),
            tuple(r[0] for r in receipts),
        )

    @storage_errors
    async def read_reporting_feed(
        self,
        request: dict[str, Any],
        *,
        caller: ReportingDeliveryPrincipal,
        consumer_status_enabled: bool = False,
        reauthorize: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        parsed = FeedRequest.parse(request)
        bound = _BOUND_CONNECTION.get()
        if bound is not None and bound[:2] == (self._pool, asyncio.current_task()):
            # A savepoint cannot release the outer transaction's account lock
            # or establish that its uncommitted history is a public boundary.
            raise ReportingFeedError("REPORTING_FEED_TRANSACTION_UNAVAILABLE")
        async with self._connection() as connection, connection.transaction():
            await validate_feed_schema(connection, notifications=self._notifications_enabled)
            stored = None
            offset = 0
            # Restore before inspecting current projection/evidence. Authorization
            # may deny a caller; no missing/old token silently opens a new walk.
            if parsed.cursor is not None:
                stored = await self._feed_snapshot_on(
                    connection, token_position(parsed.cursor)[2], caller
                )
                if stored is None:
                    raise ReportingFeedError("INVALID_CHECKPOINT")
                offset = stored.check(parsed.cursor, "cursor", parsed, caller)
            after = (0, 0)
            if parsed.changes_after is not None:
                previous = await self._feed_snapshot_on(
                    connection, token_position(parsed.changes_after)[2], caller
                )
                if previous is None:
                    raise ReportingFeedError("INVALID_CHECKPOINT")
                previous.check(parsed.changes_after, "checkpoint", parsed, caller)
                after = previous.snapshot.through
                if stored is not None and stored.snapshot.after != after:
                    raise ReportingFeedError("INVALID_CHECKPOINT")
            if stored is None:
                await self._lock_account(connection, caller.account_id)
                captured = await self._capture_feed_on(connection, caller)
        # Both the transaction and pooled connection have been released. Slow
        # graph closure, canonicalization and token/page construction must not
        # serialize receipt/materializer writers, including a size-one pool.
        if stored is not None:
            page = await asyncio.to_thread(stored.page, offset, parsed.limit)
            if reauthorize is not None:
                await reauthorize()
            return inject_context(request, page)
        prepared = await asyncio.to_thread(
            _prepare_feed, captured, caller, parsed, after, consumer_status_enabled
        )
        page = inject_context(request, prepared.page)
        if reauthorize is not None:
            # The application ACL can use the same size-one pool: no connection
            # is borrowed while the callback rechecks this captured principal.
            await reauthorize()
        async with self._connection() as connection, connection.transaction():
            await validate_feed_schema(connection, notifications=self._notifications_enabled)
            # This independent immutable row needs no account writer lock.
            # Insert/commit failure publishes no page and cannot undo writers
            # that committed after the historical capture boundary.
            await self._save_feed_snapshot_on(connection, prepared)
        return page

    @storage_errors
    async def read_reporting_feed_snapshot(
        self, snapshot_id: str, *, caller: ReportingDeliveryPrincipal
    ) -> ReportingFeedSnapshot | None:
        async with self._connection() as connection, connection.transaction():
            await validate_feed_schema(connection, notifications=self._notifications_enabled)
            stored = await self._feed_snapshot_on(connection, snapshot_id, caller)
            return stored.snapshot if stored else None
