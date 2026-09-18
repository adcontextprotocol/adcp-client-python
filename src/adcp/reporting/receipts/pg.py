"""Durable mixed-batch ordinals on the materializer's account-locked connection."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from datetime import datetime
from functools import wraps
from importlib.resources import files
from typing import Any, ParamSpec, TypeVar, cast

from adcp.reporting.canonical_json import canonical_json_sha256_v1
from adcp.reporting.ledger._delivery_state import RecordT
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingReceiptRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.pg import (
    _OBLIGATION_COLUMNS,
    _REVISION_COLUMNS,
    _json,
    _obligation_from_row,
    _revision_from_row,
)
from adcp.reporting.ledger.status_snapshot import settle_snapshot_on
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.capture import private_snapshot
from adcp.reporting.materializer.pg import PgReportingMaterializerStore, _now
from adcp.reporting.receipts._diagnostics import _Boundary, _storage_failure
from adcp.reporting.receipts.capture import ReportingReceiptBoundary, decode_receipt_boundary
from adcp.reporting.receipts.errors import ReportingReceiptError
from adcp.reporting.receipts.records import (
    failed_result,
    prepare_receipt,
    receipt_record,
    success_result,
)
from adcp.reporting.receipts.schema import validate_receipt_schema
from adcp.reporting.receipts.wire import (
    ReceiptBatch,
    ReceiptKind,
    validate_receipt_response,
    validate_receipt_results,
)
from adcp.server.helpers import inject_context

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _storage_errors(
    boundary: _Boundary,
) -> Callable[[Callable[_P, Coroutine[Any, Any, _R]]], Callable[_P, Coroutine[Any, Any, _R]]]:
    def decorate(
        fn: Callable[_P, Coroutine[Any, Any, _R]],
    ) -> Callable[_P, Coroutine[Any, Any, _R]]:
        @wraps(fn)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return await fn(*args, **kwargs)
            except ReportingReceiptError:
                raise
            except Exception as error:
                _storage_failure(error, boundary=boundary)
                unavailable = ReportingReceiptError("RECEIPT_STORAGE_UNAVAILABLE")
            # Outside the driver exception scope: no SQL/provider detail in __context__.
            raise unavailable

        return wrapped

    return decorate


class PgReportingReceiptStore(PgReportingMaterializerStore):
    """One composition: old public stores plus optional ingestion and capture.

    No network I/O, session lock, or second pool participates in an ordinal.
    All actual receipt timestamps come from PostgreSQL, including when an old
    public conformance clock was supplied to the inherited constructor.
    """

    @_storage_errors("store.create_schema")
    async def create_schema(self) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._create_schema_on(connection)
            root = files("adcp.reporting.ledger")
            await connection.execute(root.joinpath("reporting_materializer.sql").read_text())
            await connection.execute(root.joinpath("reporting_receipt_ingestion.sql").read_text())

    @_storage_errors("store.receipt_ingestion_ready")
    async def receipt_ingestion_ready(self) -> bool:
        async with self._connection() as connection:
            await validate_receipt_schema(connection, notifications=self._notifications_enabled)
        return True

    async def _delivery_time_on(self, connection: Any, record: ReportingDeliveryRecord) -> datetime:
        if isinstance(record, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)):
            return await _now(connection)
        return await super()._delivery_time_on(connection, record)

    async def _commit_record_on(
        self, connection: Any, record: RecordT, *, notify: bool = True, dirty: bool = True
    ) -> tuple[RecordT, bool]:
        stored, added = await super()._commit_record_on(
            connection, record, notify=notify, dirty=dirty
        )
        if added and isinstance(
            stored, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)
        ):
            await self._capture_receipt_on(connection, stored)
        return stored, added

    async def _receipt_batch_on(
        self, connection: Any, caller: ReportingDeliveryPrincipal, batch: ReceiptBatch
    ) -> tuple[Any, ...]:
        key = caller.account_id, caller.consumer_id, batch.key
        row = await (
            await connection.execute(
                "SELECT canonical_request,request_sha256,expected_count,final_response,final_sha256"
                " FROM reporting_receipt_ingestion_batches"
                " WHERE account_id=%s AND consumer_id=%s AND idempotency_key=%s FOR UPDATE",
                key,
            )
        ).fetchone()
        if row is None:
            await connection.execute(
                "INSERT INTO reporting_receipt_ingestion_batches"
                " (account_id,consumer_id,idempotency_key,canonical_request,"
                " request_sha256,expected_count)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (*key, batch.canonical_request.decode(), batch.digest, len(batch.items)),
            )
            return batch.canonical_request.decode(), batch.digest, len(batch.items), None, None
        if row[0] != batch.canonical_request.decode() or row[1] != batch.digest:
            raise ReportingReceiptError("IDEMPOTENCY_CONFLICT")
        if row[2] != len(batch.items):
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        return tuple(row)

    async def _receipt_results_on(
        self, connection: Any, caller: ReportingDeliveryPrincipal, batch: ReceiptBatch
    ) -> list[dict[str, Any]]:
        rows = await (
            await connection.execute(
                "SELECT ordinal,receipt_kind,reporting_receipt_id,result,content_sha256"
                " FROM reporting_receipt_ingestion_results"
                " WHERE account_id=%s AND consumer_id=%s AND idempotency_key=%s ORDER BY ordinal",
                (caller.account_id, caller.consumer_id, batch.key),
            )
        ).fetchall()
        items = batch.items
        if len(rows) > len(items) or any(
            r[0] != i
            or r[1] != items[i][0]
            or r[2] != items[i][1]["reporting_receipt_id"]
            or canonical_json_sha256_v1(r[3]) != r[4]
            for i, r in enumerate(rows)
        ):
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        results = [r[3] for r in rows]
        validate_receipt_results(results, batch)
        return results

    async def _prepare_receipt_on(
        self,
        connection: Any,
        caller: ReportingDeliveryPrincipal,
        kind: ReceiptKind,
        item: dict[str, Any],
    ) -> tuple[ReportingReceiptRecord, dict[str, Any], bool]:
        revision_id = item.get("reporting_revision_id", item.get("adjusts_reporting_revision_id"))
        revision = await (
            await connection.execute(
                # SDK-owned column list; every request value is bound below.
                f"SELECT {_REVISION_COLUMNS} FROM reporting_revisions"  # nosec B608
                " WHERE account_id=%s AND reporting_revision_id=%s",
                (caller.account_id, revision_id),
            )
        ).fetchone()
        obligation_id = item.get("reporting_obligation_id")
        if kind == "adjustment_receipt" and revision is not None:
            obligation_id = _revision_from_row(revision).reporting_obligation_id
        row = await (
            await connection.execute(
                # SDK-owned column list; every request value is bound below.
                f"SELECT {_OBLIGATION_COLUMNS} FROM reporting_obligations"  # nosec B608
                " WHERE account_id=%s AND reporting_obligation_id=%s",
                (caller.account_id, obligation_id),
            )
        ).fetchone()
        obligation = _obligation_from_row(row) if row else None
        candidate = receipt_record(kind, item, caller, obligation)
        records = await self._records(connection, caller)
        revisions = await (
            await connection.execute(
                # SDK-owned column list; every request value is bound below.
                f"SELECT {_REVISION_COLUMNS} FROM reporting_revisions"  # nosec B608
                " WHERE account_id=%s AND reporting_obligation_id=%s",
                (caller.account_id, obligation_id),
            )
        ).fetchall()
        stored, added = prepare_receipt(
            candidate,
            records,
            await self._delivery_context(connection, candidate),
            tuple(_revision_from_row(r) for r in revisions),
            await _now(connection),
        )
        return candidate, success_result(stored, added), added

    async def _insert_receipt_result_on(
        self,
        connection: Any,
        caller: ReportingDeliveryPrincipal,
        batch: ReceiptBatch,
        ordinal: int,
        result: dict[str, Any],
    ) -> None:
        kind, item = batch.items[ordinal]
        await connection.execute(
            "INSERT INTO reporting_receipt_ingestion_results"
            " (account_id,consumer_id,idempotency_key,ordinal,receipt_kind,reporting_receipt_id,"
            " receipt_record_id,result,content_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                caller.account_id,
                caller.consumer_id,
                batch.key,
                ordinal,
                kind,
                item["reporting_receipt_id"],
                item["reporting_receipt_id"] if result["result"] != "failed" else None,
                _json(result),
                canonical_json_sha256_v1(result),
            ),
        )

    async def _save_receipt_response_on(
        self,
        connection: Any,
        caller: ReportingDeliveryPrincipal,
        batch: ReceiptBatch,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        response = inject_context(batch.request, {"status": "completed", "results": results})
        validate_receipt_response(response, batch)
        await connection.execute(
            "UPDATE reporting_receipt_ingestion_batches"
            " SET final_response=%s::jsonb,final_sha256=%s,finalized_at=clock_timestamp()"
            " WHERE account_id=%s AND consumer_id=%s AND idempotency_key=%s",
            (
                _json(response),
                canonical_json_sha256_v1(response),
                caller.account_id,
                caller.consumer_id,
                batch.key,
            ),
        )
        return response

    def _assemble_receipt_response(
        self,
        response: dict[str, Any],
        digest: str,
        results: list[dict[str, Any]],
        batch: ReceiptBatch,
    ) -> dict[str, Any]:
        validate_receipt_response(response, batch)
        if canonical_json_sha256_v1(response) != digest or response["results"] != results:
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        return cast(dict[str, Any], json.loads(_json(response)))

    @_storage_errors("store.ingest_receipt_batch")
    async def ingest_receipt_batch(
        self, request: dict[str, Any], *, caller: ReportingDeliveryPrincipal
    ) -> dict[str, Any]:
        batch = ReceiptBatch.parse(request)
        checked = False
        while True:
            async with self._connection() as connection, connection.transaction():
                # Account advisory lock precedes the batch row; no lock pool or
                # second connection can deadlock a size-one application pool.
                await self._lock_account(connection, caller.account_id)
                if not checked:
                    await validate_receipt_schema(
                        connection, notifications=self._notifications_enabled
                    )
                    checked = True
                row = await self._receipt_batch_on(connection, caller, batch)
                results = await self._receipt_results_on(connection, caller, batch)
                if row[3] is not None:
                    return self._assemble_receipt_response(row[3], row[4], results, batch)
                if len(results) == len(batch.items):
                    response = await self._save_receipt_response_on(
                        connection, caller, batch, results
                    )
                    return self._assemble_receipt_response(
                        response, canonical_json_sha256_v1(response), results, batch
                    )
                kind, item = batch.items[len(results)]
                candidate = None
                try:
                    candidate, result, added = await self._prepare_receipt_on(
                        connection, caller, kind, item
                    )
                except LedgerConflictError as error:
                    result, added = failed_result(item, error), False
                # Only pure semantic preparation is caught. SQL/capture/result
                # failures roll back this ordinal instead of recording a failure
                # beside a partially inserted receipt.
                if added:
                    assert candidate is not None
                    stored, inserted = await self._commit_record_on(connection, candidate)
                    assert inserted
                    result = success_result(stored, True)
                await self._insert_receipt_result_on(
                    connection, caller, batch, len(results), result
                )

    async def _capture_receipt_on(self, connection: Any, receipt: ReportingReceiptRecord) -> None:
        assert receipt.received_at is not None
        caller = receipt.scope.principal
        core = await settle_snapshot_on(
            self, connection, account_id=caller.account_id, as_of=receipt.received_at
        )
        row = await (
            await connection.execute(
                "INSERT INTO reporting_receipt_ingestion_heads"
                " (account_id,consumer_id,max_sequence)"
                " VALUES (%s,%s,1) ON CONFLICT (account_id,consumer_id) DO UPDATE"
                " SET max_sequence=reporting_receipt_ingestion_heads.max_sequence+1"
                " RETURNING max_sequence",
                (caller.account_id, caller.consumer_id),
            )
        ).fetchone()
        account = await (
            await connection.execute(
                "UPDATE reporting_materializer_accounts SET captured_sequence=captured_sequence+1"
                " WHERE account_id=%s RETURNING captured_sequence",
                (caller.account_id,),
            )
        ).fetchone()
        if row is None or account is None:
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        value = ReportingReceiptBoundary(
            caller,
            row[0],
            account[0],
            receipt.reporting_receipt_id,
            receipt.received_at,
            private_snapshot(core, caller),
            await self._records(connection, caller),
        ).to_storage()
        await connection.execute(
            "INSERT INTO reporting_receipt_ingestion_boundaries"
            " (account_id,consumer_id,sequence,account_sequence,reporting_receipt_id,"
            " as_of,input,content_sha256)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                caller.account_id,
                caller.consumer_id,
                row[0],
                account[0],
                receipt.reporting_receipt_id,
                receipt.received_at,
                _json(value),
                canonical_json_sha256_v1(value),
            ),
        )

    @_storage_errors("store.read_receipt_boundaries")
    async def read_receipt_boundaries(
        self, *, caller: ReportingDeliveryPrincipal, after: int = 0, limit: int = 100
    ) -> tuple[ReportingReceiptBoundary, ...]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ReportingReceiptError("INVALID_REQUEST")
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, caller.account_id)
            await validate_receipt_schema(connection, notifications=self._notifications_enabled)
            rows = await (
                await connection.execute(
                    "SELECT input,content_sha256=reporting_receipt_ingestion_sha256(input)"
                    " FROM reporting_receipt_ingestion_boundaries"
                    " WHERE account_id=%s AND consumer_id=%s AND sequence>%s"
                    " ORDER BY sequence LIMIT %s",
                    (caller.account_id, caller.consumer_id, after, limit),
                )
            ).fetchall()
            if any(not r[1] for r in rows):
                raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
            return tuple(decode_receipt_boundary(r[0]) for r in rows)
