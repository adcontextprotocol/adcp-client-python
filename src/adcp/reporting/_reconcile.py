"""Reliable AdCP reporting-ledger reconciliation.

The wire schemas describe facts.  This module turns those facts into the
operational guarantee buyers care about: a closed, retained reporting scope
whose expected periods, current revisions, destination materializations, and
consumer receipts all agree.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from adcp.reporting._consumer import (
    ConsumerLoopView,
    ConsumerStatusCheckpoint,
    ConsumerStatusCheckpointStore,
    ConsumerStatusIntent,
    ConsumerStatusPlanError,
    ConsumerStatusPostError,
    ConsumerStatusPostResult,
    InMemoryConsumerStatusCheckpoints,
    ReportingContentReading,
    ReportingFailureCode,
    ReportingOperationsContactView,
    ReportingPinnedDefinition,
    classify_content_mismatch,
    consumer_status_chain_key,
    load_consumer_loop_view,
    plan_consumer_statuses,
    post_consumer_statuses,
    resolve_checkpointed_leaves,
)
from adcp.reporting.ownership import ReportingOwnershipError, page_revision_ownership
from adcp.reporting.revision_selection import RevisionHistoryEntry, select_reporting_revision
from adcp.types import (
    GetReportingStatusRequest,
    GetReportingStatusResponse,
    ReportingAdjustment,
    ReportingAdjustmentReceipt,
    ReportingCanonicalContentDigest,
    ReportingControlTotal,
    ReportingDeliveryCapabilities,
    ReportingMaterialization,
    ReportingObligation,
    ReportingReceipt,
    ReportingRevision,
    ReportingStatusIssue,
    SyncReportingReceiptsRequest,
    SyncReportingReceiptsResponse,
)
from adcp.types.core import TaskResult

if TYPE_CHECKING:
    from adcp.reporting_inspection import ReportingResourceReader


class ReportingStatusClient(Protocol):
    async def get_reporting_status(
        self, request: GetReportingStatusRequest
    ) -> TaskResult[GetReportingStatusResponse]:
        raise NotImplementedError


class ReportingReconciliationClient(ReportingStatusClient, Protocol):

    async def sync_reporting_receipts(
        self, request: SyncReportingReceiptsRequest
    ) -> TaskResult[SyncReportingReceiptsResponse]:
        raise NotImplementedError


class ReportingCheckpointStore(Protocol):
    async def get(self, reporting_materialization_id: str) -> ReportingReceipt | None:
        raise NotImplementedError

    async def put(self, receipt: ReportingReceipt) -> None:
        raise NotImplementedError


class ReportingReconciliationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReportingTier(str, Enum):
    """Feature tiers advertised by ``media_buy.reporting_delivery``."""

    CORE = "core"
    MANAGED_DELIVERY = "managed_delivery"
    RECONCILED_BILLING = "reconciled_billing"


def reporting_tiers(
    capabilities: ReportingDeliveryCapabilities,
) -> frozenset[ReportingTier]:
    """Project reporting capability flags into their cumulative SDK tiers."""
    tiers = {ReportingTier.CORE}
    if capabilities.managed_delivery:
        tiers.add(ReportingTier.MANAGED_DELIVERY)
    if capabilities.reconciled_billing:
        if not capabilities.managed_delivery:
            raise ReportingReconciliationError(
                "INVALID_REPORTING_CAPABILITIES",
                "reconciled_billing requires managed_delivery",
            )
        tiers.add(ReportingTier.RECONCILED_BILLING)
    return frozenset(tiers)


@dataclass(frozen=True)
class ExpectedReportingPeriod:
    delivery_config_id: str
    delivery_config_version: int
    report_definition_id: str
    feed_purpose: str
    reporting_profile: str
    media_buy_ids: tuple[str, ...]
    period_start: str
    period_end: str
    #: Required to *state* a period the seller omitted: the consumer-status
    #: ``period`` object requires it, and a buyer filing
    #: ``obligation_missing`` has no seller obligation to read it off. Defaults
    #: to UTC because that is what an omitted schedule alignment resolves to;
    #: a configuration with a civil-time alignment must set it explicitly or
    #: the statement describes a different period than the one expected.
    source_timezone: str = "UTC"
    #: The buyer's independently derived ``expected_at``. Needed because
    #: ``obligation_missing`` is only valid at or after it, and the posting
    #: deadline is measured from it -- neither is knowable from the seller's
    #: ledger, which is precisely the point when the period is absent from it.
    expected_at: str | None = None


@dataclass(frozen=True)
class ReportingObservation:
    row_count: int
    control_totals: list[ReportingControlTotal]
    canonical_content_digest: ReportingCanonicalContentDigest | None = None
    manifest_sha256: str | None = None
    native_version_ref: str | None = None
    consumer_commit_ref: str | None = None


@dataclass(frozen=True)
class ReportingInspectionContext:
    obligation: ReportingObligation
    revision: ReportingRevision
    materialization: ReportingMaterialization


@dataclass
class ReportingLedger:
    ledger_snapshot_id: str
    ledger_as_of: datetime
    account_id: str
    scope: BaseModel
    obligations: list[ReportingObligation]
    revisions: list[ReportingRevision]
    materializations: list[ReportingMaterialization]
    receipts: list[ReportingReceipt]
    #: This caller's own consumer-status history, when the seller advertises
    #: the loop. Needed to compare a planned statement against the current
    #: leaf: without it a buyer cannot tell "nothing changed, do not re-file"
    #: from "new claim, must supersede", and re-filing the same claim under a
    #: new id churns the chain for no reason.
    consumer_statuses: list[Any] = field(default_factory=list)
    # None is the all-pages-absent legacy mode. An empty mapping is an explicit
    # new-mode empty snapshot; do not collapse those two meanings.
    revision_ownership: dict[str, str] | None = None
    adjustments: list[ReportingAdjustment] = field(default_factory=list)
    adjustment_receipts: list[ReportingAdjustmentReceipt] = field(default_factory=list)
    # Only the exhausted, non-incremental loader establishes this provenance.
    # A mutable/manual ledger remains useful for diagnostics, never completeness.
    _read_fingerprint: bytes | None = field(default=None, init=False, repr=False, compare=False)

    def __repr__(self) -> str:
        # Resources and extension fields may contain private transport values.
        return (
            f"ReportingLedger(obligations={len(self.obligations)}, "
            f"revisions={len(self.revisions)}, materializations={len(self.materializations)}, "
            f"receipts={len(self.receipts)}, adjustments={len(self.adjustments)}, "
            f"adjustment_receipts={len(self.adjustment_receipts)})"
        )


@dataclass(frozen=True)
class ObligationReconciliation:
    reporting_obligation_id: str
    definitive: bool
    reporting_revision_id: str | None = None
    reporting_materialization_id: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass
class ReportingReconciliationResult:
    definitive: bool
    ledger: ReportingLedger
    obligations: list[ObligationReconciliation]
    missing_expected_periods: list[ExpectedReportingPeriod]
    submitted_receipts: list[ReportingReceipt] = field(default_factory=list)
    totals_by_revision: list[tuple[str, int, list[ReportingControlTotal]]] = field(
        default_factory=list
    )
    #: AdCP 3.2.0-rc.3. What the seller said about *this buyer's* side of the
    #: loop: how many periods it owes a status for, the issue lifecycle fields
    #: for ageing a work item, and where to find a human. ``None`` when the
    #: seller does not advertise ``consumer_status_task``, which is distinct
    #: from an empty view -- see :class:`~adcp.reporting._consumer.ConsumerLoopView`.
    consumer_loop: ConsumerLoopView | None = None
    #: Statements the buyer owes right now, by the rc.3 deadline rather than by
    #: scope close. Empty when nothing is due.
    consumer_status_plan: list[ConsumerStatusIntent] = field(default_factory=list)

    @property
    def consumer_status_pending(self) -> int | None:
        """The seller's ``obligation_counts.consumer_status_pending``, if any."""
        return self.consumer_loop.consumer_status_pending if self.consumer_loop else None

    @property
    def operations_contact(self) -> ReportingOperationsContactView | None:
        """The seller's advertised human escalation path. Never dereferenced."""
        return self.consumer_loop.operations_contact if self.consumer_loop else None

    @property
    def consumer_mismatch_issues(self) -> tuple[ReportingStatusIssue, ...]:
        """Issues the seller raised from this buyer's own statements.

        Each carries ``opened_at`` (stable across re-emission, so it ages as one
        work item), ``issue_state``, and an inert ``external_ref``.
        """
        return self.consumer_loop.mismatch_issues if self.consumer_loop else ()


def _json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _enum(value: object) -> str:
    return str(getattr(value, "value", value))


def _identifiers(values: Iterable[object] | None) -> tuple[str, ...]:
    return tuple(sorted(str(getattr(value, "root", value)) for value in values or []))


