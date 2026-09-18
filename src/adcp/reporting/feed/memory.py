"""Reference feed participant sharing the approved memory rollback boundary."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.feed._errors import storage_errors
from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.feed.projection import capture_feed
from adcp.reporting.feed.request import FeedRequest
from adcp.reporting.feed.snapshot import (
    ReportingFeedSnapshot,
    StoredFeedSnapshot,
    decode_snapshot,
    token_position,
)
from adcp.reporting.ledger._delivery_state import principal
from adcp.reporting.ledger.delivery_changes import ReportingReconciliationChange
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.status_snapshot import memory_snapshot
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.receipts.memory import InMemoryReportingReceiptStore
from adcp.server.helpers import inject_context


class InMemoryReportingFeedStore(InMemoryReportingReceiptStore):
    """Persisted for this store's lifetime; production uses PgReportingFeedStore."""

    _reporting_feed_snapshots: dict[str, tuple[bytes, str, bytes]]

    def _feed_projection_options(self, caller: ReportingDeliveryPrincipal) -> dict[str, Any]:
        return {}

    def _feed_snapshot(
        self, snapshot_id: str, caller: ReportingDeliveryPrincipal
    ) -> StoredFeedSnapshot | None:
        value = getattr(self, "_reporting_feed_snapshots", {}).get(snapshot_id)
        if value is None:
            return None
        document, digest, key = value
        if hashlib.sha256(document).hexdigest() != digest or len(key) != 32:
            raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
        snapshot = decode_snapshot(json.loads(document))
        if snapshot.snapshot_id != snapshot_id:
            raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
        if snapshot.caller != caller:
            return None
        return StoredFeedSnapshot(snapshot, key)

    def _save_feed_snapshot(self, stored: StoredFeedSnapshot) -> None:
        document = canonical_json_utf8_v1(stored.snapshot.to_storage())
        decode_snapshot(json.loads(document))
        if not hasattr(self, "_reporting_feed_snapshots"):
            self._reporting_feed_snapshots = {}
        if stored.snapshot.snapshot_id in self._reporting_feed_snapshots:
            raise ReportingFeedError("REPORTING_FEED_STORAGE_UNAVAILABLE")
        self._reporting_feed_snapshots[stored.snapshot.snapshot_id] = (
            document,
            hashlib.sha256(document).hexdigest(),
            stored.signing_key,
        )

    def _capture_feed(
        self,
        caller: ReportingDeliveryPrincipal,
        request: FeedRequest,
        after: tuple[int, int],
        consumer_status_enabled: bool,
    ) -> ReportingFeedSnapshot:
        owned = tuple(
            (seq, who, record)
            for seq, who, record in getattr(self, "_delivery_records", ())
            if who == caller or principal(record) == caller
        )
        if any(who != caller or principal(record) != caller for _, who, record in owned):
            raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
        changes = tuple(ReportingReconciliationChange(seq, record) for seq, _, record in owned)
        return capture_feed(
            memory_snapshot(self, caller.account_id),
            changes,
            caller=caller,
            request=request,
            after=after,
            consumer_status_enabled=consumer_status_enabled,
            materializer_boundaries=tuple(
                b for b in getattr(self, "_materializer_boundaries", ()) if b.caller == caller
            ),
            receipt_boundaries=tuple(
                b for b in getattr(self, "_receipt_boundaries", ()) if b.caller == caller
            ),
            **self._feed_projection_options(caller),
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
        try:
            async with self._mutation():
                stored = None
                offset = 0
                if parsed.cursor is not None:
                    stored = self._feed_snapshot(token_position(parsed.cursor)[2], caller)
                    if stored is None:
                        raise ReportingFeedError("INVALID_CHECKPOINT")
                    offset = stored.check(parsed.cursor, "cursor", parsed, caller)
                after = (0, 0)
                if parsed.changes_after is not None:
                    previous = self._feed_snapshot(token_position(parsed.changes_after)[2], caller)
                    if previous is None:
                        raise ReportingFeedError("INVALID_CHECKPOINT")
                    previous.check(parsed.changes_after, "checkpoint", parsed, caller)
                    after = previous.snapshot.through
                    if stored is not None and stored.snapshot.after != after:
                        raise ReportingFeedError("INVALID_CHECKPOINT")
                if stored is None:
                    stored = StoredFeedSnapshot(
                        self._capture_feed(caller, parsed, after, consumer_status_enabled),
                        secrets.token_bytes(32),
                    )
                    if reauthorize is not None:
                        await reauthorize()
                    self._save_feed_snapshot(stored)
                elif reauthorize is not None:
                    await reauthorize()
                return inject_context(request, stored.page(offset, parsed.limit))
        except LedgerConflictError:
            raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT") from None

    @storage_errors
    async def read_reporting_feed_snapshot(
        self, snapshot_id: str, *, caller: ReportingDeliveryPrincipal
    ) -> ReportingFeedSnapshot | None:
        async with self._lock:
            stored = self._feed_snapshot(snapshot_id, caller)
            return stored.snapshot if stored else None

    async def reporting_feed_ready(self) -> bool:
        return True
