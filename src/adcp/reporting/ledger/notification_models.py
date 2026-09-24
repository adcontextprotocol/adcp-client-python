"""Closed logical notifications and typed, consumer-qualified status identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from adcp.reporting.evidence import (
    aware_utc,
    consumer_reference,
    principal_reference,
    reporting_identifier,
)
from adcp.reporting.ledger.delivery_models import _ClosedValue, _freeze_fields
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingFinality,
    ReportingHealth,
    ReportingIssueLifecycle,
    ReportingObligationRecord,
)
from adcp.validation.schema_loader import get_named_validator

NotificationType: TypeAlias = Literal[
    "reporting.ledger_changed", "reporting.delivery_ready", "reporting.status_changed"
]
FeedPurpose: TypeAlias = Literal["pacing", "analytics", "billing"]
DirtyReason: TypeAlias = Literal[
    "configuration",
    "obligation",
    "revision",
    "adjustment",
    "readability",
    "consumer_status",
    "issue",
    "destination",
    "materialization",
    "receipt",
    "clock",
]


class ReportingNotificationError(ValueError):
    """A sanitized local classification, never an external diagnostic."""

    def __init__(self, code: str = "invalid_notification") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RevisionPublished(_ClosedValue):
    reporting_revision_id: str
    finality: ReportingFinality
    supersedes_reporting_revision_id: str | None = None
    kind: Literal["revision_published"] = field(default="revision_published", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_revision_id, maximum=255)
        if self.supersedes_reporting_revision_id is not None:
            reporting_identifier(self.supersedes_reporting_revision_id, maximum=255)


@dataclass(frozen=True, slots=True)
class AdjustmentPublished(_ClosedValue):
    reporting_adjustment_id: str
    adjusts_reporting_revision_id: str
    kind: Literal["adjustment_published"] = field(default="adjustment_published", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_adjustment_id, maximum=255)
        reporting_identifier(self.adjusts_reporting_revision_id, maximum=255)


@dataclass(frozen=True, slots=True)
class MaterializationReady(_ClosedValue):
    """Snapshot of a verified, frozen Managed binding; never a capability string."""

    generation_key: ReportingConfigurationGenerationKey
    consumer_id: str
    reporting_obligation_id: str
    destination_ref: str
    method: Literal["file_transfer", "dataset_share", "warehouse_materialization"]
    reporting_revision_id: str
    reporting_materialization_id: str
    readiness: Literal["available", "delivered"]
    finality: ReportingFinality
    data_through: datetime | None
    feed_purpose: FeedPurpose
    kind: Literal["materialization_ready"] = field(default="materialization_ready", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        consumer_reference(self.consumer_id)
        for value in (
            self.reporting_obligation_id,
            self.reporting_revision_id,
            self.reporting_materialization_id,
        ):
            reporting_identifier(value, maximum=255)
        reporting_identifier(self.generation_key.delivery_config_id, maximum=64)
        if self.data_through is not None:
            object.__setattr__(self, "data_through", aware_utc(self.data_through))


@dataclass(frozen=True, slots=True)
class StatusChanged(_ClosedValue):
    """One locked scope checkpoint generation; private identity stays off the wire."""

    scope: ReportingStatusScope
    health: ReportingHealth
    fingerprint: str
    checkpoint_generation: int
    previous_health: ReportingHealth | None = None
    issue_ids: tuple[str, ...] = ()
    kind: Literal["status_changed"] = field(default="status_changed", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        if (
            self.scope.generation_key is None
            or self.scope.feed_purpose is None
            or self.checkpoint_generation < 1
            or len(self.fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in self.fingerprint)
            or tuple(sorted(set(self.issue_ids))) != self.issue_ids
            or len(self.issue_ids) > 16
            or (self.health in {"delayed", "action_required"} and not self.issue_ids)
        ):
            raise ReportingNotificationError("invalid_status_projection")
        for issue_id in self.issue_ids:
            reporting_identifier(issue_id)


NotificationCause: TypeAlias = (
    RevisionPublished | AdjustmentPublished | MaterializationReady | StatusChanged
)


@dataclass(frozen=True, slots=True)
class ReportingDomainEvent(_ClosedValue):
    account_id: str
    notification_id: str
    fired_at: datetime
    cause: NotificationCause
    cause_generation: int = 1

    def __post_init__(self) -> None:
        _freeze_fields(self)
        principal_reference(self.account_id)
        reporting_identifier(self.notification_id, maximum=255)
        object.__setattr__(self, "fired_at", aware_utc(self.fired_at))
        if self.cause_generation < 1:
            raise ReportingNotificationError()
        if isinstance(self.cause, MaterializationReady):
            if self.cause.generation_key.account_id != self.account_id:
                raise ReportingNotificationError()
        if isinstance(self.cause, StatusChanged) and (
            self.cause.scope.account_id != self.account_id
            or self.cause.checkpoint_generation != self.cause_generation
        ):
            raise ReportingNotificationError()

    @property
    def notification_type(self) -> NotificationType:
        if isinstance(self.cause, StatusChanged):
            return "reporting.status_changed"
        return (
            "reporting.delivery_ready"
            if isinstance(self.cause, MaterializationReady)
            else "reporting.ledger_changed"
        )

    @property
    def cause_id(self) -> str:
        cause = self.cause
        if isinstance(cause, RevisionPublished):
            return cause.reporting_revision_id
        if isinstance(cause, AdjustmentPublished):
            return cause.reporting_adjustment_id
        if isinstance(cause, StatusChanged):
            from adcp.reporting.canonical_json import canonical_json_utf8_v1

            return (
                "rpsc_"
                + hashlib.sha256(canonical_json_utf8_v1(cause.scope.checkpoint_key)).hexdigest()
            )
        # Structured encoding: consumers and materializations may reuse IDs.
        return json.dumps(
            [cause.consumer_id, cause.reporting_materialization_id], separators=(",", ":")
        )

    @property
    def consumer_namespace(self) -> str:
        if isinstance(self.cause, StatusChanged):
            return self.cause.scope.consumer_id or ""
        return self.cause.consumer_id if isinstance(self.cause, MaterializationReady) else ""

    @property
    def causal_key(self) -> tuple[str, str, str, str, str, int]:
        return (
            self.account_id,
            self.consumer_namespace,
            self.notification_type,
            self.cause.kind,
            self.cause_id,
            self.cause_generation,
        )

    def body(self, *, subscriber_id: str, idempotency_key: str) -> bytes:
        """Explicit wire allowlist, validated against all rc.3 conditionals."""
        from adcp.reporting.canonical_json import canonical_json_utf8_v1

        value: dict[str, Any] = {
            "account_id": self.account_id,
            "notification_id": self.notification_id,
            "notification_type": self.notification_type,
            "fired_at": iso(self.fired_at),
            "subscriber_id": subscriber_id,
            "idempotency_key": idempotency_key,
        }
        cause = self.cause
        if isinstance(cause, RevisionPublished):
            value.update(
                change_kind=cause.kind,
                reporting_revision_id=cause.reporting_revision_id,
                finality=cause.finality,
            )
            if cause.supersedes_reporting_revision_id is not None:
                value["supersedes_reporting_revision_id"] = cause.supersedes_reporting_revision_id
        elif isinstance(cause, AdjustmentPublished):
            value.update(
                change_kind=cause.kind,
                reporting_adjustment_id=cause.reporting_adjustment_id,
                adjusts_reporting_revision_id=cause.adjusts_reporting_revision_id,
            )
        elif isinstance(cause, StatusChanged):
            key = cause.scope.generation_key
            assert key is not None
            value.update(
                delivery_config_id=key.delivery_config_id,
                delivery_config_version=key.delivery_config_version,
                feed_purpose=cause.scope.feed_purpose,
                health=cause.health,
            )
            if cause.scope.reporting_obligation_id is not None:
                value["reporting_obligation_id"] = cause.scope.reporting_obligation_id
            if cause.previous_health is not None:
                value["previous_health"] = cause.previous_health
            if cause.issue_ids:
                value["issue_ids"] = list(cause.issue_ids)
        else:
            value.update(
                delivery_config_id=cause.generation_key.delivery_config_id,
                delivery_config_version=cause.generation_key.delivery_config_version,
                reporting_revision_id=cause.reporting_revision_id,
                reporting_materialization_id=cause.reporting_materialization_id,
                readiness=cause.readiness,
                finality=cause.finality,
                data_through=iso(cause.data_through) if cause.data_through is not None else None,
                feed_purpose=cause.feed_purpose,
            )
        validate_notification_payload(value)
        return canonical_json_utf8_v1(value)


@dataclass(frozen=True, slots=True)
class ReportingStatusScope(_ClosedValue):
    """A typed projection target. Missing detail means invalidate the account.

    Legacy issue keys are opaque. They are never parsed to infer a generation,
    obligation, consumer, or health. Adopters may supply this optional scope to
    the concrete stores' issue methods without changing ReportingLedgerStore.
    """

    account_id: str
    generation_key: ReportingConfigurationGenerationKey | None = None
    reporting_obligation_id: str | None = None
    consumer_id: str | None = None
    feed_purpose: FeedPurpose | None = None

    def __post_init__(self) -> None:
        _freeze_fields(self)
        principal_reference(self.account_id)
        if self.consumer_id is not None:
            consumer_reference(self.consumer_id)
        if self.reporting_obligation_id is not None:
            reporting_identifier(self.reporting_obligation_id, maximum=255)
        if self.generation_key is not None and self.generation_key.account_id != self.account_id:
            raise ReportingNotificationError("invalid_status_scope")

    @property
    def checkpoint_key(self) -> tuple[str, str, str, int, str, str]:
        """Six independent, non-null columns. Scope kinds never share an ID space."""
        if self.generation_key is None:
            raise ReportingNotificationError("invalid_status_scope")
        return (
            self.account_id,
            self.consumer_id or "",
            self.generation_key.delivery_config_id,
            self.generation_key.delivery_config_version,
            "obligation" if self.reporting_obligation_id is not None else "configuration",
            self.reporting_obligation_id or "",
        )

    @classmethod
    def for_obligation(
        cls, obligation: ReportingObligationRecord, consumer_id: str | None = None
    ) -> ReportingStatusScope:
        # Existing ledger fields are strings; validating through the closed
        # adapter refuses a provider-supplied feed label rather than echoing it.
        return decode_status_scope(
            {
                "account_id": obligation.account_id,
                "generation_key": asdict(obligation.generation_key),
                "reporting_obligation_id": obligation.reporting_obligation_id,
                "consumer_id": consumer_id,
                "feed_purpose": obligation.feed_purpose,
            }
        )


def validate_scope_refinement(
    existing: ReportingStatusScope | None, requested: ReportingStatusScope
) -> None:
    """Only fill unknown detail within the same account and consumer.

    Ownership of added generation/obligation fields must additionally be checked
    by the connection-bound store before it persists the refinement.
    """
    if existing is not None and (
        existing.account_id != requested.account_id
        or existing.consumer_id != requested.consumer_id
        or any(
            getattr(existing, name) is not None
            and getattr(existing, name) != getattr(requested, name)
            for name in ("generation_key", "reporting_obligation_id", "feed_purpose")
        )
    ):
        raise ReportingNotificationError("invalid_status_scope")


@dataclass(frozen=True, slots=True)
class ReportingStatusEvidence(_ClosedValue):
    """Replay evidence for one status mutation, containing no adopter prose.

    Immutable records are referenced in their account/consumer namespace. The
    mutable inputs (readability and issue lifecycle) are snapshotted on both
    sides, so rapid reversals cannot disappear before a projector runs.
    """

    record_kind: Literal[
        "configuration",
        "obligation",
        "revision",
        "adjustment",
        "consumer_status",
        "issue",
        "destination_binding",
        "obligation_delivery",
        "materialization_attempt",
        "materialization",
        "materialization_check",
        "revision_receipt",
        "adjustment_receipt",
        "clock",
    ]
    record_id: str
    record_version: int = 1
    readable: bool | None = None
    issue_state: Literal["open", "acknowledged", "waived", "resolved"] | None = None
    opened_at: datetime | None = None
    retired_at: datetime | None = None
    supersedes_id: str | None = None
    activated_at: datetime | None = None
    deactivated_at: datetime | None = None
    automated_recovery_seconds: float | None = None
    status_retention_days: int | None = None

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.record_id, maximum=255)
        if self.record_version < 1:
            raise ReportingNotificationError("invalid_status_evidence")
        for name in ("opened_at", "retired_at", "activated_at", "deactivated_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, aware_utc(value))


@dataclass(frozen=True, slots=True)
class ReportingStatusDirty(_ClosedValue):
    sequence: int
    scope: ReportingStatusScope
    reason: DirtyReason
    changed_at: datetime
    cause_id: str
    cause_generation: int
    before: ReportingStatusEvidence | None = None
    after: ReportingStatusEvidence | None = None

    def __post_init__(self) -> None:
        _freeze_fields(self)
        object.__setattr__(self, "changed_at", aware_utc(self.changed_at))
        if self.sequence < 1 or self.cause_generation < 1:
            raise ReportingNotificationError("invalid_status_evidence")


def issue_evidence(issue: ReportingIssueLifecycle) -> ReportingStatusEvidence:
    # Attribute allowlist: issue_key and external_ref are intentionally absent.
    return ReportingStatusEvidence(
        "issue",
        issue.issue_id,
        issue.generation,
        issue_state=issue.issue_state,
        opened_at=issue.opened_at,
        retired_at=issue.retired_at,
    )


def configuration_evidence(configuration: ReportingConfiguration) -> ReportingStatusEvidence:
    return ReportingStatusEvidence(
        "configuration",
        configuration.delivery_config_id,
        configuration.delivery_config_version,
        activated_at=configuration.activated_at,
        deactivated_at=configuration.deactivated_at,
        automated_recovery_seconds=configuration.automated_recovery_window.total_seconds(),
        status_retention_days=configuration.status_retention_days,
    )


def iso(value: datetime) -> str:
    return aware_utc(value).isoformat().replace("+00:00", "Z")


def event_storage(event: ReportingDomainEvent) -> dict[str, Any]:
    return dict(json.loads(json.dumps(asdict(event), default=iso)))


_EVENT = TypeAdapter(ReportingDomainEvent)
_SCOPE = TypeAdapter(ReportingStatusScope)
_DIRTY = TypeAdapter(ReportingStatusDirty)


def dirty_storage(record: ReportingStatusDirty) -> dict[str, Any]:
    return dict(json.loads(json.dumps(asdict(record), default=iso)))


def decode_dirty(value: object) -> ReportingStatusDirty:
    try:
        record = _DIRTY.validate_python(value)
        if dirty_storage(record) == value:
            return record
    except (ValidationError, ValueError, TypeError):
        # Leave the parsing exception context before emitting the closed classification.
        pass
    raise ReportingNotificationError("invalid_status_evidence") from None


def decode_event(value: object) -> ReportingDomainEvent:
    try:
        event = _EVENT.validate_python(value)
        if event_storage(event) == value:
            event.body(subscriber_id="validation", idempotency_key="0" * 32)
            return event
    except (ValidationError, ValueError, TypeError):
        # Leave the parsing exception context before emitting the closed classification.
        pass
    raise ReportingNotificationError("invalid_event") from None


def decode_status_scope(value: object) -> ReportingStatusScope:
    try:
        scope = _SCOPE.validate_python(value)
        if asdict(scope) == value:
            return scope
    except (ValidationError, ValueError, TypeError):
        # Leave the parsing exception context before emitting the closed classification.
        pass
    raise ReportingNotificationError("invalid_status_scope") from None


def new_event(account_id: str, cause: NotificationCause, at: datetime) -> ReportingDomainEvent:
    event = ReportingDomainEvent(account_id, str(uuid4()), at, cause)
    event.body(subscriber_id="validation", idempotency_key="0" * 32)
    return event


_SCHEMAS = {
    "reporting.ledger_changed": "core/reporting-ledger-changed-webhook.json",
    "reporting.delivery_ready": "core/reporting-delivery-ready-webhook.json",
    # Validation is useful to #1168B; acceptance here does not enable emission.
    "reporting.status_changed": "core/reporting-status-changed-webhook.json",
}


def validate_notification_payload(value: object) -> None:
    """Offline full-schema gate, additionally excluding extension metadata."""
    if not isinstance(value, dict) or "ext" in value:
        raise ReportingNotificationError("invalid_payload")
    notification_type = value.get("notification_type")
    path = _SCHEMAS.get(notification_type) if isinstance(notification_type, str) else None
    if path is None:
        raise ReportingNotificationError("invalid_payload")

    def scan(item: object) -> None:
        if isinstance(item, str):
            principal_reference(item)
        elif isinstance(item, list):
            for child in item:
                scan(child)
        elif isinstance(item, dict):
            for child in item.values():
                scan(child)

    try:
        scan(value)
    except ValueError:
        raise ReportingNotificationError("invalid_payload") from None
    validator = get_named_validator(path)
    if validator is None or not validator.is_valid(value):
        raise ReportingNotificationError("invalid_payload")