def _coverage_is_full(coverage: BaseModel, media_buy_ids: Iterable[object]) -> bool:
    """Apply the reporting-coverage partition invariant before closing a period."""
    expected_media_buys = _identifiers(media_buy_ids)
    package_ids = _identifiers(getattr(coverage, "package_ids", None))
    return bool(
        _enum(getattr(coverage, "status", None)) == "full"
        and _identifiers(getattr(coverage, "media_buy_ids", None)) == expected_media_buys
        and _identifiers(getattr(coverage, "fully_covered_media_buy_ids", None))
        == expected_media_buys
        and not _identifiers(getattr(coverage, "partially_covered_media_buy_ids", None))
        and not _identifiers(getattr(coverage, "unsupported_media_buy_ids", None))
        and not _identifiers(getattr(coverage, "unknown_media_buy_ids", None))
        and _identifiers(getattr(coverage, "covered_package_ids", None)) == package_ids
        and not _identifiers(getattr(coverage, "unsupported_package_ids", None))
        and not _identifiers(getattr(coverage, "unknown_package_ids", None))
    )


def _iso(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


def _totals(value: list[ReportingControlTotal]) -> str:
    return _json(
        sorted((item.model_dump(mode="json") for item in value), key=lambda item: item["name"])
    )


def _revision_matches_obligation(
    revision: ReportingRevision, obligation: ReportingObligation
) -> bool:
    return bool(
        revision.account_id == obligation.account_id
        and revision.report_definition_id == obligation.report_definition_id
        and revision.reporting_profile == obligation.reporting_profile
        and _identifiers(revision.media_buy_ids) == _identifiers(obligation.media_buy_ids)
        and _json(revision.period) == _json(obligation.period)
    )


_RecordT = TypeVar("_RecordT", bound=BaseModel)


def _record_json(value: BaseModel) -> str:
    # Preserve optional-field presence for immutable typed-page comparisons.
    # This is NOT the received canonical bytes of adjustment evidence.
    return _json(value.model_dump(mode="json", exclude_unset=True, exclude_none=False))


def _ledger_fingerprint(ledger: ReportingLedger) -> bytes:
    return hashlib.sha256(
        _json(
            [
                ledger.ledger_snapshot_id,
                ledger.ledger_as_of.isoformat(),
                ledger.account_id,
                _record_json(ledger.scope),
                ledger.revision_ownership,
                *[
                    [_record_json(record) for record in records]
                    for records in (
                        ledger.obligations,
                        ledger.revisions,
                        ledger.materializations,
                        ledger.receipts,
                        ledger.consumer_statuses,
                        ledger.adjustments,
                        ledger.adjustment_receipts,
                    )
                ],
            ]
        ).encode("utf-8")
    ).digest()


def _add_immutable(
    target: dict[str, _RecordT], identifier: str, value: _RecordT, kind: str
) -> None:
    previous = target.get(identifier)
    if previous is not None and _record_json(previous) != _record_json(value):
        raise ReportingReconciliationError(
            "IMMUTABLE_RECORD_CHANGED", f"{kind} changed within one ledger snapshot"
        )
    target[identifier] = value


async def load_reporting_ledger(
    client: ReportingStatusClient,
    request: GetReportingStatusRequest,
    *,
    max_snapshot_restarts: int = 2,
    max_pages: int = 2048,
    max_records: int = 200_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> ReportingLedger:
    """Load a complete authenticated periods snapshot, never an incremental delta.

    Restart at the first page, retaining scope and the requested page size.
    Incremental, health, finality and exact-revision result selectors cannot prove
    completeness and are removed. Page, received-row (including replays), and
    serialized typed-page byte limits bound the walk. Transport adapters must
    also bound raw responses.
    """
    if (
        type(max_snapshot_restarts) is not int
        or max_snapshot_restarts < 0
        or type(max_pages) is not int
        or max_pages < 1
        or type(max_records) is not int
        or max_records < 1
        or type(max_bytes) is not int
        or max_bytes < 1
    ):
        raise ValueError("reporting walk bounds must be positive (restarts may be zero)")
    prepared = None
    try:
        base = request.model_dump(mode="json", exclude_none=True, warnings="error")
        base["view"] = "periods"
        base.pop("pagination", None)
        for selector in ("changes_after", "reporting_revision_id", "health", "finality"):
            base.pop(selector, None)
        page_size = request.pagination.max_results if request.pagination else None
        requested_account = request.account.model_dump(mode="json", warnings="error").get(
            "account_id"
        )
        prepared = (base, page_size, requested_account)
    except Exception:
        prepared = None
    if prepared is None:
        raise ReportingReconciliationError(
            "INVALID_STATUS_REQUEST", "reporting status request could not be constructed"
        )
    base, page_size, requested_account = prepared
    for restart in range(max_snapshot_restarts + 1):
        try:
            obligations: dict[str, ReportingObligation] = {}
            revisions: dict[str, ReportingRevision] = {}
            materializations: dict[str, ReportingMaterialization] = {}
            receipts: dict[str, ReportingReceipt] = {}
            consumer_statuses: dict[str, Any] = {}
            adjustments: dict[str, ReportingAdjustment] = {}
            adjustment_receipts: dict[str, ReportingAdjustmentReceipt] = {}
            ownership: dict[str, str] = {}
            ownership_mode: bool | None = None
            frozen_metadata: str | None = None
            cursor: str | None = None
            seen_cursors: set[str] = set()
            snapshot_id: str | None = None
            ledger_as_of: datetime | None = None
            account_id: str | None = None
            scope: BaseModel | None = None
            total_count: int | None = None
            received_records = 0
            received_bytes = 0

            for _page_number in range(max_pages):
                payload = dict(base)
                pagination_request: dict[str, object] = {}
                if page_size is not None:
                    pagination_request["max_results"] = page_size
                if cursor:
                    pagination_request["cursor"] = cursor
                if pagination_request:
                    payload["pagination"] = pagination_request
                page_request = None
                try:
                    page_request = GetReportingStatusRequest.model_validate(payload)
                except Exception:
                    page_request = None
                if page_request is None:
                    # SDK-side construction and provider failures are distinct,
                    # but neither may retain validation inputs or private context.
                    raise ReportingReconciliationError(
                        "INVALID_STATUS_REQUEST",
                        "reporting status request could not be constructed",
                    )
                result = None
                try:
                    result = await client.get_reporting_status(page_request)
                except Exception:
                    result = None
                if result is None:
                    # Leave the exception scope: do not retain a private provider
                    # exception or Pydantic input in __context__ either.
                    raise ReportingReconciliationError(
                        "STATUS_READ_FAILED", "get_reporting_status could not be read"
                    )
                response = result.data
                if (
                    not result.success
                    or response is None
                    or _enum(response.view) != "periods"
                    or _enum(response.status) != "completed"
                ):
                    raise ReportingReconciliationError(
                        "STATUS_READ_FAILED",
                        "get_reporting_status did not return a completed periods view",
                    )
                received_bytes += len(response.model_dump_json(exclude_unset=True).encode("utf-8"))
                received_records += sum(
                    len(records or [])
                    for records in (
                        response.periods,
                        response.revisions,
                        response.materializations,
                        response.receipts,
                        response.consumer_statuses,
                        response.adjustments,
                        response.adjustment_receipts,
                    )
                )
                if received_bytes > max_bytes or received_records > max_records:
                    raise ReportingReconciliationError(
                        "LEDGER_LIMIT_EXCEEDED", "ledger read budget exceeded"
                    )
                response = response.model_copy(deep=True)
                raw_page = response.model_dump(mode="json", exclude_none=True)
                if "ext" in response.model_fields_set and response.ext is None:
                    raw_page["ext"] = None
                try:
                    local = page_revision_ownership(raw_page)
                except ReportingOwnershipError:
                    raise ReportingReconciliationError(
                        "INVALID_REVISION_OWNERSHIP", "invalid page-local revision ownership"
                    ) from None
                mode = local is not None
                if ownership_mode is not None and mode != ownership_mode:
                    raise ReportingReconciliationError(
                        "INVALID_REVISION_OWNERSHIP", "mixed ownership modes within one snapshot"
                    )
                ownership_mode = mode
                for revision_id, owner_id in (local or {}).items():
                    if revision_id in ownership and ownership[revision_id] != owner_id:
                        raise ReportingReconciliationError(
                            "INVALID_REVISION_OWNERSHIP", "ownership changed within one snapshot"
                        )
                    ownership[revision_id] = owner_id
                metadata = _json(
                    {
                        k: raw_page.get(k)
                        for k in (
                            "changes_checkpoint",
                            "next_expected_at",
                            "health",
                            "issues",
                            "obligation_counts",
                            "coverage",
                            "data_through",
                        )
                    }
                )
                if frozen_metadata is not None and metadata != frozen_metadata:
                    raise ReportingReconciliationError(
                        "SNAPSHOT_CHANGED", "frozen projection changed"
                    )
                frozen_metadata = metadata
                pagination = response.pagination
                if (
                    not response.ledger_snapshot_id
                    or not response.ledger_as_of
                    or not response.account_id
                    or not response.scope
                    or pagination is None
                    or pagination.total_count is None
                    or type(pagination.has_more) is not bool
                ):
                    raise ReportingReconciliationError(
                        "INCOMPLETE_LEDGER_PAGE", "get_reporting_status omitted ledger metadata"
                    )
                if requested_account is not None and response.account_id != requested_account:
                    raise ReportingReconciliationError(
                        "LEDGER_SCOPE_MISMATCH", "ledger does not match the requested account"
                    )
                if snapshot_id and snapshot_id != response.ledger_snapshot_id:
                    raise ReportingReconciliationError("SNAPSHOT_CHANGED", "snapshot changed")
                if ledger_as_of and ledger_as_of != response.ledger_as_of:
                    raise ReportingReconciliationError(
                        "SNAPSHOT_CHANGED", "ledger boundary changed"
                    )
                if account_id and account_id != response.account_id:
                    raise ReportingReconciliationError("SNAPSHOT_CHANGED", "account changed")
                if scope and _json(scope) != _json(response.scope):
                    raise ReportingReconciliationError("SNAPSHOT_CHANGED", "denominator changed")
                if total_count is not None and total_count != pagination.total_count:
                    raise ReportingReconciliationError("SNAPSHOT_CHANGED", "record total changed")

                snapshot_id = response.ledger_snapshot_id
                ledger_as_of = response.ledger_as_of
                account_id = response.account_id
                scope = response.scope
                total_count = pagination.total_count
                if total_count > max_records:
                    raise ReportingReconciliationError(
                        "LEDGER_LIMIT_EXCEEDED", "ledger record limit exceeded"
                    )
                for obligation in response.periods or []:
                    _add_immutable(
                        obligations,
                        obligation.reporting_obligation_id,
                        obligation,
                        "obligation",
                    )
                for revision in response.revisions or []:
                    _add_immutable(revisions, revision.reporting_revision_id, revision, "revision")
                for materialization in response.materializations or []:
                    _add_immutable(
                        materializations,
                        materialization.reporting_materialization_id,
                        materialization,
                        "materialization",
                    )
                for receipt in response.receipts or []:
                    _add_immutable(receipts, receipt.reporting_receipt_id, receipt, "receipt")
                for adjustment in response.adjustments or []:
                    _add_immutable(
                        adjustments, adjustment.reporting_adjustment_id, adjustment, "adjustment"
                    )
                for adjustment_receipt in response.adjustment_receipts or []:
                    _add_immutable(
                        adjustment_receipts,
                        adjustment_receipt.reporting_receipt_id,
                        adjustment_receipt,
                        "adjustment receipt",
                    )
                for status in getattr(response, "consumer_statuses", None) or []:
                    _add_immutable(
                        consumer_statuses,
                        status.reporting_status_id,
                        status,
                        "consumer status",
                    )

                if (
                    sum(
                        len(records)
                        for records in (
                            obligations,
                            revisions,
                            materializations,
                            receipts,
                            consumer_statuses,
                            adjustments,
                            adjustment_receipts,
                        )
                    )
                    > max_records
                ):
                    raise ReportingReconciliationError(
                        "LEDGER_LIMIT_EXCEEDED", "ledger record limit exceeded"
                    )
                if not pagination.has_more:
                    break
                cursor = pagination.cursor
                if not cursor or len(cursor) > 2048 or cursor in seen_cursors:
                    raise ReportingReconciliationError(
                        "CURSOR_LOOP", "ledger pagination did not advance"
                    )
                seen_cursors.add(cursor)
            else:
                raise ReportingReconciliationError(
                    "LEDGER_LIMIT_EXCEEDED", "ledger page limit exceeded"
                )

            count = (
                len(obligations)
                + len(revisions)
                + len(materializations)
                + len(receipts)
                + len(consumer_statuses)
                + len(adjustments)
                + len(adjustment_receipts)
            )
            if total_count != count:
                raise ReportingReconciliationError(
                    "LEDGER_COUNT_MISMATCH",
                    f"ledger declared {total_count} records but returned {count}",
                )
            if not snapshot_id or not ledger_as_of or not account_id or not scope:
                raise ReportingReconciliationError(
                    "EMPTY_LEDGER_RESPONSE", "get_reporting_status returned no ledger page"
                )
            ledger = ReportingLedger(
                snapshot_id,
                ledger_as_of,
                account_id,
                scope,
                list(obligations.values()),
                list(revisions.values()),
                list(materializations.values()),
                list(receipts.values()),
                list(consumer_statuses.values()),
                ownership if ownership_mode else None,
                list(adjustments.values()),
                list(adjustment_receipts.values()),
            )
            _, partition_complete, _ = _validate_read_ledger(ledger)
            if partition_complete:
                ledger._read_fingerprint = _ledger_fingerprint(ledger)
            return ledger
        except ReportingReconciliationError as error:
            if error.code != "SNAPSHOT_CHANGED" or restart == max_snapshot_restarts:
                raise
    raise ReportingReconciliationError("SNAPSHOT_CHANGED", "ledger never stabilized")


def _validate_owned_ledger(ledger: ReportingLedger) -> None:
    """Validate explicit ownership only after all bounded pages are present."""
    owners = {o.reporting_obligation_id: o for o in ledger.obligations}
    revisions = {r.reporting_revision_id: r for r in ledger.revisions}
    bindings = ledger.revision_ownership

    def invalid() -> NoReturn:
        raise ReportingReconciliationError(
            "INVALID_REVISION_OWNERSHIP", "incomplete or inconsistent ownership dependencies"
        )

    if bindings is None or set(bindings) != set(revisions):
        invalid()
    if any(o.account_id != ledger.account_id for o in owners.values()):
        invalid()
    for revision_id, owner_id in bindings.items():
        if owner_id not in owners or not _revision_matches_obligation(
            revisions[revision_id], owners[owner_id]
        ):
            invalid()
        predecessor = revisions[revision_id].supersedes_reporting_revision_id
        if predecessor is not None and bindings.get(predecessor) != owner_id:
            invalid()
    counts = Counter(bindings.values())
    for owner in owners.values():
        if counts[owner.reporting_obligation_id] != owner.revision_count:
            invalid()
    materials = {m.reporting_materialization_id: m for m in ledger.materializations}
    owned_evidence: tuple[ReportingMaterialization | ReportingReceipt, ...] = (
        *ledger.materializations,
        *ledger.receipts,
    )
    for item in owned_evidence:
        if bindings.get(item.reporting_revision_id) != item.reporting_obligation_id:
            invalid()
    for receipt in ledger.receipts:
        material = materials.get(receipt.reporting_materialization_id)
        if material is None or (
            material.reporting_revision_id != receipt.reporting_revision_id
            or material.reporting_obligation_id != receipt.reporting_obligation_id
        ):
            invalid()
    adjustments = {a.reporting_adjustment_id: a for a in ledger.adjustments}
    for adjustment in adjustments.values():
        revision = revisions.get(adjustment.adjusts_reporting_revision_id)
        if revision is None or _enum(revision.finality) != "official":
            invalid()
    for adjustment_receipt in ledger.adjustment_receipts:
        target_adjustment = adjustments.get(adjustment_receipt.reporting_adjustment_id)
        if (
            target_adjustment is None
            or target_adjustment.adjusts_reporting_revision_id
            != adjustment_receipt.adjusts_reporting_revision_id
        ):
            invalid()


def _status_matches_obligation(status: Any, obligation: ReportingObligation) -> bool:
    # Logical compatibility alone is never an ownership proof: statuses have
    # no account, campaign-set or reporting-profile fields to disambiguate it.
    return bool(
        status.reporting_obligation_id in {None, obligation.reporting_obligation_id}
        and _status_scope(status) == _status_scope(obligation)
    )


_Receipt = ReportingReceipt | ReportingAdjustmentReceipt
_ReceiptTarget = tuple[str, str, str]


def _receipt_target(receipt: _Receipt) -> _ReceiptTarget:
    if isinstance(receipt, ReportingReceipt):
        return ("revision", receipt.reporting_obligation_id, receipt.reporting_revision_id)
    return ("adjustment", receipt.reporting_adjustment_id, receipt.adjusts_reporting_revision_id)


def _receipt_leaves(ledger: ReportingLedger) -> dict[_ReceiptTarget, _Receipt]:
    """Linear, order-independent validation of every exact receipt chain."""
    records: list[_Receipt] = [*ledger.receipts, *ledger.adjustment_receipts]
    by_id = {r.reporting_receipt_id: r for r in records}

    def invalid() -> NoReturn:
        raise ReportingReconciliationError(
            "INVALID_RECEIPT_CHAIN", "receipt history is not one exact predecessor chain"
        )

    if len(by_id) != len(records):
        invalid()
    roots: dict[_ReceiptTarget, list[str]] = {}
    counts: Counter[_ReceiptTarget] = Counter()
    successors: dict[str, str] = {}
    for receipt in records:
        key = _receipt_target(receipt)
        counts[key] += 1
        roots.setdefault(key, [])
        if (_enum(receipt.status) == "rejected") != bool(receipt.rejection_codes):
            invalid()
        predecessor_id = receipt.supersedes_reporting_receipt_id
        if predecessor_id is None:
            roots[key].append(receipt.reporting_receipt_id)
            continue
        predecessor = by_id.get(predecessor_id)
        if (
            predecessor is None
            or _receipt_target(predecessor) != key
            or _enum(predecessor.status) != "rejected"
            or predecessor_id in successors
        ):
            invalid()
        successors[predecessor_id] = receipt.reporting_receipt_id
    leaves: dict[_ReceiptTarget, _Receipt] = {}
    for key, chain_roots in roots.items():
        if len(chain_roots) != 1:
            invalid()
        identifier = chain_roots[0]
        visited: set[str] = set()
        while identifier not in visited:
            visited.add(identifier)
            successor = successors.get(identifier)
            if successor is None:
                break
            identifier = successor
        if len(visited) != counts[key] or identifier in successors:
            invalid()
        leaves[key] = by_id[identifier]
    return leaves


def _ordered_times(*values: datetime | None) -> bool:
    if any(v is None or v.tzinfo is None or v.utcoffset() is None for v in values):
        return False
    return all(a <= b for a, b in zip(values, values[1:]) if a is not None and b is not None)


def _status_scope(item: Any) -> tuple[str, int, str, str]:
    return (
        item.delivery_config_id,
        item.delivery_config_version,
        item.report_definition_id,
        _json(item.period),
    )


def _revision_scope(
    item: ReportingRevision | ReportingObligation,
) -> tuple[str, str, str, tuple[str, ...], str]:
    return (
        item.account_id,
        item.report_definition_id,
        item.reporting_profile,
        _identifiers(item.media_buy_ids),
        _json(item.period),
    )


@dataclass
class _ObligationHistory:
    revisions: list[ReportingRevision] = field(default_factory=list)
    materializations: list[ReportingMaterialization] = field(default_factory=list)
    receipts: list[ReportingReceipt] = field(default_factory=list)
    adjustments: list[ReportingAdjustment] = field(default_factory=list)
    adjustment_receipts: list[ReportingAdjustmentReceipt] = field(default_factory=list)
    statuses: list[Any] = field(default_factory=list)
    ambiguous_revisions: bool = False
    unresolved_status_count: int = 0


@dataclass
class _ReadIndex:
    owners: dict[str, ReportingObligation]
    revisions: dict[str, ReportingRevision]
    materials: dict[str, ReportingMaterialization]
    adjustments: dict[str, ReportingAdjustment]
    histories: dict[str, _ObligationHistory]
    adjustments_by_revision: dict[str, list[ReportingAdjustment]]
    unresolved_statuses: bool
    selections: dict[
        str, tuple[ReportingRevision | None, ReportingMaterialization | None, list[str]]
    ] = field(default_factory=dict)


def _index_read_ledger(ledger: ReportingLedger) -> _ReadIndex:
    """Index the complete read, checking identity before partitioning evidence.

    Indexes are private to this validation, never cached on mutable ledgers.
    Legacy ambiguous associations get a separate work bound so sharing one
    semantic scope cannot expand a bounded record set into quadratic work.
    """
    if ledger.revision_ownership is not None:
        _validate_owned_ledger(ledger)
    owners = {o.reporting_obligation_id: o for o in ledger.obligations}
    revisions = {r.reporting_revision_id: r for r in ledger.revisions}
    materials = {m.reporting_materialization_id: m for m in ledger.materializations}
    adjustments = {a.reporting_adjustment_id: a for a in ledger.adjustments}
    statuses = {s.reporting_status_id: s for s in ledger.consumer_statuses}

    def invalid() -> NoReturn:
        raise ReportingReconciliationError(
            "INVALID_LEDGER_DEPENDENCY", "ledger dependencies are incomplete or inconsistent"
        )

    if any(
        len(index) != len(records)
        for index, records in (
            (owners, ledger.obligations),
            (revisions, ledger.revisions),
            (materials, ledger.materializations),
            (adjustments, ledger.adjustments),
            (statuses, ledger.consumer_statuses),
        )
    ):
        invalid()
    if any(o.account_id != ledger.account_id for o in owners.values()) or any(
        r.account_id != ledger.account_id for r in revisions.values()
    ):
        invalid()
    histories = {owner_id: _ObligationHistory() for owner_id in owners}
    revision_owners = dict(ledger.revision_ownership or {})
    for material in materials.values():
        owner = owners.get(material.reporting_obligation_id)
        revision = revisions.get(material.reporting_revision_id)
        if (
            owner is None
            or revision is None
            or not _revision_matches_obligation(revision, owner)
            or material.delivery_config_id != owner.delivery_config_id
            or material.delivery_config_version != owner.delivery_config_version
            or material.destination_ref != owner.destination_ref
            or material.feed_purpose != owner.feed_purpose
            or revision_owners.setdefault(
                material.reporting_revision_id, material.reporting_obligation_id
            )
            != material.reporting_obligation_id
        ):
            invalid()
        histories[owner.reporting_obligation_id].materializations.append(material)
    owners_by_scope: dict[tuple[str, str, str, tuple[str, ...], str], list[str]] = defaultdict(list)
    for owner_id, owner in owners.items():
        owners_by_scope[_revision_scope(owner)].append(owner_id)
    association_count = 0
    association_limit = max(
        200_000,
        sum(
            len(records)
            for records in (
                ledger.obligations,
                ledger.revisions,
                ledger.materializations,
                ledger.receipts,
                ledger.adjustments,
                ledger.adjustment_receipts,
                ledger.consumer_statuses,
            )
        ),
    )

    def account_associations(count: int) -> None:
        nonlocal association_count
        association_count += count
        if association_count > association_limit:
            raise ReportingReconciliationError(
                "LEDGER_LIMIT_EXCEEDED", "legacy history association budget exceeded"
            )

    for revision_id, revision in revisions.items():
        predecessor = revision.supersedes_reporting_revision_id
        if predecessor is not None and predecessor not in revisions:
            invalid()
        exact_owner = revision_owners.get(revision_id)
        matching = (
            [exact_owner]
            if exact_owner is not None
            else owners_by_scope.get(_revision_scope(revision), [])
        )
        if not matching:
            invalid()
        account_associations(len(matching))
        for owner_id in matching:
            histories[owner_id].revisions.append(revision)
            histories[owner_id].ambiguous_revisions |= len(matching) > 1
    for receipt in ledger.receipts:
        owner = owners.get(receipt.reporting_obligation_id)
        revision = revisions.get(receipt.reporting_revision_id)
        referenced_material = materials.get(receipt.reporting_materialization_id)
        if (
            owner is None
            or revision is None
            or referenced_material is None
            or referenced_material.reporting_revision_id != receipt.reporting_revision_id
            or referenced_material.reporting_obligation_id != receipt.reporting_obligation_id
            or not _revision_matches_obligation(revision, owner)
            or _enum(owner.reconciliation_mode) != "consumer_receipt"
        ):
            invalid()
        histories[owner.reporting_obligation_id].receipts.append(receipt)
    adjustments_by_revision: dict[str, list[ReportingAdjustment]] = defaultdict(list)
    receipts_by_adjustment: dict[str, list[ReportingAdjustmentReceipt]] = defaultdict(list)
    for adjustment in adjustments.values():
        if adjustment.adjusts_reporting_revision_id not in revisions:
            invalid()
        adjustments_by_revision[adjustment.adjusts_reporting_revision_id].append(adjustment)
    for adjustment_receipt in ledger.adjustment_receipts:
        target = adjustments.get(adjustment_receipt.reporting_adjustment_id)
        if target is None or target.adjusts_reporting_revision_id != (
            adjustment_receipt.adjusts_reporting_revision_id
        ):
            invalid()
        receipts_by_adjustment[target.reporting_adjustment_id].append(adjustment_receipt)
    for history in histories.values():
        for revision in history.revisions:
            related = adjustments_by_revision.get(revision.reporting_revision_id, [])
            account_associations(len(related))
            history.adjustments.extend(related)
        for adjustment in history.adjustments:
            related_receipts = receipts_by_adjustment.get(adjustment.reporting_adjustment_id, [])
            account_associations(len(related_receipts))
            history.adjustment_receipts.extend(related_receipts)

    status_owners: dict[str, str | None] = {}
    unresolved_by_scope: Counter[tuple[str, int, str, str]] = Counter()
    for status in statuses.values():
        status_revision_id = status.reporting_revision_id
        status_owner: str | None = status.reporting_obligation_id
        if status_owner is None and status_revision_id is not None:
            status_owner = revision_owners.get(status_revision_id)
        if status_owner is None:
            # Neither a logical-key match nor a seller's projected count can
            # invent missing immutable ownership. Keep these rows diagnostic.
            unresolved_by_scope[_status_scope(status)] += 1
        elif status_owner not in owners or not _status_matches_obligation(
            status, owners[status_owner]
        ):
            invalid()
        if status_revision_id is not None and (
            status_revision_id not in revisions
            or (
                status_owner is not None
                and (
                    not _revision_matches_obligation(
                        revisions[status_revision_id], owners[status_owner]
                    )
                    or revision_owners.get(status_revision_id, status_owner) != status_owner
                )
            )
        ):
            invalid()
        status_owners[status.reporting_status_id] = status_owner
        if status_owner is not None:
            histories[status_owner].statuses.append(status)
    for status in statuses.values():
        predecessor_id = status.supersedes_reporting_status_id
        if predecessor_id is None:
            continue
        predecessor = statuses.get(predecessor_id)
        if predecessor is None or _status_scope(status) != _status_scope(predecessor):
            invalid()
        status_owner = status_owners[status.reporting_status_id]
        predecessor_owner = status_owners[predecessor_id]
        if (
            status_owner is not None
            and predecessor_owner is not None
            and status_owner != predecessor_owner
        ):
            invalid()
    for owner_id, owner in owners.items():
        histories[owner_id].unresolved_status_count = unresolved_by_scope[_status_scope(owner)]
        current_id = owner.current_consumer_status_id
        if current_id is not None:
            current = statuses.get(current_id)
            if (
                current is None
                or not _status_matches_obligation(current, owner)
                or status_owners[current_id] not in {None, owner_id}
                or owner.consumer_status_count == 0
            ):
                invalid()
        elif owner.consumer_status_count:
            invalid()
    return _ReadIndex(
        owners,
        revisions,
        materials,
        adjustments,
        histories,
        adjustments_by_revision,
        bool(unresolved_by_scope),
    )


def _validate_read_ledger(
    ledger: ReportingLedger,
) -> tuple[dict[_ReceiptTarget, _Receipt], bool, _ReadIndex]:
    """Check the full frozen denominator and dependencies before using evidence."""
    index = _index_read_ledger(ledger)
    owners, revisions = index.owners, index.revisions
    materials, adjustments = index.materials, index.adjustments
    partition_complete = not index.unresolved_statuses

    def invalid() -> NoReturn:
        raise ReportingReconciliationError(
            "INVALID_LEDGER_DEPENDENCY", "ledger dependencies are incomplete or inconsistent"
        )

    for owner_id, owner in owners.items():
        selection = _select_current(owner, ledger, index)
        index.selections[owner_id] = selection
        if any(
            reason in selection[2]
            for reason in ("ASSOCIATED_HISTORY_INCOMPLETE", "AMBIGUOUS_REVISION_OWNERSHIP")
        ):
            partition_complete = False
    for adjustment in adjustments.values():
        revision = revisions.get(adjustment.adjusts_reporting_revision_id)
        if (
            revision is None
            or _enum(revision.finality) != "official"
            or not revision.finality_basis
            or not revision.finality_policy_id
            or not _ordered_times(revision.period.end, revision.finalized_at, revision.created_at)
            or not _ordered_times(
                revision.finalized_at,
                adjustment.correction_observed_at,
                adjustment.created_at,
                ledger.ledger_as_of,
            )
            or not _ordered_times(
                adjustment.accounting_period.start, adjustment.accounting_period.end
            )
            or adjustment.accounting_period.start == adjustment.accounting_period.end
        ):
            invalid()
    for receipt_adjustment in ledger.adjustment_receipts:
        target = adjustments.get(receipt_adjustment.reporting_adjustment_id)
        if (
            target is None
            or target.adjusts_reporting_revision_id
            != receipt_adjustment.adjusts_reporting_revision_id
            or not _ordered_times(
                target.created_at, receipt_adjustment.observed_at, ledger.ledger_as_of
            )
            or (
                receipt_adjustment.received_at is not None
                and not _ordered_times(
                    receipt_adjustment.observed_at,
                    receipt_adjustment.received_at,
                    ledger.ledger_as_of,
                )
            )
        ):
            invalid()
        if _enum(receipt_adjustment.status) == "accepted" and (
            not target.canonical_adjustment_sha256
            or target.canonical_adjustment_sha256 != receipt_adjustment.observed_adjustment_sha256
        ):
            raise ReportingReconciliationError(
                "INVALID_RECEIPT_EVIDENCE", "accepted adjustment evidence does not match its target"
            )
    leaves = _receipt_leaves(ledger)
    for owner in owners.values():
        if owner.pending_adjustment_count is None:
            continue
        selected, _, _ = index.selections[owner.reporting_obligation_id]
        if selected is None:
            continue
        pending = sum(
            (
                leaf := leaves.get(
                    ("adjustment", a.reporting_adjustment_id, selected.reporting_revision_id)
                )
            )
            is None
            or _enum(leaf.status) != "accepted"
            for a in index.adjustments_by_revision.get(selected.reporting_revision_id, [])
        )
        if owner.pending_adjustment_count != pending:
            raise ReportingReconciliationError(
                "LEDGER_COUNT_MISMATCH", "frozen adjustment counts do not match current leaves"
            )
    for receipt in ledger.receipts:
        if _enum(receipt.status) != "accepted":
            continue
        owner = owners[receipt.reporting_obligation_id]
        revision = revisions[receipt.reporting_revision_id]
        material = materials[receipt.reporting_materialization_id]
        if _materialization_reasons(owner, revision, material) or not _receipt_matches(
            receipt, revision, material
        ):
            raise ReportingReconciliationError(
                "INVALID_RECEIPT_EVIDENCE", "accepted receipt evidence does not match its target"
            )
    return leaves, partition_complete, index


def _owned_revisions(
    obligation: ReportingObligation, ledger: ReportingLedger
) -> list[ReportingRevision]:
    if ledger.revision_ownership is not None:
        return [
            r
            for r in ledger.revisions
            if ledger.revision_ownership.get(r.reporting_revision_id)
            == obligation.reporting_obligation_id
        ]
    return [r for r in ledger.revisions if _revision_matches_obligation(r, obligation)]


def _obligation_history(
    obligation: ReportingObligation, index: _ReadIndex
) -> tuple[list[ReportingRevision], list[ReportingMaterialization], list[str]]:
    history = index.histories[obligation.reporting_obligation_id]
    reasons: list[str] = []
    incomplete = False
    if history.ambiguous_revisions:
        reasons.append("AMBIGUOUS_REVISION_OWNERSHIP")
    managed = obligation.destination_ref is not None
    reconciled = _enum(obligation.reconciliation_mode) == "consumer_receipt"
    # Optional fields remain readable in legacy shapes. Their absence prevents
    # proof where applicable; only a present, provably false count is an error.
    # Applicability never depends on the unrelated revision-ownership extension.
    counts = (
        (obligation.revision_count, len(history.revisions), True, history.ambiguous_revisions),
        (obligation.materialization_count, len(history.materializations), managed, False),
        (
            obligation.successful_materialization_count,
            sum(_enum(m.status) in {"available", "delivered"} for m in history.materializations),
            managed,
            False,
        ),
        (obligation.receipt_count, len(history.receipts), reconciled, False),
        (
            obligation.accepted_receipt_count,
            sum(_enum(r.status) == "accepted" for r in history.receipts),
            reconciled,
            False,
        ),
        # Reliable Reporting requires this count; its optional schema shape also
        # admits older sellers, which remain diagnostic until evidence is complete.
        (obligation.adjustment_count, len(history.adjustments), True, history.ambiguous_revisions),
        (
            obligation.adjustment_receipt_count,
            len(history.adjustment_receipts),
            reconciled,
            history.ambiguous_revisions,
        ),
        (
            obligation.accepted_adjustment_receipt_count,
            sum(_enum(r.status) == "accepted" for r in history.adjustment_receipts),
            reconciled,
            history.ambiguous_revisions,
        ),
    )
    for declared, actual, required, ambiguous in counts:
        if declared is None:
            incomplete |= required
        elif declared != actual:
            if ambiguous:
                incomplete = True
            else:
                raise ReportingReconciliationError(
                    "LEDGER_COUNT_MISMATCH", "frozen obligation counts do not match the ledger"
                )
    known = len(history.statuses)
    declared = obligation.consumer_status_count
    if declared is not None and not known <= declared <= known + history.unresolved_status_count:
        raise ReportingReconciliationError(
            "LEDGER_COUNT_MISMATCH", "frozen consumer status counts do not match the ledger"
        )
    # A disabled status projection does not require an optional count. A current
    # status reference without its count cannot prove the projected history.
    incomplete |= declared is None and obligation.current_consumer_status_id is not None
    if history.unresolved_status_count:
        reasons.append("AMBIGUOUS_CONSUMER_STATUS_OWNERSHIP")
    if incomplete:
        reasons.append("ASSOCIATED_HISTORY_INCOMPLETE")
    return history.revisions, history.materializations, reasons


def _select_current(
    obligation: ReportingObligation, ledger: ReportingLedger, index: _ReadIndex
) -> tuple[ReportingRevision | None, ReportingMaterialization | None, list[str]]:
    candidates, attempts, reasons = _obligation_history(obligation, index)
    selection = select_reporting_revision(
        tuple(
            RevisionHistoryEntry(
                ledger.account_id,
                obligation.reporting_obligation_id,
                item.reporting_revision_id,
                _enum(item.finality),
                item.supersedes_reporting_revision_id,
            )
            for item in candidates
        ),
        account_id=ledger.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        required_finality=_enum(obligation.required_finality),
    )
    if selection.kind == "corrupt":
        reasons.append(
            "AMBIGUOUS_REVISION_CHAIN"
            if selection.reason
            in {
                "multiple_officials",
                "forked_snapshot_history",
                "disconnected_snapshot_history",
                "duplicate_revision_id",
            }
            else "INCOMPLETE_REVISION_CHAIN"
        )
        return None, None, reasons
    if selection.kind == "not_ready":
        reasons.append(
            "FINALITY_NOT_MET"
            if selection.reason == "official_required"
            else "MISSING_CURRENT_REVISION"
        )
        return None, None, reasons
    revision = next(
        r for r in candidates if r.reporting_revision_id == selection.revision.reporting_revision_id
    )
    if (
        not _coverage_is_full(obligation.coverage, obligation.media_buy_ids)
        or obligation.coverage.evaluated_at != obligation.scope_resolved_at
    ):
        reasons.append("REPORTING_COVERAGE_INCOMPLETE")
    if _json(revision.coverage) != _json(obligation.coverage):
        reasons.append("REVISION_COVERAGE_MISMATCH")
    if _enum(obligation.required_finality) == "official" and _enum(revision.finality) != "official":
        reasons.append("FINALITY_NOT_MET")
    finality_basis = revision.finality_basis
    finality_policy_id = revision.finality_policy_id
    finalized_at = revision.finalized_at
    if _enum(revision.finality) == "official":
        if finality_basis is None or finality_policy_id is None or finalized_at is None:
            reasons.append("FINALITY_EVIDENCE_MISSING")
        elif not (obligation.period.end <= finalized_at <= revision.created_at):
            reasons.append("FINALITY_EVIDENCE_INVALID")
    elif finality_basis is not None or finality_policy_id is not None or finalized_at is not None:
        reasons.append("FINALITY_EVIDENCE_INVALID")

    if obligation.destination_ref is None:
        if _enum(obligation.reconciliation_mode) == "consumer_receipt":
            reasons.append("INVALID_RECONCILIATION_TIER")
        return revision, None, reasons

    successful = sorted(
        (
            item
            for item in attempts
            if item.reporting_revision_id == revision.reporting_revision_id
            and _enum(item.status) in {"available", "delivered"}
        ),
        key=lambda item: item.attempt,
        reverse=True,
    )
    materialization = successful[0] if successful else None
    reasons.extend(_materialization_reasons(obligation, revision, materialization))
    return revision, materialization, reasons


def _materialization_reasons(
    obligation: ReportingObligation,
    revision: ReportingRevision,
    materialization: ReportingMaterialization | None,
) -> list[str]:
    """Immutable producer evidence; current readability/expiry is independent."""
    reasons: list[str] = []
    if (
        not materialization
        or _enum(materialization.status) not in {"available", "delivered"}
        or not materialization.ready_at
        or not materialization.verification
        or not materialization.resource
    ):
        reasons.append("MISSING_VERIFIED_MATERIALIZATION")
        return reasons
    if (
        materialization.reporting_obligation_id != obligation.reporting_obligation_id
        or materialization.reporting_revision_id != revision.reporting_revision_id
        or materialization.delivery_config_id != obligation.delivery_config_id
        or materialization.delivery_config_version != obligation.delivery_config_version
        or materialization.destination_ref != obligation.destination_ref
        or _enum(materialization.feed_purpose) != _enum(obligation.feed_purpose)
    ):
        reasons.append("MATERIALIZATION_SCOPE_MISMATCH")
    if materialization.verification.row_count != revision.row_count or _totals(
        materialization.verification.control_totals
    ) != _totals(revision.control_totals):
        reasons.append("PRODUCER_CONTROL_TOTAL_MISMATCH")
    method = _enum(materialization.method)
    resource_kind = _enum(materialization.resource.kind)
    verification_path = _enum(materialization.verification.verification_path)
    method_evidence_valid = (
        (
            method == "file_transfer"
            and resource_kind == "manifest"
            and bool(materialization.verification.physical_checksums)
        )
        or (
            method == "dataset_share"
            and resource_kind == "dataset"
            and verification_path == "representative_consumer"
        )
        or (
            method == "warehouse_materialization"
            and resource_kind == "warehouse_relation"
            and verification_path == "destination"
        )
    )
    if not method_evidence_valid:
        reasons.append("MATERIALIZATION_METHOD_EVIDENCE_MISMATCH")
    if (
        _enum(materialization.feed_purpose) == "billing"
        and _enum(materialization.verification.verification_profile) != "canonical_digest"
    ):
        reasons.append("BILLING_VERIFICATION_PROFILE_MISMATCH")
    if _enum(materialization.verification.verification_profile) == "canonical_digest" and (
        not revision.canonical_content_digest
        or _json(materialization.verification.canonical_content_digest)
        != _json(revision.canonical_content_digest)
    ):
        reasons.append("PRODUCER_DIGEST_MISMATCH")
    if _enum(materialization.verification.verification_profile) == "native_commit":
        evidence = materialization.verification.native_commit_evidence
        if (
            not evidence
            # A matching reference and path prove nothing unless the retained
            # descriptor itself declares the resource immutable by native version.
            or _enum(materialization.resource.immutability) != "native_version"
            or not materialization.resource.native_version_ref
            or evidence.native_version_ref != materialization.resource.native_version_ref
            or _enum(evidence.observed_through)
            != _enum(materialization.verification.verification_path)
        ):
            reasons.append("PRODUCER_NATIVE_EVIDENCE_MISMATCH")
    if _enum(materialization.verification.verification_profile) == "manifest_checksums" and (
        _enum(materialization.resource.kind) != "manifest"
        or materialization.resource.manifest_version != "1.0"
        or not materialization.resource.manifest_sha256
        or not materialization.verification.physical_checksums
    ):
        reasons.append("PRODUCER_MANIFEST_EVIDENCE_MISSING")
    return reasons


def _receipt_matches(
    receipt: ReportingReceipt,
    revision: ReportingRevision,
    materialization: ReportingMaterialization,
) -> bool:
    verification = materialization.verification
    resource = materialization.resource
    if not verification or not resource or _enum(receipt.status) != "accepted":
        return False
    if (
        receipt.reporting_obligation_id != materialization.reporting_obligation_id
        or receipt.reporting_revision_id != revision.reporting_revision_id
        or receipt.reporting_materialization_id != materialization.reporting_materialization_id
        or _enum(receipt.verification_profile) != _enum(verification.verification_profile)
        or receipt.observed_row_count != revision.row_count
        or _totals(receipt.observed_control_totals) != _totals(revision.control_totals)
    ):
        return False
    profile = _enum(receipt.verification_profile)
    if profile == "canonical_digest":
        return bool(
            revision.canonical_content_digest
            and receipt.observed_canonical_content_digest
            and _json(receipt.observed_canonical_content_digest)
            == _json(revision.canonical_content_digest)
        )
    if profile == "manifest_checksums":
        return bool(
            resource.manifest_sha256
            and receipt.observed_manifest_sha256 == resource.manifest_sha256
        )
    return bool(
        resource.native_version_ref
        and getattr(receipt, "observed_native_version_ref", None) == resource.native_version_ref
    )


def _receipt_targets(
    receipt: ReportingReceipt,
    obligation: ReportingObligation,
    revision: ReportingRevision,
    materialization: ReportingMaterialization,
) -> bool:
    verification = materialization.verification
    return bool(
        verification
        and receipt.reporting_obligation_id == obligation.reporting_obligation_id
        and receipt.reporting_revision_id == revision.reporting_revision_id
        and receipt.reporting_materialization_id == materialization.reporting_materialization_id
        and _enum(receipt.verification_profile) == _enum(verification.verification_profile)
    )


def build_reporting_receipt(
    context: ReportingInspectionContext,
    observation: ReportingObservation,
    *,
    reporting_receipt_id: str | None = None,
    observed_at: datetime | None = None,
) -> ReportingReceipt:
    materialization = context.materialization
    revision = context.revision
    if not materialization.verification or not materialization.resource:
        raise ReportingReconciliationError(
            "MATERIALIZATION_NOT_READY", "cannot receipt an unverified materialization"
        )
    failures: list[str] = []
    if observation.row_count != revision.row_count:
        failures.append("ROW_COUNT_MISMATCH")
    if _totals(observation.control_totals) != _totals(revision.control_totals):
        failures.append("CONTROL_TOTAL_MISMATCH")
    profile = _enum(materialization.verification.verification_profile)
    if profile == "canonical_digest" and (
        not revision.canonical_content_digest
        or _json(observation.canonical_content_digest) != _json(revision.canonical_content_digest)
    ):
        failures.append("CANONICAL_DIGEST_MISMATCH")
    if (
        profile == "manifest_checksums"
        and observation.manifest_sha256 != materialization.resource.manifest_sha256
    ):
        failures.append("MANIFEST_DIGEST_MISMATCH")
    if (
        profile == "native_commit"
        and observation.native_version_ref != materialization.resource.native_version_ref
    ):
        failures.append("NATIVE_VERSION_MISMATCH")
    payload: dict[str, object] = {
        "reporting_receipt_id": reporting_receipt_id or f"reporting-receipt:{uuid4()}",
        "reporting_obligation_id": context.obligation.reporting_obligation_id,
        "reporting_revision_id": revision.reporting_revision_id,
        "reporting_materialization_id": materialization.reporting_materialization_id,
        "status": "rejected" if failures else "accepted",
        "verification_profile": profile,
        "observed_row_count": observation.row_count,
        "observed_control_totals": observation.control_totals,
        "observed_at": observed_at or datetime.now(timezone.utc),
    }
    if observation.canonical_content_digest:
        payload["observed_canonical_content_digest"] = observation.canonical_content_digest
    if observation.manifest_sha256:
        payload["observed_manifest_sha256"] = observation.manifest_sha256
    if observation.native_version_ref:
        payload["observed_native_version_ref"] = observation.native_version_ref
    if observation.consumer_commit_ref:
        payload["consumer_commit_ref"] = observation.consumer_commit_ref
    if failures:
        payload["rejection_codes"] = failures
    return ReportingReceipt.model_validate(payload)


def _expected_in_scope(
    expected: ExpectedReportingPeriod,
    scope: BaseModel,
    generations: set[tuple[str, int, str]],
    media_buy_ids: set[str],
) -> bool:
    """Missing obligations can only be diagnosed inside the retained denominator."""
    start = datetime.fromisoformat(expected.period_start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(expected.period_end.replace("Z", "+00:00"))
    return bool(
        getattr(scope, "coverage_complete", False)
        and _ordered_times(
            getattr(scope, "period_start", None), start, end, getattr(scope, "period_end", None)
        )
        and _ordered_times(getattr(scope, "ledger_retained_from", None), start)
        and start < end
        and (
            expected.delivery_config_id,
            expected.delivery_config_version,
            expected.feed_purpose,
        )
        in generations
        and (
            getattr(scope, "all_accessible_media_buys", False)
            or set(expected.media_buy_ids) <= media_buy_ids
        )
    )


def evaluate_reporting_ledger(
    ledger: ReportingLedger,
    *,
    expected_periods: list[ExpectedReportingPeriod] | None = None,
    now: datetime | None = None,
) -> ReportingReconciliationResult:
    """Evaluate exact retained evidence without performing reads or writes.

    Definitive and missing-period claims require an unchanged full snapshot from
    :func:`load_reporting_ledger`. Manually assembled ledgers are diagnostic only;
    this API does not certify a caller's incremental merge or recompute raw
    adjustment digests from normalized models.
    """
    complete_read = ledger._read_fingerprint is not None
    if complete_read and ledger._read_fingerprint != _ledger_fingerprint(ledger):
        raise ReportingReconciliationError(
            "LEDGER_CHANGED", "the completed ledger was changed after loading"
        )
    leaves, partition_complete, index = _validate_read_ledger(ledger)
    complete_read = complete_read and partition_complete
    now = now or datetime.now(timezone.utc)
    outcomes: list[ObligationReconciliation] = []
    unique_revisions: dict[str, ReportingRevision] = {}
    for obligation in ledger.obligations:
        revision, materialization, selected_reasons = index.selections[
            obligation.reporting_obligation_id
        ]
        reasons = list(selected_reasons)
        if not complete_read:
            reasons.append("UNVERIFIED_LEDGER_SNAPSHOT")
        if _enum(obligation.health) != "complete":
            reasons.append(f"OBLIGATION_{_enum(obligation.health).upper()}")
        if (
            materialization
            and materialization.resource
            and materialization.resource.expires_at <= now
        ):
            reasons.append("RESOURCE_EXPIRED")
        if revision:
            unique_revisions[revision.reporting_revision_id] = revision
        if _enum(obligation.reconciliation_mode) == "consumer_receipt" and revision:
            if _enum(revision.finality) != "official" and "FINALITY_NOT_MET" not in reasons:
                reasons.append("FINALITY_NOT_MET")
            receipt = leaves.get(
                ("revision", obligation.reporting_obligation_id, revision.reporting_revision_id)
            )
            # Validation binds this leaf to the materialization it names, not
            # the newest artifact selected independently for current readability.
            if receipt is None or _enum(receipt.status) != "accepted":
                reasons.append("MISSING_MATCHING_CONSUMER_RECEIPT")
            applicable = index.adjustments_by_revision.get(revision.reporting_revision_id, [])
            if any(
                (
                    leaf := leaves.get(
                        ("adjustment", a.reporting_adjustment_id, revision.reporting_revision_id)
                    )
                )
                is None
                or _enum(leaf.status) != "accepted"
                for a in applicable
            ):
                reasons.append("MISSING_MATCHING_ADJUSTMENT_RECEIPT")
        outcomes.append(
            ObligationReconciliation(
                obligation.reporting_obligation_id,
                not reasons,
                revision.reporting_revision_id if revision else None,
                materialization.reporting_materialization_id if materialization else None,
                tuple(reasons),
            )
        )

    actual = {
        (
            item.delivery_config_id,
            item.delivery_config_version,
            item.report_definition_id,
            _enum(item.feed_purpose),
            item.reporting_profile,
            _identifiers(item.media_buy_ids),
            item.period.start.isoformat(),
            item.period.end.isoformat(),
            item.period.source_timezone,
        )
        for item in ledger.obligations
    }
    generations = {
        (g.delivery_config_id, g.delivery_config_version, _enum(g.feed_purpose))
        for g in getattr(ledger.scope, "delivery_config_generations", [])
    }
    media_buy_ids = set(_identifiers(getattr(ledger.scope, "media_buy_ids", [])))
    expectations = [
        (
            item,
            _expected_in_scope(item, ledger.scope, generations, media_buy_ids),
            (
                item.delivery_config_id,
                item.delivery_config_version,
                item.report_definition_id,
                item.feed_purpose,
                item.reporting_profile,
                tuple(sorted(item.media_buy_ids)),
                _iso(item.period_start),
                _iso(item.period_end),
                item.source_timezone,
            )
            in actual,
        )
        for item in expected_periods or []
    ]
    # The producer describes configuration requirements here, not current
    # revision finality: [official] can be a complete unfiltered denominator.
    # ExpectedReportingPeriod has no trusted finality fact, however, so for an
    # *absent* obligation we cannot prove membership in a proper subset. This
    # intentionally withholds some valid absence claims; it is not evidence of
    # seller filtering. Positive returned obligations retain their exact proof.
    absence_finality_proven = {_enum(value) for value in getattr(ledger.scope, "finality", [])} == {
        "snapshot",
        "official",
    }
    missing = [
        item
        for item, in_scope, present in expectations
        if complete_read and absence_finality_proven and in_scope and not present
    ]
    definitive = bool(
        complete_read
        and expected_periods is not None
        and all(in_scope and present for _, in_scope, present in expectations)
        and bool(getattr(ledger.scope, "scope_closed", False))
        and bool(getattr(ledger.scope, "coverage_complete", False))
        and all(item.definitive for item in outcomes)
    )
    return ReportingReconciliationResult(
        definitive,
        ledger,
        outcomes,
        missing,
        totals_by_revision=[
            (item.reporting_revision_id, item.row_count, item.control_totals)
            for item in unique_revisions.values()
        ],
    )


async def reconcile_reporting_core(
    client: ReportingStatusClient,
    request: GetReportingStatusRequest,
    *,
    expected_periods: list[ExpectedReportingPeriod],
    max_snapshot_restarts: int = 2,
    now: datetime | None = None,
    automated_recovery_window: timedelta | None = None,
    readings: dict[str, ReportingContentReading] | None = None,
    pinned_definition: ReportingPinnedDefinition | None = None,
    capabilities: dict[str, Any] | None = None,
) -> ReportingReconciliationResult:
    """Reconcile the Core API-delivered tier without destination handling.

    A Core caller only compares obligations, revisions, coverage, finality, and
    the reporting clock.  Destination materializations, manifests, digests,
    and consumer receipts are deliberately rejected rather than accidentally
    activating a higher tier.

    When ``automated_recovery_window`` is supplied the result also carries the
    AdCP 3.2.0-rc.3 buyer-side loop: the seller's
    ``obligation_counts.consumer_status_pending``, the issue lifecycle fields,
    ``operations_contact``, and a plan of the statements this buyer owes *by
    its deadline* rather than by scope close.  It is opt-in because posting
    status is only a duty when the seller advertises ``consumer_status_task``
    -- inferring a reverse endpoint from a seller that never offered one is the
    failure the opt-in exists to prevent.
    """
    ledger = await load_reporting_ledger(
        client, request, max_snapshot_restarts=max_snapshot_restarts
    )
    if any(obligation.destination_ref is not None for obligation in ledger.obligations):
        raise ReportingReconciliationError(
            "MANAGED_DELIVERY_NOT_ENABLED",
            "Core reconciliation received a managed-delivery obligation",
        )
    if ledger.materializations or ledger.receipts:
        raise ReportingReconciliationError(
            "MANAGED_DELIVERY_NOT_ENABLED",
            "Core reconciliation received destination or receipt records",
        )
    if any(
        _enum(obligation.reconciliation_mode) == "consumer_receipt"
        for obligation in ledger.obligations
    ):
        raise ReportingReconciliationError(
            "RECONCILED_BILLING_NOT_ENABLED",
            "Core reconciliation received a consumer-receipt obligation",
        )
    result = evaluate_reporting_ledger(ledger, expected_periods=expected_periods, now=now)
    if automated_recovery_window is not None:
        result.consumer_loop = await load_consumer_loop_view(
            client, request, capabilities=capabilities
        )
        result.consumer_status_plan = plan_consumer_statuses(
            ledger.obligations,
            now=now or ledger.ledger_as_of,
            automated_recovery_window=automated_recovery_window,
            # The periods the *buyer's* denominator expects and the seller's
            # ledger omitted. Dropping these was the whole reason
            # ``obligation_missing`` could never be emitted -- and it is the
            # one status the seller cannot derive for itself.
            missing_expected_periods=result.missing_expected_periods,
            readings=readings,
            definition=pinned_definition,
            revisions={item.reporting_revision_id: item for item in ledger.revisions},
            obligation_revisions={
                obligation.reporting_obligation_id: _owned_revisions(obligation, ledger)
                for obligation in ledger.obligations
            },
            current_statuses=ledger.consumer_statuses,
            account_id=ledger.account_id,
        )
    return result


async def reconcile_reporting(
    client: ReportingReconciliationClient,
    request: GetReportingStatusRequest,
    inspect: Callable[[ReportingInspectionContext], Awaitable[ReportingObservation]] | None = None,
    *,
    expected_periods: list[ExpectedReportingPeriod],
    resource_reader: ReportingResourceReader | None = None,
    checkpoint_store: ReportingCheckpointStore | None = None,
    max_snapshot_restarts: int = 2,
    max_inspection_attempts: int = 3,
    inspection_timeout_seconds: float = 30.0,
    inspection_retry_backoff_seconds: float = 1.0,
    now: datetime | None = None,
    reporting_capabilities: ReportingDeliveryCapabilities | None = None,
) -> ReportingReconciliationResult:
    """Reconcile a closed ledger, persist observations, and submit receipts.

    Pass ``resource_reader`` for the built-in manifest/file inspector, or
    ``inspect`` as an advanced adapter for warehouses and native shares. Each
    inspection is time-bounded. Typed transient failures retry with exponential
    backoff; permanent integrity failures stop immediately.
    """

    if inspect is not None and resource_reader is not None:
        raise ValueError("pass inspect or resource_reader, not both")
    if resource_reader is not None:
        from adcp.reporting_inspection import ManifestReportingInspector

        inspect = ManifestReportingInspector(resource_reader)

    if (
        not isinstance(max_inspection_attempts, int)
        or isinstance(max_inspection_attempts, bool)
        or max_inspection_attempts < 1
    ):
        raise ValueError("max_inspection_attempts must be at least 1")
    if not isfinite(inspection_timeout_seconds) or inspection_timeout_seconds <= 0:
        raise ValueError("inspection_timeout_seconds must be finite and greater than 0")
    if not isfinite(inspection_retry_backoff_seconds) or inspection_retry_backoff_seconds < 0:
        raise ValueError("inspection_retry_backoff_seconds must be finite and not negative")

    ledger = await load_reporting_ledger(
        client, request, max_snapshot_restarts=max_snapshot_restarts
    )
    if reporting_capabilities is not None:
        tiers = reporting_tiers(reporting_capabilities)
        if ReportingTier.MANAGED_DELIVERY not in tiers and any(
            item.destination_ref is not None for item in ledger.obligations
        ):
            raise ReportingReconciliationError(
                "MANAGED_DELIVERY_NOT_ENABLED",
                "reporting obligations require the managed_delivery tier",
            )
        if ReportingTier.RECONCILED_BILLING not in tiers and any(
            _enum(item.reconciliation_mode) == "consumer_receipt"
            or _enum(item.feed_purpose) == "billing"
            for item in ledger.obligations
        ):
            raise ReportingReconciliationError(
                "RECONCILED_BILLING_NOT_ENABLED",
                "consumer receipts and billing reporting require the reconciled_billing tier",
            )
        if ReportingTier.RECONCILED_BILLING not in tiers and any(
            item.canonical_content_digest is not None for item in ledger.revisions
        ):
            raise ReportingReconciliationError(
                "RECONCILED_BILLING_NOT_ENABLED",
                "canonical-digest reporting requires the reconciled_billing tier",
            )
        if ReportingTier.RECONCILED_BILLING not in tiers and any(
            item.verification
            and _enum(item.verification.verification_profile) == "canonical_digest"
            for item in ledger.materializations
        ):
            raise ReportingReconciliationError(
                "RECONCILED_BILLING_NOT_ENABLED",
                "canonical-digest verification requires the reconciled_billing tier",
            )
    submitted: list[ReportingReceipt] = []
    _, _, index = _validate_read_ledger(ledger)
    for obligation in ledger.obligations:
        if _enum(obligation.reconciliation_mode) != "consumer_receipt":
            continue
        revision, materialization, reasons = index.selections[obligation.reporting_obligation_id]
        if not revision or not materialization or reasons:
            continue
        if any(_receipt_matches(item, revision, materialization) for item in ledger.receipts):
            continue
        receipt = (
            await checkpoint_store.get(materialization.reporting_materialization_id)
            if checkpoint_store
            else None
        )
        checkpoint_is_recorded = bool(
            receipt
            and any(
                item.reporting_receipt_id == receipt.reporting_receipt_id
                for item in ledger.receipts
            )
        )
        if (
            not receipt
            or checkpoint_is_recorded
            or not _receipt_targets(receipt, obligation, revision, materialization)
        ):
            if inspect is None:
                raise ReportingReconciliationError(
                    "INSPECTOR_REQUIRED",
                    "consumer-receipt reconciliation requires inspect or resource_reader",
                )
            last_error: Exception | None = None
            observation = None
            inspection_context = ReportingInspectionContext(obligation, revision, materialization)
            for attempt in range(max_inspection_attempts):
                try:
                    observation = await asyncio.wait_for(
                        inspect(inspection_context), timeout=inspection_timeout_seconds
                    )
                    break
                except Exception as error:  # destination SDKs define their own transient errors
                    if getattr(error, "retryable", None) is False:
                        code = getattr(error, "code", "INSPECTION_FAILED")
                        raise ReportingReconciliationError(_enum(code), str(error)) from error
                    last_error = (
                        TimeoutError(
                            "materialization inspection timed out after "
                            f"{inspection_timeout_seconds:g} seconds"
                        )
                        if isinstance(error, asyncio.TimeoutError)
                        else error
                    )
                    if attempt + 1 < max_inspection_attempts:
                        await asyncio.sleep(inspection_retry_backoff_seconds * (2**attempt))
            if observation is None:
                raise ReportingReconciliationError(
                    "INSPECTION_FAILED",
                    "materialization inspection failed after "
                    f"{max_inspection_attempts} attempts: {last_error}",
                )
            receipt = build_reporting_receipt(inspection_context, observation)
            if checkpoint_store:
                await checkpoint_store.put(receipt)
        submitted.append(receipt)

    if submitted:
        write = await client.sync_reporting_receipts(
            SyncReportingReceiptsRequest.model_validate(
                {
                    "account": request.account,
                    "idempotency_key": str(uuid4()),
                    "receipts": submitted,
                }
            )
        )
        if not write.success or write.data is None:
            raise ReportingReconciliationError(
                "RECEIPT_WRITE_FAILED", "seller did not record reporting receipts"
            )
        failed = [item for item in write.data.results if _enum(item.result) == "failed"]
        if failed:
            raise ReportingReconciliationError(
                "RECEIPT_WRITE_FAILED", f"{len(failed)} reporting receipt(s) failed"
            )
        ledger = await load_reporting_ledger(
            client, request, max_snapshot_restarts=max_snapshot_restarts
        )

    result = evaluate_reporting_ledger(ledger, expected_periods=expected_periods, now=now)
    result.submitted_receipts = submitted
    return result


__all__ = [
    "ConsumerLoopView",
    "ConsumerStatusCheckpoint",
    "ConsumerStatusCheckpointStore",
    "ConsumerStatusIntent",
    "ConsumerStatusPlanError",
    "ConsumerStatusPostError",
    "ConsumerStatusPostResult",
    "InMemoryConsumerStatusCheckpoints",
    "ReportingContentReading",
    "ReportingFailureCode",
    "ReportingOperationsContactView",
    "ReportingPinnedDefinition",
    "classify_content_mismatch",
    "consumer_status_chain_key",
    "load_consumer_loop_view",
    "plan_consumer_statuses",
    "post_consumer_statuses",
    "resolve_checkpointed_leaves",
    "ExpectedReportingPeriod",
    "ObligationReconciliation",
    "ReportingCheckpointStore",
    "ReportingInspectionContext",
    "ReportingLedger",
    "ReportingObservation",
    "ReportingReconciliationClient",
    "ReportingReconciliationError",
    "ReportingReconciliationResult",
    "ReportingStatusClient",
    "ReportingTier",
    "build_reporting_receipt",
    "evaluate_reporting_ledger",
    "load_reporting_ledger",
    "reconcile_reporting",
    "reconcile_reporting_core",
    "reporting_tiers",
]
