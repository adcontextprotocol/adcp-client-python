"""Immutable finish boundaries and a real, isolated notification enqueue path.

B2.1 does not expose a delivery worker or an activation certificate. A/B/C
workers cannot see this queue. Pre-activation events stay quarantined forever.
B2.4 must prove the mounted projection and tier components before admitting new
events in the original verified-finish transaction; it cannot release old ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from adcp.reporting.evidence import aware_utc, reporting_identifier
from adcp.reporting.ledger._delivery_state import decode_record, payload, principal
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingMaterializationRecord,
)
from adcp.reporting.ledger.notification_models import (
    ReportingDomainEvent,
    ReportingNotificationError,
)
from adcp.reporting.ledger.status_projection import ReportingStatusSnapshot
from adcp.reporting.outbox.memory import NotificationState

_CORE = TypeAdapter(ReportingStatusSnapshot)


@dataclass(frozen=True)
class ReportingMaterializerBoundary:
    """Private frozen projection inputs; never returned as a public wire blob."""

    caller: ReportingDeliveryPrincipal
    sequence: int
    account_sequence: int
    reporting_materialization_id: str
    as_of: datetime
    core: ReportingStatusSnapshot = field(repr=False)
    reconciliation: tuple[ReportingDeliveryRecord, ...] = field(repr=False)

    def __post_init__(self) -> None:
        outcomes = tuple(
            record
            for record in self.reconciliation
            if isinstance(record, ReportingMaterializationRecord)
            and record.reporting_materialization_id == self.reporting_materialization_id
        )
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or type(self.account_sequence) is not int
            or self.account_sequence < self.sequence
            or self.core.account_id != self.caller.account_id
            or self.core.as_of != self.as_of
            or self.core.consumer_ids != (self.caller.consumer_id,)
            or any(s.consumer_id != self.caller.consumer_id for s in self.core.statuses)
            or any(
                i.consumer_id not in {None, self.caller.consumer_id} for i in self.core.lifecycles
            )
            or any(principal(r) != self.caller for r in self.reconciliation)
            or len(outcomes) != 1
            or outcomes[0].completed_at != self.as_of
        ):
            raise ReportingNotificationError("materializer_boundary_invalid")
        reporting_identifier(self.reporting_materialization_id, maximum=255)
        object.__setattr__(self, "as_of", aware_utc(self.as_of))

    def to_storage(self) -> dict[str, Any]:
        return {
            "version": 1,
            "account_id": self.caller.account_id,
            "consumer_id": self.caller.consumer_id,
            "sequence": self.sequence,
            "account_sequence": self.account_sequence,
            "reporting_materialization_id": self.reporting_materialization_id,
            "as_of": self.as_of.isoformat(),
            "core": _CORE.dump_python(self.core, mode="json"),
            "reconciliation": [payload(r) for r in self.reconciliation],
        }


def decode_materializer_boundary(value: dict[str, Any]) -> ReportingMaterializerBoundary:
    result = None
    try:
        if type(value) is dict and type(value.get("version")) is int and value["version"] == 1:
            result = ReportingMaterializerBoundary(
                ReportingDeliveryPrincipal(value["account_id"], value["consumer_id"]),
                value["sequence"],
                value["account_sequence"],
                value["reporting_materialization_id"],
                datetime.fromisoformat(value["as_of"]),
                _CORE.validate_python(value["core"]),
                tuple(decode_record(r) for r in value["reconciliation"]),
            )
    except (ValueError, TypeError, KeyError, ValidationError):
        # Convert malformed persisted input to the closed protocol error below.
        pass
    if result is None or result.to_storage() != value:
        raise ReportingNotificationError("materializer_boundary_invalid")
    return result


class _MaterializerQueueConnection:
    """Closed table substitution, preserving the finish transaction's connection."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def execute(self, query: str, params: Any = None) -> Any:
        return await self.connection.execute(
            query.replace("reporting_notification_", "reporting_materializer_notification_"), params
        )


async def enqueue_materializer_event_on(connection: Any, event: ReportingDomainEvent) -> None:
    from adcp.reporting.outbox.pg import enqueue_event

    if event.notification_type != "reporting.delivery_ready":
        raise ReportingNotificationError("materializer_event_invalid")
    await enqueue_event(_MaterializerQueueConnection(connection), event)


def private_snapshot(
    snapshot: ReportingStatusSnapshot, caller: ReportingDeliveryPrincipal
) -> ReportingStatusSnapshot:
    """Internal boundaries also exclude other consumers' private statements."""
    statuses = tuple(s for s in snapshot.statuses if s.consumer_id == caller.consumer_id)
    status_ids = {s.reporting_status_id for s in statuses}
    issues = tuple(i for i in snapshot.lifecycles if i.consumer_id in {None, caller.consumer_id})
    issue_ids = {i.issue_id for i in issues}
    return replace(
        snapshot,
        statuses=statuses,
        lifecycles=issues,
        consumer_ids=(caller.consumer_id,),
        issue_scopes=tuple((i, scope) for i, scope in snapshot.issue_scopes if i in issue_ids),
        changes=tuple(
            c for c in snapshot.changes if c[1] != "consumer_status" or c[2] in status_ids
        ),
    )


class MaterializerNotificationState(NotificationState):
    """Pre-activation events remain permanently quarantined, including on restart."""

    def enqueue(self, event: ReportingDomainEvent) -> None:
        super().enqueue(event)
        for (account, consumer, notification_id, _), work in self.expansions.items():
            if (account, consumer, notification_id) == (
                event.account_id,
                event.consumer_namespace,
                event.notification_id,
            ):
                work.state = "quarantined"
