"""Standalone public imports for the pure buyer adjustment primitive."""

from datetime import datetime

from adcp.reporting.adjustment_evidence import (
    ReportingAdjustmentEvidence,
    ReportingAdjustmentReceiptContext,
    ReportingAdjustmentScope,
    build_reporting_adjustment_receipt,
    capture_reporting_adjustment_evidence,
)
from adcp.types import (
    ReportingAdjustment,
    ReportingAdjustmentReceipt,
    ReportingObligation,
    ReportingRevision,
)


def capture_and_build(
    raw: bytes,
    typed: ReportingAdjustment,
    trusted_scope: ReportingAdjustmentScope,
    selected_obligation: ReportingObligation,
    selected_official: ReportingRevision,
    exact_revision_owner: str,
    reserved_receipt_id: str,
    reserved_observed_at: datetime,
    verified_current_leaf: ReportingAdjustmentReceipt | None,
) -> tuple[ReportingAdjustmentEvidence, ReportingAdjustmentReceipt]:
    evidence = capture_reporting_adjustment_evidence(
        raw, typed_adjustment=typed, scope=trusted_scope
    )
    context = ReportingAdjustmentReceiptContext.from_selection(
        trusted_scope,
        obligation=selected_obligation,
        revision=selected_official,
        revision_owner=exact_revision_owner,
    )
    receipt = build_reporting_adjustment_receipt(
        evidence,
        context,
        reporting_receipt_id=reserved_receipt_id,
        observed_at=reserved_observed_at,
        current_receipt=verified_current_leaf,
    )
    return evidence, receipt
