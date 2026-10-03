"""Registered production promises, independent of runtime work admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.models import ReportingDeliveryEscalation
from adcp.reporting.ledger.producer import _advertised_reporting_delivery

if TYPE_CHECKING:
    from adcp.reporting.production.service import ReportingProductionDestination


def declared_reporting_delivery(
    *,
    offerings: Sequence[Mapping[str, Any]],
    destination: ReportingProductionDestination,
    durable: bool,
    escalation: ReportingDeliveryEscalation,
    consumer_status_enabled: bool,
    automated_recovery_window: timedelta,
    status_retention_days: int,
    notifications: bool,
) -> dict[str, Any]:
    # The development memory production graph deliberately supplies no durable
    # production guarantees. Ordinary Core memory services still declare their
    # inline offerings through the Core projector.
    if not durable or not offerings:
        return {}
    from adcp.reporting.production.service import ReportingProductionDestination

    if (
        not isinstance(destination, ReportingProductionDestination)
        or destination.production_eligible is not True
        or type(destination.resource_retention_days) is not int
        or not 1 <= destination.resource_retention_days <= 36500
        or type(destination.authorization_revocation_seconds) is not int
        or not 0 <= destination.authorization_revocation_seconds <= 86400
    ):
        raise ValueError("production declaration requires bounded registered destination promises")
    payload = _advertised_reporting_delivery(
        escalation=escalation,
        consumer_status_task=consumer_status_enabled,
        offerings=offerings,
        automated_recovery_window=automated_recovery_window,
        status_retention_days=status_retention_days,
    )
    payload.update(
        managed_delivery=True,
        resource_retention_days=destination.resource_retention_days,
        authorization_revocation_seconds=destination.authorization_revocation_seconds,
    )
    if any(o.get("reconciliation_mode") == "consumer_receipt" for o in offerings):
        payload.update(reconciled_billing=True, receipt_task="sync_reporting_receipts")
    if notifications:
        payload.update(
            ledger_notification="reporting.ledger_changed",
            readiness_notification="reporting.delivery_ready",
            status_notification="reporting.status_changed",
        )
    return payload
