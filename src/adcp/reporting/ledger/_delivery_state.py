"""One transition contract shared by memory and PostgreSQL, with no I/O."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Annotated, Any, NoReturn, TypeVar

from pydantic import Field, TypeAdapter, ValidationError

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.currency import require_frozen_currency
from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingReceiptRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.models import (
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.store import (
    LedgerConflictError,
    validate_adjustment_currency,
    validate_managed_total_units,
)

RecordT = TypeVar("RecordT", bound=ReportingDeliveryRecord)
_ADAPTER: TypeAdapter[ReportingDeliveryRecord] = TypeAdapter(
    Annotated[ReportingDeliveryRecord, Field(discriminator="kind")]
)
_RECEIPTS = (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)


def fail(code: str) -> NoReturn:
    # All errors are closed classifications. Never attach provider exceptions,
    # raw input, identifiers, a database detail string, or credential resolver state.
    raise LedgerConflictError(code, "reporting record violates the retained evidence contract")


def unavailable() -> NoReturn:
    raise LedgerConflictError("REPORTING_RECORD_UNAVAILABLE", "reporting record is unavailable")


def iso(value: datetime) -> str:
    return aware_utc(value).isoformat().replace("+00:00", "Z")


def payload(record: ReportingDeliveryRecord) -> dict[str, Any]:
    # Dataclass fields only: never __dict__, a provider object, or a wire extras bag.
    result: dict[str, Any] = json.loads(json.dumps(asdict(record), default=iso))
    return result


def decode_record(value: object) -> ReportingDeliveryRecord:
    result: ReportingDeliveryRecord | None = None
    try:
        result = _ADAPTER.validate_python(value)
    except (ValidationError, ValueError, TypeError):
        # Classify after leaving the handler so validation details do not become
        # the public error's exception context.
        pass
    if result is None:
        fail("INVALID_REPORTING_RECORD")
    if payload(result) != value:
        # Predecessor dataclasses may otherwise ignore unknown nested fields.
        # Never hash or echo a raw untrusted payload while checking its shape.
        fail("INVALID_REPORTING_RECORD")
    return result


def fingerprint(record: ReportingDeliveryRecord) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(payload(record))).hexdigest()


def principal(record: ReportingDeliveryRecord) -> ReportingDeliveryPrincipal:
    return (
        record.principal
        if isinstance(record, ReportingDestinationBinding)
        else record.scope.principal
    )


def record_identity(record: ReportingDeliveryRecord) -> tuple[str, str]:
    if isinstance(record, ReportingDestinationBinding):
        generation = record.generation_key
        return record.kind, hashlib.sha256(canonical_json_utf8_v1(asdict(generation))).hexdigest()
    if isinstance(record, ReportingObligationDeliveryRecord):
        return record.kind, record.scope.reporting_obligation_id
    if isinstance(record, ReportingMaterializationCheck):
        return record.kind, record.check_id
    if isinstance(record, _RECEIPTS):
        # One namespace across both receipt arrays, as required by the batch schema.
        return "receipt", record.reporting_receipt_id
    return record.kind, record.reporting_materialization_id


def change_id(record: ReportingDeliveryRecord) -> str:
    who = principal(record)
    return hashlib.sha256(
        canonical_json_utf8_v1([who.account_id, who.consumer_id, *record_identity(record)])
    ).hexdigest()


def receipt_chain(record: ReportingReceiptRecord) -> str:
    if isinstance(record, ReportingAdjustmentReceiptRecord):
        parts = ["adjustment", record.reporting_adjustment_id]
    else:
        parts = ["revision", record.scope.reporting_obligation_id, record.reporting_revision_id]
    return hashlib.sha256(canonical_json_utf8_v1(parts)).hexdigest()


def storage_identity(record: ReportingDeliveryRecord) -> tuple[str | int | None, ...]:
    """Every indexed/joined/transition column, in the PostgreSQL schema order."""
    who = principal(record)
    if isinstance(record, ReportingDestinationBinding):
        generation = record.generation_key
        obligation_id = None
    else:
        generation = record.scope.generation_key
        obligation_id = record.scope.reporting_obligation_id
    receipt: ReportingReceiptRecord | None = record if isinstance(record, _RECEIPTS) else None
    revision_id = None
    if isinstance(record, ReportingAdjustmentReceiptRecord):
        revision_id = record.adjusts_reporting_revision_id
    elif isinstance(
        record,
        (
            ReportingMaterializationAttempt,
            ReportingMaterializationRecord,
            ReportingRevisionReceiptRecord,
        ),
    ):
        revision_id = record.reporting_revision_id
    materialization_id = (
        record.reporting_materialization_id
        if isinstance(
            record,
            (
                ReportingMaterializationAttempt,
                ReportingMaterializationRecord,
                ReportingMaterializationCheck,
                ReportingRevisionReceiptRecord,
            ),
        )
        else None
    )
    return (
        who.account_id,
        who.consumer_id,
        *record_identity(record),
        record.kind,
        generation.delivery_config_id,
        generation.delivery_config_version,
        obligation_id,
        revision_id,
        materialization_id,
        (
            record.reporting_adjustment_id
            if isinstance(record, ReportingAdjustmentReceiptRecord)
            else None
        ),
        record.attempt if isinstance(record, ReportingMaterializationAttempt) else None,
        receipt_chain(receipt) if receipt is not None else None,
        receipt.status if receipt is not None else None,
        receipt.supersedes_reporting_receipt_id if receipt is not None else None,
    )


def current_receipt(
    records: tuple[ReportingDeliveryRecord, ...], requested: ReportingReceiptRecord
) -> ReportingReceiptRecord | None:
    chain = [
        item
        for item in records
        if isinstance(item, _RECEIPTS) and receipt_chain(item) == receipt_chain(requested)
    ]
    if not chain:
        return None
    by_id = {item.reporting_receipt_id: item for item in chain}
    superseded = [
        item.supersedes_reporting_receipt_id
        for item in chain
        if item.supersedes_reporting_receipt_id is not None
    ]
    leaves = [item for item in chain if item.reporting_receipt_id not in superseded]
    if len(by_id) != len(chain) or len(set(superseded)) != len(superseded) or len(leaves) != 1:
        fail("REPORTING_HISTORY_CORRUPT")
    # One apparent leaf alone does not rule out a disconnected cycle, a missing
    # predecessor, an accepted predecessor, or a cross-target/generation edge.
    visited: set[str] = set()
    node: ReportingReceiptRecord | None = leaves[0]
    while node is not None:
        if (
            node.reporting_receipt_id in visited
            or type(node) is not type(requested)
            or node.scope != requested.scope
        ):
            fail("REPORTING_HISTORY_CORRUPT")
        if isinstance(node, ReportingAdjustmentReceiptRecord) and isinstance(
            requested, ReportingAdjustmentReceiptRecord
        ):
            if node.adjusts_reporting_revision_id != requested.adjusts_reporting_revision_id:
                fail("REPORTING_HISTORY_CORRUPT")
        visited.add(node.reporting_receipt_id)
        predecessor_id = node.supersedes_reporting_receipt_id
        if predecessor_id is None:
            node = None
        else:
            node = by_id.get(predecessor_id)
            if node is None or node.status != "rejected":
                fail("REPORTING_HISTORY_CORRUPT")
    if len(visited) != len(chain):
        fail("REPORTING_HISTORY_CORRUPT")
    return leaves[0]


def replay(
    record: ReportingDeliveryRecord, records: tuple[ReportingDeliveryRecord, ...]
) -> ReportingDeliveryRecord | None:
    existing = next(
        (item for item in records if record_identity(item) == record_identity(record)), None
    )
    if existing is None:
        return None
    if isinstance(existing, _RECEIPTS):
        current_receipt(records, existing)
    comparison = existing
    if (
        isinstance(record, _RECEIPTS)
        and isinstance(existing, _RECEIPTS)
        and record.received_at is None
    ):
        comparison = replace(existing, received_at=None)
    if fingerprint(comparison) != fingerprint(record):
        fail("REPORTING_IDENTITY_CONFLICT")
    return existing


@dataclass(frozen=True)
class DeliveryContext:
    configuration: ReportingConfiguration | None = None
    obligation: ReportingObligationRecord | None = None
    revision: ReportingRevisionRecord | None = None
    adjustment: ReportingAdjustmentRecord | None = None


def validate_transition(
    record: ReportingDeliveryRecord,
    records: tuple[ReportingDeliveryRecord, ...],
    context: DeliveryContext,
    now: datetime,
) -> ReportingDeliveryRecord:
    """Validate only new writes. Call replay first, including after retention loss."""
    now = aware_utc(now)
    who = principal(record)
    if isinstance(record, ReportingDestinationBinding):
        config = context.configuration
        if config is None or config.generation_key != record.generation_key:
            unavailable()
        if config.feed_purpose != record.feed_purpose:
            fail("CONFIGURATION_BINDING_MISMATCH")
        if record.created_at > now:
            fail("REPORTING_TIME_INVALID")
        if config.feed_purpose == "billing" and record.verification_profile != "canonical_digest":
            fail("BILLING_REQUIRES_CANONICAL_DIGEST")
        if config.feed_purpose == "billing" and (
            record.reconciliation_mode != "consumer_receipt"
            or config.required_finality != "official"
        ):
            fail("BILLING_REQUIRES_OFFICIAL_RECEIPTS")
        return record

    binding = next(
        (
            item
            for item in records
            if isinstance(item, ReportingDestinationBinding)
            and item.generation_key == record.scope.generation_key
        ),
        None,
    )
    obligation = context.obligation
    if (
        binding is None
        or obligation is None
        or obligation.account_id != who.account_id
        or (
            obligation.reporting_obligation_id != record.scope.reporting_obligation_id
            or obligation.generation_key != record.scope.generation_key
        )
    ):
        unavailable()
    currency = require_frozen_currency(obligation.currency)

    if isinstance(record, ReportingObligationDeliveryRecord):
        if record.currency != currency:
            fail("CURRENCY_MISMATCH")
        if (
            record.created_at < max(binding.created_at, obligation.created_at)
            or record.created_at > now
        ):
            fail("REPORTING_TIME_INVALID")
        if record.resource_retained_until < record.created_at + timedelta(
            days=binding.resource_retention_days
        ):
            fail("RESOURCE_RETENTION_INVALID")
        return record

    delivery = next(
        (
            item
            for item in records
            if isinstance(item, ReportingObligationDeliveryRecord) and item.scope == record.scope
        ),
        None,
    )
    if delivery is None:
        unavailable()
    if delivery.currency != currency:
        fail("CURRENCY_MISMATCH")

    if isinstance(record, ReportingMaterializationCheck):
        target = _materialization(records, record.reporting_materialization_id)
        if target is None or target.scope != record.scope or target.status == "failed":
            unavailable()
        assert target.resource is not None
        latest = latest_check(records, record.reporting_materialization_id)
        if (
            record.checked_at < target.completed_at
            or record.checked_at > now
            or (latest is not None and record.checked_at <= latest.checked_at)
        ):
            fail("REPORTING_TIME_INVALID")
        if record.state == "readable" and record.checked_at >= target.resource.expires_at:
            fail("RESOURCE_RETENTION_INVALID")
        return record

    revision = context.revision
    revision_id = (
        record.adjusts_reporting_revision_id
        if isinstance(record, ReportingAdjustmentReceiptRecord)
        else record.reporting_revision_id
    )
    if (
        revision is None
        or revision.account_id != who.account_id
        or revision.reporting_revision_id != revision_id
        or revision.reporting_obligation_id != obligation.reporting_obligation_id
    ):
        unavailable()

    if isinstance(record, ReportingMaterializationAttempt):
        revision_control_totals(revision, obligation)
        if (
            record.created_at < max(delivery.created_at, revision.created_at)
            or record.created_at > now
        ):
            fail("REPORTING_TIME_INVALID")
        siblings = [
            item
            for item in records
            if isinstance(item, ReportingMaterializationAttempt)
            and item.scope == record.scope
            and item.reporting_revision_id == revision_id
        ]
        if record.attempt != len(siblings) + 1:
            fail("MATERIALIZATION_ATTEMPT_CONFLICT")
        if (
            binding.verification_profile == "canonical_digest"
            and revision.canonical_content_digest is None
        ):
            fail("REVISION_CANONICAL_EVIDENCE_REQUIRED")
        if not revision.readable:
            fail("REVISION_UNREADABLE")
        return record

    if isinstance(record, ReportingMaterializationRecord):
        attempt = next(
            (
                item
                for item in records
                if isinstance(item, ReportingMaterializationAttempt)
                and item.reporting_materialization_id == record.reporting_materialization_id
            ),
            None,
        )
        if (
            attempt is None
            or attempt.scope != record.scope
            or attempt.reporting_revision_id != revision_id
        ):
            unavailable()
        if record.completed_at < attempt.created_at or record.completed_at > now:
            fail("REPORTING_TIME_INVALID")
        if record.status != "failed":
            _verify_materialization(record, binding, delivery, revision, obligation)
        return record

    if binding.reconciliation_mode != "consumer_receipt":
        fail("RECEIPTS_NOT_ENABLED")
    if record.received_at is not None:
        fail("RECEIVED_AT_READ_ONLY")
    if record.observed_at > now:
        fail("REPORTING_TIME_INVALID")
    if isinstance(record, ReportingRevisionReceiptRecord):
        _verify_receipt(record, records, revision)
    else:
        adjustment = context.adjustment
        if (
            adjustment is None
            or adjustment.account_id != who.account_id
            or (
                adjustment.reporting_adjustment_id != record.reporting_adjustment_id
                or adjustment.adjusts_reporting_revision_id != revision_id
            )
        ):
            unavailable()
        validate_adjustment_currency(obligation, adjustment)
        if revision.finality != "official" or revision.finalized_at is None:
            fail("ADJUSTMENT_REQUIRES_OFFICIAL")
        if not (
            obligation.period.end <= revision.finalized_at <= revision.created_at
            and revision.finalized_at <= adjustment.correction_observed_at <= adjustment.created_at
            and adjustment.accounting_period_start < adjustment.accounting_period_end
            and adjustment.created_at <= record.observed_at
        ):
            fail("ADJUSTMENT_ORDER_INVALID")
        if record.status == "accepted" and record.observed_adjustment_sha256 != adjustment_sha256(
            adjustment
        ):
            fail("ADJUSTMENT_DIGEST_MISMATCH")

    leaf = current_receipt(records, record)
    if leaf is not None and leaf.status == "accepted":
        fail("ACCEPTED_RECEIPT_TERMINAL")
    if leaf is None:
        if record.supersedes_reporting_receipt_id is not None:
            unavailable()
    elif record.supersedes_reporting_receipt_id != leaf.reporting_receipt_id:
        # Missing/stale/cross-caller pointers have the same result.
        unavailable()
    return replace(record, received_at=now)


def _verify_materialization(
    record: ReportingMaterializationRecord,
    binding: ReportingDestinationBinding,
    delivery: ReportingObligationDeliveryRecord,
    revision: ReportingRevisionRecord,
    obligation: ReportingObligationRecord,
) -> None:
    resource, verification = record.resource, record.verification
    assert resource is not None and verification is not None
    if record.status != binding.success_status:
        fail("MATERIALIZATION_STATUS_MISMATCH")
    if verification.verified_at != record.completed_at:
        fail("VERIFICATION_TIME_MISMATCH")
    if resource.expires_at < max(
        delivery.resource_retained_until,
        record.completed_at + timedelta(days=binding.resource_retention_days),
    ):
        fail("RESOURCE_RETENTION_INVALID")
    if (
        verification.verification_profile != binding.verification_profile
        or verification.verified_format != binding.format
    ):
        fail("VERIFICATION_PROFILE_MISMATCH")
    if resource.reader_compatibility != binding.reader_compatibility:
        fail("READER_COMPATIBILITY_MISMATCH")
    if verification.row_count != revision.row_count or sorted(
        verification.control_totals, key=lambda item: item.name
    ) != sorted(revision_control_totals(revision, obligation), key=lambda item: item.name):
        fail("VERIFICATION_TOTALS_MISMATCH")
    digest = verification.canonical_content_digest
    if (digest is not None and digest != revision.canonical_content_digest) or (
        binding.verification_profile == "canonical_digest" and digest is None
    ):
        fail("VERIFICATION_DIGEST_MISMATCH")
    expected_kind = {
        "file_transfer": "manifest",
        "dataset_share": "dataset",
        "warehouse_materialization": "warehouse_relation",
    }[binding.method]
    if resource.kind != expected_kind:
        fail("VERIFICATION_METHOD_MISMATCH")
    path = verification.verification_path
    if (
        (binding.method == "dataset_share" and path != "representative_consumer")
        or (binding.method == "warehouse_materialization" and path != "destination")
        or (record.status == "delivered" and path != "destination")
        or (binding.method == "warehouse_materialization" and record.status != "delivered")
    ):
        fail("VERIFICATION_PATH_MISMATCH")
    checksums = verification.physical_checksums
    if binding.method == "file_transfer" and (not checksums or not resource.object_refs):
        fail("PHYSICAL_CHECKSUMS_REQUIRED")
    if len({(item.object_ref, item.algorithm) for item in checksums}) != len(checksums) or any(
        item.object_ref not in resource.object_refs for item in checksums
    ):
        fail("PHYSICAL_CHECKSUM_BINDING_MISMATCH")
    if binding.method == "file_transfer" and {item.object_ref for item in checksums} != set(
        resource.object_refs
    ):
        fail("PHYSICAL_CHECKSUM_BINDING_MISMATCH")
    if binding.verification_profile == "manifest_checksums" and (
        resource.kind != "manifest" or resource.manifest_sha256 is None or not checksums
    ):
        fail("PHYSICAL_CHECKSUMS_REQUIRED")
    if (
        binding.verification_profile == "native_commit"
        or verification.native_version_ref is not None
    ):
        if (
            resource.immutability != "native_version"
            or verification.native_version_ref is None
            or verification.native_version_ref != resource.native_version_ref
            or verification.native_observed_through != path
        ):
            fail("NATIVE_COMMIT_MISMATCH")


def _materialization(
    records: tuple[ReportingDeliveryRecord, ...], materialization_id: str
) -> ReportingMaterializationRecord | None:
    return next(
        (
            item
            for item in records
            if isinstance(item, ReportingMaterializationRecord)
            and item.reporting_materialization_id == materialization_id
        ),
        None,
    )


def latest_check(
    records: tuple[ReportingDeliveryRecord, ...],
    materialization_id: str,
    *,
    at: datetime | None = None,
) -> ReportingMaterializationCheck | None:
    return max(
        (
            item
            for item in records
            if isinstance(item, ReportingMaterializationCheck)
            and item.reporting_materialization_id == materialization_id
            and (at is None or item.checked_at <= at)
        ),
        key=lambda item: item.checked_at,
        default=None,
    )


def _verify_receipt(
    record: ReportingRevisionReceiptRecord,
    records: tuple[ReportingDeliveryRecord, ...],
    revision: ReportingRevisionRecord,
) -> None:
    target = _materialization(records, record.reporting_materialization_id)
    if (
        target is None
        or target.scope != record.scope
        or (
            target.reporting_revision_id != record.reporting_revision_id
            or target.status == "failed"
        )
    ):
        unavailable()
    assert target.resource is not None and target.verification is not None
    if record.verification_profile != target.verification.verification_profile:
        fail("RECEIPT_PROFILE_MISMATCH")
    if record.observed_at < target.completed_at:
        fail("REPORTING_TIME_INVALID")
    if record.status != "accepted":
        return
    if (
        record.observed_row_count != revision.row_count
        or record.observed_row_count != target.verification.row_count
        or sorted(record.observed_control_totals, key=lambda item: item.name)
        != sorted(target.verification.control_totals, key=lambda item: item.name)
    ):
        fail("RECEIPT_TOTALS_MISMATCH")
    profile = record.verification_profile
    if (
        (
            record.observed_canonical_content_digest is not None
            and (
                record.observed_canonical_content_digest != revision.canonical_content_digest
                or record.observed_canonical_content_digest
                != target.verification.canonical_content_digest
            )
        )
        or (
            record.observed_manifest_sha256 is not None
            and record.observed_manifest_sha256 != target.resource.manifest_sha256
        )
        or (
            record.observed_native_version_ref is not None
            and record.observed_native_version_ref != target.resource.native_version_ref
        )
        or (
            profile == "canonical_digest"
            and (
                record.observed_canonical_content_digest is None
                or record.observed_canonical_content_digest != revision.canonical_content_digest
            )
        )
        or (
            profile == "manifest_checksums"
            and record.observed_manifest_sha256 != target.resource.manifest_sha256
        )
        or (
            profile == "native_commit"
            and record.observed_native_version_ref != target.resource.native_version_ref
        )
    ):
        fail("RECEIPT_EVIDENCE_MISMATCH")
    check = latest_check(records, record.reporting_materialization_id, at=record.observed_at)
    if record.observed_at >= target.resource.expires_at or (
        check is not None and check.state != "readable"
    ):
        fail("MATERIALIZATION_UNREADABLE")


def totals_to_wire(totals: tuple[ReportingControlTotalRecord, ...]) -> list[dict[str, str]]:
    return [item.to_wire() for item in totals]


def revision_control_totals(
    revision: ReportingRevisionRecord, obligation: ReportingObligationRecord
) -> tuple[ReportingControlTotalRecord, ...]:
    if revision.managed_control_totals is None:
        fail("REVISION_TOTAL_EVIDENCE_REQUIRED")
    validate_managed_total_units(obligation, revision.managed_control_totals)
    return revision.managed_control_totals


def adjustment_payload(adjustment: ReportingAdjustmentRecord) -> dict[str, Any]:
    if not adjustment.managed_control_total_deltas:
        fail("ADJUSTMENT_TOTAL_EVIDENCE_REQUIRED")
    result: dict[str, Any] = {
        "reporting_adjustment_id": adjustment.reporting_adjustment_id,
        "adjusts_reporting_revision_id": adjustment.adjusts_reporting_revision_id,
        "reason_code": adjustment.reason_code,
        "accounting_period": {
            "start": iso(adjustment.accounting_period_start),
            "end": iso(adjustment.accounting_period_end),
        },
        "control_total_deltas": totals_to_wire(adjustment.managed_control_total_deltas),
        "correction_observed_at": iso(adjustment.correction_observed_at),
        "created_at": iso(adjustment.created_at),
    }
    if adjustment.reason_detail is not None:
        result["reason_detail"] = adjustment.reason_detail
    return result


def adjustment_sha256(adjustment: ReportingAdjustmentRecord) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(adjustment_payload(adjustment))).hexdigest()
