"""Replay retained private boundaries without creating active notifications.

The old materializer and receipt captures are complete for their own caller.
They are never joined to today's configuration, artifacts or other consumers.
Their derived checkpoints use the same pure projector and generation primitive
as live projection. The original readiness queues and external identities are
not read, copied or modified here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.models import ReportingDeliveryEscalation
from adcp.reporting.ledger.notification_models import (
    ReportingNotificationError,
    event_storage,
)
from adcp.reporting.ledger.status_projection import (
    StatusProjectionInput,
    project_status_scope,
    projection_scopes,
)
from adcp.reporting.materializer.capture import (
    ReportingMaterializerBoundary,
    decode_materializer_boundary,
)
from adcp.reporting.outbox.status import StatusCheckpoint, advance_checkpoint, settled_replay
from adcp.reporting.receipts.capture import ReportingReceiptBoundary, decode_receipt_boundary

HistoricalBoundary = ReportingMaterializerBoundary | ReportingReceiptBoundary
HistoricalKind = Literal["materializer", "receipt"]
_CHECKPOINT = TypeAdapter(StatusCheckpoint)


def decode_boundary(kind: HistoricalKind, value: dict[str, Any]) -> HistoricalBoundary:
    if kind == "materializer":
        return decode_materializer_boundary(value)
    if kind == "receipt":
        return decode_receipt_boundary(value)
    raise ReportingNotificationError("status_projection_history_corrupt")


def checkpoint_key(checkpoint: StatusCheckpoint) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(checkpoint.scope.checkpoint_key)).hexdigest()


def checkpoint_document(checkpoint: StatusCheckpoint) -> dict[str, Any]:
    result: dict[str, Any] = _CHECKPOINT.dump_python(checkpoint, mode="json")
    return result


def decode_checkpoint(document: dict[str, Any]) -> StatusCheckpoint:
    value = _CHECKPOINT.validate_python(document)
    if checkpoint_document(value) != document:
        raise ReportingNotificationError("status_projection_history_corrupt")
    return value


@dataclass(frozen=True)
class HistoricalStep:
    checkpoint: StatusCheckpoint
    event: dict[str, Any] | None

    def document(self) -> dict[str, Any]:
        return {
            "admission_epoch": 0,
            "checkpoint": checkpoint_document(self.checkpoint),
            "event": self.event,
        }


def project_boundary(
    boundary: HistoricalBoundary,
    previous: Mapping[str, StatusCheckpoint],
    *,
    baselines: Mapping[str, StatusCheckpoint],
    source_sequence: int,
    escalation: ReportingDeliveryEscalation | None,
    consumer_status_enabled: bool,
) -> tuple[HistoricalStep, ...]:
    core = settled_replay(boundary.core)
    scopes = {
        s.checkpoint_key: s
        for s in projection_scopes(core)
        if s.consumer_id == boundary.caller.consumer_id
    }
    scopes.update(
        (c.scope.checkpoint_key, c.scope)
        for c in (*baselines.values(), *previous.values())
        if c.scope.account_id == boundary.caller.account_id
        and c.scope.consumer_id == boundary.caller.consumer_id
    )
    results = []
    for scope in sorted(scopes.values(), key=lambda s: s.checkpoint_key):
        result = project_status_scope(
            StatusProjectionInput(
                core,
                scope,
                escalation,
                reconciliation=boundary.reconciliation,
                consumer_status_enabled=consumer_status_enabled,
            )
        )
        key = hashlib.sha256(canonical_json_utf8_v1(scope.checkpoint_key)).hexdigest()
        checkpoint, event = advance_checkpoint(
            previous.get(key, baselines.get(key)),
            result,
            fired_at=boundary.as_of,
            source_sequence=source_sequence,
        )
        results.append(HistoricalStep(checkpoint, event_storage(event) if event else None))
    return tuple(results)
