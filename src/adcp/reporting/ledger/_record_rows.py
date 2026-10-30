"""Row codecs for the shared PostgreSQL record layout.

These functions translate between the SQL column tuples of the ledger tables and
the record dataclasses, and between records and their canonical JSON payloads.
They touch no database: the PostgreSQL stores (``adcp-reporting-storage``) use
them to read rows, and :func:`adcp.reporting.ledger.status_snapshot.snapshot_from_storage`
uses them to decode a stored status snapshot without a driver.

The column order is the storage contract the JavaScript SDK shares; change a
column list only together with the DDL in ``adcp-reporting-storage``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from adcp.reporting.evidence import ReportingCanonicalDigest, ReportingControlTotalRecord
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingDefinitionBinding,
    ReportingIssueLifecycle,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
    ReportingSubscriptionGeneration,
)
from adcp.reporting.ledger.store import _utc, managed_revision_metadata

_OBLIGATION_COLUMNS = (
    "reporting_obligation_id, account_id, delivery_config_id, delivery_config_version,"
    " report_definition_id, reporting_profile, feed_purpose, period_key, period_start,"
    " period_end, source_timezone, expected_at, scope_resolved_at,"
    " automated_recovery_deadline_at, required_finality, coverage_status, media_buy_ids,"
    " package_ids, schedule, definition, created_at, currency, consumer_id, offering_id"
)

#: ``offering_id`` is '' for an account-level generation and the resolved
#: offering for a reporting subscription's generation (AdCP 3.3).
_CONFIGURATION_COLUMNS = (
    "delivery_config_id, delivery_config_version, account_id,"
    " report_definition_id, reporting_profile, feed_purpose, required_finality,"
    " account_timezone, schedule, media_buy_ids, activated_at, deactivated_at,"
    " automated_recovery_seconds, status_retention_days, definition,"
    " authoritative_party, consumer_id, quarantined, offering_id"
)


_REVISION_COLUMNS = (
    "reporting_revision_id, account_id, reporting_obligation_id, finality,"
    " revision_content_sha256, row_count, control_totals, observed_at, data_through,"
    " created_at, supersedes_reporting_revision_id, finality_basis, finality_policy_id,"
    " finalized_at, readable, readable_at_commit, source_publication_id, source_manifest_sha256,"
    " canonical_content_digest, managed_control_totals"
)


_ADJUSTMENT_COLUMNS = (
    "reporting_adjustment_id, account_id, adjusts_reporting_revision_id, reason_code,"
    " reason_detail, accounting_period_start, accounting_period_end, control_total_deltas,"
    " correction_observed_at, created_at, managed_control_total_deltas"
)


_STATUS_COLUMNS = (
    "s.reporting_status_id, s.account_id, s.consumer_id, s.delivery_config_id,"
    " s.delivery_config_version, s.report_definition_id, s.period_start, s.period_end,"
    " s.period_source_timezone, s.consumer_status, s.status_as_of, s.recorded_at,"
    " s.supersedes_reporting_status_id, s.reporting_obligation_id, s.reporting_revision_id,"
    " s.observed_revision_content_sha256, s.failure_code, s.mismatch_code,"
    " s.consumer_commit_ref, s.seller_ledger_snapshot_id, s.seller_ledger_as_of, s.superseded,"
    " s.offering_id"
)


_STATUS_COLUMNS_BARE = _STATUS_COLUMNS.replace("s.", "")


_ISSUE_COLUMNS = (
    "issue_key, account_id, generation, issue_id, consumer_id, opened_at, issue_state,"
    " external_ref, retired_at"
)


def _issue_from_row(row: Sequence[Any]) -> ReportingIssueLifecycle:
    return ReportingIssueLifecycle(
        issue_key=row[0],
        account_id=row[1],
        generation=row[2],
        issue_id=row[3],
        consumer_id=row[4],
        opened_at=_utc(row[5]),
        issue_state=row[6],
        external_ref=row[7],
        retired_at=_utc(row[8]) if row[8] else None,
        waived_reporting_status_id=row[9] if len(row) > 9 else None,
        waived_conflict_sha256=row[10] if len(row) > 10 else None,
    )


def _schedule_payload(schedule: ReportingScheduleSpec) -> dict[str, Any]:
    return {
        "period_duration": schedule.period_duration,
        "delivery_sla": schedule.delivery_sla,
        "alignment": schedule.alignment,
        "period_timezone": schedule.period_timezone,
        "period_anchor": (
            _utc(schedule.period_anchor).isoformat() if schedule.period_anchor else None
        ),
    }


def _definition_payload(
    definition: ReportingDefinitionBinding | None,
) -> dict[str, Any] | None:
    return definition.to_storage() if definition is not None else None


def _definition_from_payload(payload: dict[str, Any] | None) -> ReportingDefinitionBinding | None:
    return ReportingDefinitionBinding(**payload) if payload else None


def _schedule_from_payload(payload: dict[str, Any]) -> ReportingScheduleSpec:
    anchor = payload.get("period_anchor")
    return ReportingScheduleSpec(
        period_duration=payload["period_duration"],
        delivery_sla=payload["delivery_sla"],
        alignment=payload.get("alignment", "utc"),
        period_timezone=payload.get("period_timezone"),
        period_anchor=datetime.fromisoformat(anchor) if anchor else None,
    )


def _configuration_payload(configuration: ReportingConfiguration) -> dict[str, Any]:
    return {
        "account_id": configuration.account_id,
        "report_definition_id": configuration.report_definition_id,
        "reporting_profile": configuration.reporting_profile,
        "feed_purpose": configuration.feed_purpose,
        "required_finality": configuration.required_finality,
        "account_timezone": configuration.account_timezone,
        "media_buy_ids": sorted(configuration.media_buy_ids),
        "schedule": _schedule_payload(configuration.schedule),
        "definition": _definition_payload(configuration.definition),
        # Only a subscription generation carries it, so account generations
        # keep their content digest.
        **(
            {"subscription": configuration.subscription.to_storage()}
            if configuration.subscription is not None
            else {}
        ),
    }


def _offering(row: Sequence[Any], index: int) -> str | None:
    """The trailing ``offering_id`` column; '' (or absent) is an account generation."""
    return (row[index] or None) if len(row) > index else None


def _configuration_from_row(row: Sequence[Any]) -> ReportingConfiguration:
    return ReportingConfiguration(
        delivery_config_id=row[0],
        delivery_config_version=row[1],
        account_id=row[2],
        report_definition_id=row[3],
        reporting_profile=row[4],
        feed_purpose=row[5],
        required_finality=row[6],
        account_timezone=row[7],
        schedule=_schedule_from_payload(row[8]),
        media_buy_ids=tuple(row[9] or ()),
        activated_at=_utc(row[10]) if row[10] else None,
        deactivated_at=_utc(row[11]) if row[11] else None,
        automated_recovery_window=timedelta(seconds=float(row[12])),
        status_retention_days=row[13],
        definition=_definition_from_payload(row[14]),
        authoritative_party=row[15],
        consumer_id=row[16],
        quarantined=row[17],
        subscription=(
            ReportingSubscriptionGeneration(row[0], offering)
            if (offering := _offering(row, 18)) is not None
            else None
        ),
    )


def _obligation_from_row(row: Sequence[Any]) -> ReportingObligationRecord:
    return ReportingObligationRecord(
        reporting_obligation_id=row[0],
        account_id=row[1],
        delivery_config_id=row[2],
        delivery_config_version=row[3],
        report_definition_id=row[4],
        reporting_profile=row[5],
        feed_purpose=row[6],
        period=ReportingPeriodBoundary(
            period_key=row[7],
            start=_utc(row[8]),
            end=_utc(row[9]),
            source_timezone=row[10],
            expected_at=_utc(row[11]),
        ),
        scope_resolved_at=_utc(row[12]),
        automated_recovery_deadline_at=_utc(row[13]),
        required_finality=row[14],
        coverage_status=row[15],
        media_buy_ids=tuple(row[16] or ()),
        package_ids=tuple(row[17] or ()),
        schedule=_schedule_from_payload(row[18]),
        definition=_definition_from_payload(row[19]),
        created_at=_utc(row[20]),
        currency=row[21],
        consumer_id=row[22],
        offering_id=_offering(row, 23),
    )


def _revision_from_row(row: Sequence[Any]) -> ReportingRevisionRecord:
    return ReportingRevisionRecord(
        reporting_revision_id=row[0],
        account_id=row[1],
        reporting_obligation_id=row[2],
        finality=row[3],
        revision_content_sha256=row[4],
        row_count=int(row[5]),
        control_totals=tuple((name, value) for name, value in (row[6] or [])),
        observed_at=_utc(row[7]),
        data_through=_utc(row[8]) if row[8] else None,
        created_at=_utc(row[9]),
        supersedes_reporting_revision_id=row[10],
        finality_basis=row[11],
        finality_policy_id=row[12],
        finalized_at=_utc(row[13]) if row[13] else None,
        readable=row[14],
        readable_at_commit=row[15],
        source_publication_id=row[16],
        source_manifest_sha256=row[17],
        canonical_content_digest=(
            ReportingCanonicalDigest.from_wire(row[18]) if row[18] is not None else None
        ),
        managed_control_totals=(
            tuple(ReportingControlTotalRecord.from_wire(item) for item in row[19])
            if row[19] is not None
            else None
        ),
    )


def _adjustment_from_row(row: Sequence[Any]) -> ReportingAdjustmentRecord:
    return ReportingAdjustmentRecord(
        reporting_adjustment_id=row[0],
        account_id=row[1],
        adjusts_reporting_revision_id=row[2],
        reason_code=row[3],
        reason_detail=row[4],
        accounting_period_start=_utc(row[5]),
        accounting_period_end=_utc(row[6]),
        control_total_deltas=tuple((name, value) for name, value in (row[7] or [])),
        correction_observed_at=_utc(row[8]),
        created_at=_utc(row[9]),
        managed_control_total_deltas=(
            tuple(ReportingControlTotalRecord.from_wire(item) for item in row[10])
            if row[10] is not None
            else None
        ),
    )


def _status_from_row(row: Sequence[Any]) -> ConsumerStatusRecord:
    return ConsumerStatusRecord(
        reporting_status_id=row[0],
        account_id=row[1],
        consumer_id=row[2],
        delivery_config_id=row[3],
        delivery_config_version=row[4],
        report_definition_id=row[5],
        period_start=_utc(row[6]),
        period_end=_utc(row[7]),
        period_source_timezone=row[8],
        consumer_status=row[9],
        status_as_of=_utc(row[10]),
        recorded_at=_utc(row[11]),
        supersedes_reporting_status_id=row[12],
        reporting_obligation_id=row[13],
        reporting_revision_id=row[14],
        observed_revision_content_sha256=row[15],
        failure_code=row[16],
        mismatch_code=row[17],
        consumer_commit_ref=row[18],
        seller_ledger_snapshot_id=row[19],
        seller_ledger_as_of=_utc(row[20]) if row[20] else None,
        superseded=row[21],
        offering_id=_offering(row, 22),
    )


def _consumer_status_payload(status: ConsumerStatusRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chain": list(status.chain_key),
        "consumer_status": status.consumer_status,
        "status_as_of": _utc(status.status_as_of).isoformat(),
        "supersedes": status.supersedes_reporting_status_id,
        "obligation": status.reporting_obligation_id,
        "revision": status.reporting_revision_id,
        "digest": status.observed_revision_content_sha256,
        "failure_code": status.failure_code,
    }
    # Conditional on purpose. ``canonical_json_utf8_v1`` encodes ``None`` as
    # ``null``, so including the key unconditionally would change the digest of
    # every statement that has no mismatch_code -- i.e. every row an rc.2 SDK
    # wrote. After an in-place upgrade a buyer's exact retry would then fail
    # with STATUS_IDENTITY_CONFLICT instead of replaying as ``unchanged``,
    # which is the one thing an idempotent append-only surface must never do.
    # Omitting the key when absent keeps rc.2 digests byte-identical while a
    # code-only change is still a conflict: "the metric is missing" and "the
    # currency is wrong" are different claims and must not share one immutable
    # identity.
    if status.mismatch_code is not None:
        payload["mismatch_code"] = status.mismatch_code
    return payload


def _revision_payload(revision: ReportingRevisionRecord) -> dict[str, Any]:
    payload = {
        "finality": revision.finality,
        "revision_content_sha256": revision.revision_content_sha256,
        "row_count": revision.row_count,
        "control_totals": [list(item) for item in revision.control_totals],
        "obligation": revision.reporting_obligation_id,
        "supersedes": revision.supersedes_reporting_revision_id,
    }
    if revision.canonical_content_digest is not None:
        payload["canonical_content_digest"] = revision.canonical_content_digest.to_wire()
    if revision.managed_control_totals is not None:
        payload["managed_control_totals"] = [
            item.to_wire() for item in revision.managed_control_totals
        ]
    if revision.canonical_content_digest is not None or revision.managed_control_totals is not None:
        payload["managed_metadata"] = managed_revision_metadata(revision)
    return payload
