"""Reliable AdCP reporting-ledger reconciliation.

The wire schemas describe facts.  This module turns those facts into the
operational guarantee buyers care about: a closed, retained reporting scope
whose expected periods, current revisions, destination materializations, and
consumer receipts all agree.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
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
from adcp.reporting.revision_selection import RevisionHistoryEntry, select_reporting_revision
from adcp.types import (
    GetReportingStatusRequest,
    GetReportingStatusResponse,
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


_RecordT = TypeVar("_RecordT")


def _add_immutable(
    target: dict[str, _RecordT], identifier: str, value: _RecordT, kind: str
) -> None:
    previous = target.get(identifier)
    if previous is not None and _json(previous) != _json(value):
        raise ReportingReconciliationError(
            "IMMUTABLE_RECORD_CHANGED", f"{kind} {identifier} changed within one ledger snapshot"
        )
    target[identifier] = value


async def load_reporting_ledger(
    client: ReportingStatusClient,
    request: GetReportingStatusRequest,
    *,
    max_snapshot_restarts: int = 2,
) -> ReportingLedger:
    """Exhaust a stable periods cursor and verify its declared record count."""

    base = request.model_dump(mode="json", exclude_none=True)
    base["view"] = "periods"
    base.pop("pagination", None)
    for restart in range(max_snapshot_restarts + 1):
        try:
            obligations: dict[str, ReportingObligation] = {}
            revisions: dict[str, ReportingRevision] = {}
            materializations: dict[str, ReportingMaterialization] = {}
            receipts: dict[str, ReportingReceipt] = {}
            consumer_statuses: dict[str, Any] = {}
            cursor: str | None = None
            seen_cursors: set[str] = set()
            snapshot_id: str | None = None
            ledger_as_of: datetime | None = None
            account_id: str | None = None
            scope: BaseModel | None = None
            total_count: int | None = None

            while True:
                payload = dict(base)
                if cursor:
                    payload["pagination"] = {"cursor": cursor}
                result = await client.get_reporting_status(
                    GetReportingStatusRequest.model_validate(payload)
                )
                response = result.data
                if not result.success or response is None or _enum(response.view) != "periods":
                    raise ReportingReconciliationError(
                        "STATUS_READ_FAILED",
                        "get_reporting_status did not return a completed periods view",
                    )
                pagination = response.pagination
                if (
                    not response.ledger_snapshot_id
                    or not response.ledger_as_of
                    or not response.account_id
                    or not response.scope
                    or pagination is None
                ):
                    raise ReportingReconciliationError(
                        "INCOMPLETE_LEDGER_PAGE", "get_reporting_status omitted ledger metadata"
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
                for status in getattr(response, "consumer_statuses", None) or []:
                    _add_immutable(
                        consumer_statuses,
                        status.reporting_status_id,
                        status,
                        "consumer status",
                    )

                if not pagination.has_more:
                    break
                cursor = pagination.cursor
                if not cursor or cursor in seen_cursors:
                    raise ReportingReconciliationError(
                        "CURSOR_LOOP", "ledger pagination did not advance"
                    )
                seen_cursors.add(cursor)

            count = (
                len(obligations)
                + len(revisions)
                + len(materializations)
                + len(receipts)
                + len(consumer_statuses)
            )
            if total_count is not None and total_count != count:
                raise ReportingReconciliationError(
                    "LEDGER_COUNT_MISMATCH",
                    f"ledger declared {total_count} records but returned {count}",
                )
            if not snapshot_id or not ledger_as_of or not account_id or not scope:
                raise ReportingReconciliationError(
                    "EMPTY_LEDGER_RESPONSE", "get_reporting_status returned no ledger page"
                )
            return ReportingLedger(
                snapshot_id,
                ledger_as_of,
                account_id,
                scope,
                list(obligations.values()),
                list(revisions.values()),
                list(materializations.values()),
                list(receipts.values()),
                list(consumer_statuses.values()),
            )
        except ReportingReconciliationError as error:
            if error.code != "SNAPSHOT_CHANGED" or restart == max_snapshot_restarts:
                raise
    raise ReportingReconciliationError("SNAPSHOT_CHANGED", "ledger never stabilized")


def _select_current(
    obligation: ReportingObligation, ledger: ReportingLedger
) -> tuple[ReportingRevision | None, ReportingMaterialization | None, list[str]]:
    reasons: list[str] = []
    attempts = [
        item
        for item in ledger.materializations
        if item.reporting_obligation_id == obligation.reporting_obligation_id
    ]
    revision_ids = {item.reporting_revision_id for item in attempts}
    managed_delivery = obligation.destination_ref is not None
    # ``ReportingRevision`` carries no obligation reference, so semantic scope
    # alone cannot separate two obligations that legitimately share a definition,
    # profile, campaign set and period. A revision some *other* obligation has
    # materialized is that obligation's publication. Anything this obligation also
    # materialized stays a candidate so illegal fan-out is reported below rather
    # than silently narrowed away. Revisions no obligation has materialized -- an
    # unmaterialized official included -- are never excluded here.
    owned_elsewhere = {
        item.reporting_revision_id
        for item in ledger.materializations
        if item.reporting_obligation_id != obligation.reporting_obligation_id
    } - revision_ids
    candidates = [
        item
        for item in ledger.revisions
        if (
            _revision_matches_obligation(item, obligation)
            or (managed_delivery and item.reporting_revision_id in revision_ids)
        )
        and item.reporting_revision_id not in owned_elsewhere
    ]
    receipts = [
        item
        for item in ledger.receipts
        if item.reporting_obligation_id == obligation.reporting_obligation_id
    ]
    successful_attempts = [
        item for item in attempts if _enum(item.status) in {"available", "delivered"}
    ]
    accepted_receipts = [item for item in receipts if _enum(item.status) == "accepted"]
    history_incomplete = len(candidates) != obligation.revision_count
    if managed_delivery:
        history_incomplete = history_incomplete or (
            obligation.materialization_count is None
            or len(attempts) != obligation.materialization_count
            or obligation.successful_materialization_count is None
            or len(successful_attempts) != obligation.successful_materialization_count
        )
    elif attempts:
        history_incomplete = True
    if obligation.receipt_count is not None:
        history_incomplete = history_incomplete or len(receipts) != obligation.receipt_count
    if obligation.accepted_receipt_count is not None:
        history_incomplete = (
            history_incomplete or len(accepted_receipts) != obligation.accepted_receipt_count
        )
    if _enum(obligation.reconciliation_mode) == "consumer_receipt" and (
        obligation.receipt_count is None or obligation.accepted_receipt_count is None
    ):
        history_incomplete = True
    if history_incomplete:
        reasons.append("ASSOCIATED_HISTORY_INCOMPLETE")
    if any(not _revision_matches_obligation(item, obligation) for item in candidates) or any(
        item.reporting_revision_id in revision_ids
        and item.reporting_obligation_id != obligation.reporting_obligation_id
        for item in ledger.materializations
    ):
        reasons.append("REVISION_SCOPE_MISMATCH")
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

    if not managed_delivery:
        if _enum(obligation.reconciliation_mode) == "consumer_receipt":
            reasons.append("INVALID_RECONCILIATION_TIER")
        return revision, None, reasons

    successful = sorted(
        (
            item
            for item in successful_attempts
            if item.reporting_revision_id == revision.reporting_revision_id
        ),
        key=lambda item: item.attempt,
        reverse=True,
    )
    materialization = successful[0] if successful else None
    if (
        not materialization
        or not materialization.ready_at
        or not materialization.verification
        or not materialization.resource
    ):
        reasons.append("MISSING_VERIFIED_MATERIALIZATION")
        return revision, materialization, reasons
    if (
        materialization.delivery_config_id != obligation.delivery_config_id
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
    return revision, materialization, reasons


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


def evaluate_reporting_ledger(
    ledger: ReportingLedger,
    *,
    expected_periods: list[ExpectedReportingPeriod] | None = None,
    now: datetime | None = None,
) -> ReportingReconciliationResult:
    now = now or datetime.now(timezone.utc)
    outcomes: list[ObligationReconciliation] = []
    unique_revisions: dict[str, ReportingRevision] = {}
    for obligation in ledger.obligations:
        revision, materialization, reasons = _select_current(obligation, ledger)
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
        if (
            _enum(obligation.reconciliation_mode) == "consumer_receipt"
            and revision
            and materialization
            and not any(
                _receipt_matches(receipt, revision, materialization) for receipt in ledger.receipts
            )
        ):
            reasons.append("MISSING_MATCHING_CONSUMER_RECEIPT")
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
        )
        for item in ledger.obligations
    }
    missing = [
        item
        for item in expected_periods or []
        if (
            item.delivery_config_id,
            item.delivery_config_version,
            item.report_definition_id,
            item.feed_purpose,
            item.reporting_profile,
            tuple(sorted(item.media_buy_ids)),
            _iso(item.period_start),
            _iso(item.period_end),
        )
        not in actual
    ]
    definitive = bool(
        expected_periods is not None
        and bool(getattr(ledger.scope, "scope_closed", False))
        and bool(getattr(ledger.scope, "coverage_complete", False))
        and not missing
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
                obligation.reporting_obligation_id: [
                    revision
                    for revision in ledger.revisions
                    if _revision_matches_obligation(revision, obligation)
                ]
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
    for obligation in ledger.obligations:
        if _enum(obligation.reconciliation_mode) != "consumer_receipt":
            continue
        revision, materialization, reasons = _select_current(obligation, ledger)
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
