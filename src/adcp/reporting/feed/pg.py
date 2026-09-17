"""Connection-bound frozen reads over the unchanged receipt/materializer store."""

from __future__ import annotations

import hashlib
import json
import secrets
from importlib.resources import files
from typing import Any

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
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.status_snapshot import read_snapshot_on
from adcp.reporting.materializer.capture import decode_materializer_boundary
from adcp.reporting.materializer.pg import _now
from adcp.reporting.receipts.capture import decode_receipt_boundary
from adcp.reporting.receipts.pg import PgReportingReceiptStore
from adcp.server.helpers import inject_context


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

    async def _save_feed_snapshot_on(self, connection: Any, stored: StoredFeedSnapshot) -> None:
        snapshot = stored.snapshot
        document = canonical_json_utf8_v1(snapshot.to_storage())
        decode_snapshot(json.loads(document))
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
                document.decode(),
                hashlib.sha256(document).hexdigest(),
                stored.signing_key,
            ),
        )

    async def _capture_feed_on(
        self,
        connection: Any,
        caller: ReportingDeliveryPrincipal,
        request: FeedRequest,
        after: tuple[int, int],
        consumer_status_enabled: bool,
    ) -> ReportingFeedSnapshot:
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
        return capture_feed(
            core,
            changes,
            caller=caller,
            request=request,
            after=after,
            consumer_status_enabled=consumer_status_enabled,
            materializer_boundaries=tuple(decode_materializer_boundary(r[0]) for r in materializer),
            receipt_boundaries=tuple(decode_receipt_boundary(r[0]) for r in receipts),
        )

    @storage_errors
    async def read_reporting_feed(
        self,
        request: dict[str, Any],
        *,
        caller: ReportingDeliveryPrincipal,
        consumer_status_enabled: bool = False,
    ) -> dict[str, Any]:
        parsed = FeedRequest.parse(request)
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, caller.account_id)
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
                snapshot = await self._capture_feed_on(
                    connection, caller, parsed, after, consumer_status_enabled
                )
                stored = StoredFeedSnapshot(snapshot, secrets.token_bytes(32))
                await self._save_feed_snapshot_on(connection, stored)
            return inject_context(request, stored.page(offset, parsed.limit))

    @storage_errors
    async def read_reporting_feed_snapshot(
        self, snapshot_id: str, *, caller: ReportingDeliveryPrincipal
    ) -> ReportingFeedSnapshot | None:
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, caller.account_id)
            await validate_feed_schema(connection, notifications=self._notifications_enabled)
            stored = await self._feed_snapshot_on(connection, snapshot_id, caller)
            return stored.snapshot if stored else None
