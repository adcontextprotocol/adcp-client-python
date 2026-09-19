"""Optional durable work contracts, independent of the foundation store protocols."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, Protocol, get_args, runtime_checkable
from uuid import UUID

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import aware_utc, reporting_identifier
from adcp.reporting.ledger._delivery_state import latest_check
from adcp.reporting.ledger.delivery_models import (
    MaterializationFailure,
    ReportingDeliveryRecord,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
)
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.materializer._errors import ReportingMaterializerUsageError
from adcp.reporting.materializer.contracts import (
    ReportingDestinationRequest,
    ReportingPreparedRevision,
    ReportingVerificationKey,
    ReportingWriterFailure,
    failure,
)
from adcp.reporting.materializer.verification import (
    ReportingRevisionRowReader,
    ReportingVerifiedDestination,
    _same_definition,
)
from adcp.reporting.revision_selection import select_reporting_revision

MaterializerReason = Literal[
    "ready",
    "verified",
    "retry",
    "inactive",
    "revision_not_ready",
    "revision_unreadable",
    "target_changed",
    "history_corrupt",
    "legacy_pending",
    "legacy_terminal",
    "component_unavailable",
    "binding_changed",
    "effect_unknown",
    "operator_required",
]


def scope_id(scope: ReportingDeliveryScope) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(asdict(scope))).hexdigest()


def verification_key_id(key: ReportingVerificationKey) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(asdict(key))).hexdigest()


def key_for(
    binding: ReportingDestinationBinding,
    obligation: ReportingObligationRecord,
    keys: tuple[ReportingVerificationKey, ...],
    *,
    required: str | None = None,
) -> ReportingVerificationKey:
    matches = tuple(
        key
        for key in keys
        if (required is None or verification_key_id(key) == required)
        and _same_definition(key, obligation.definition)
        and (key.report_definition_id, key.reporting_profile)
        == (obligation.report_definition_id, obligation.reporting_profile)
        and (
            key.capability.method,
            key.capability.transport,
            key.capability.format,
            key.capability.verification_profile,
        )
        == (binding.method, binding.transport, binding.format, binding.verification_profile)
    )
    if len(matches) != 1:
        raise failure("UNSUPPORTED_VERIFICATION")
    return matches[0]


@dataclass(frozen=True)
class MaterializerContext:
    configuration: ReportingConfiguration
    obligation: ReportingObligationRecord
    binding: ReportingDestinationBinding
    delivery: ReportingObligationDeliveryRecord | None
    revisions: tuple[ReportingRevisionRecord, ...]
    records: tuple[ReportingDeliveryRecord, ...]

    @property
    def scope(self) -> ReportingDeliveryScope:
        return ReportingDeliveryScope(
            self.obligation.generation_key,
            self.binding.consumer_id,
            self.obligation.reporting_obligation_id,
        )

    def selection(self, now: datetime) -> tuple[ReportingRevisionRecord | None, MaterializerReason]:
        config = self.configuration
        if (
            config.activated_at is None
            or config.activated_at > now
            or (config.deactivated_at is not None and config.deactivated_at <= now)
        ):
            return None, "inactive"
        selected = select_reporting_revision(
            self.revisions,
            account_id=self.scope.principal.account_id,
            reporting_obligation_id=self.scope.reporting_obligation_id,
            required_finality=self.obligation.required_finality,
        )
        if selected.kind == "corrupt":
            return None, "history_corrupt"
        if selected.kind != "selected":
            return None, "revision_not_ready"
        if not selected.revision.readable:
            return selected.revision, "revision_unreadable"
        return selected.revision, "ready"

    def attempts(self, revision_id: str) -> tuple[ReportingMaterializationAttempt, ...]:
        return tuple(
            sorted(
                (
                    r
                    for r in self.records
                    if isinstance(r, ReportingMaterializationAttempt)
                    and r.scope == self.scope
                    and r.reporting_revision_id == revision_id
                ),
                key=lambda r: r.attempt,
            )
        )

    def outcome(
        self, attempt: ReportingMaterializationAttempt
    ) -> ReportingMaterializationRecord | None:
        return next(
            (
                r
                for r in self.records
                if isinstance(r, ReportingMaterializationRecord) and r.key == attempt.key
            ),
            None,
        )

    def pending_attempts(self) -> tuple[ReportingMaterializationAttempt, ...]:
        return tuple(
            r
            for r in self.records
            if isinstance(r, ReportingMaterializationAttempt)
            and r.scope == self.scope
            and self.outcome(r) is None
        )

    def resumable(self, attempt: ReportingMaterializationAttempt) -> bool:
        history = self.attempts(attempt.reporting_revision_id)
        return (
            bool(history)
            and history[-1] == attempt
            and tuple(a.attempt for a in history) == tuple(range(1, len(history) + 1))
            and self.pending_attempts() == (attempt,)
        )

    def retained_success(
        self, outcome: ReportingMaterializationRecord, now: datetime
    ) -> tuple[MaterializerReason, datetime | None]:
        check = latest_check(self.records, outcome.reporting_materialization_id, at=now)
        if (
            outcome.resource is None
            or outcome.resource.expires_at <= now
            or (check is not None and check.state != "readable")
        ):
            return "operator_required", None
        return "verified", outcome.resource.expires_at


@dataclass(frozen=True)
class ReportingMaterializerLease:
    """A short fenced reservation. The token grants no destination authorization."""

    scope: ReportingDeliveryScope
    generation: int
    token: str = field(repr=False)
    expires_at: datetime
    attempt: ReportingMaterializationAttempt
    request: ReportingDestinationRequest
    context: MaterializerContext
    notifications_enabled: bool
    admission_epoch: int = 0

    def __post_init__(self) -> None:
        invalid = False
        try:
            invalid = (
                type(self.generation) is not int
                or self.generation < 1
                or type(self.notifications_enabled) is not bool
                or type(self.admission_epoch) is not int
                or self.admission_epoch not in {0, 2}
                or UUID(self.token).version != 4
                or self.attempt.scope != self.scope
                or self.context.scope != self.scope
                or self.request
                != ReportingDestinationRequest.from_binding(
                    self.context.binding, self.attempt, self.request.verification_key
                )
            )
            object.__setattr__(self, "expires_at", aware_utc(self.expires_at))
        except (ValueError, TypeError, AttributeError):
            invalid = True
        if invalid:
            raise failure("BINDING_MISMATCH")


@dataclass(frozen=True)
class ReportingMaterializerTurn:
    state: Literal["idle", "discovered", "parked", "pending", "verified", "failed"]
    reason: MaterializerReason | None = None
    reporting_materialization_id: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"idle", "discovered", "parked", "pending", "verified", "failed"} or (
            self.reason is not None and self.reason not in get_args(MaterializerReason)
        ):
            raise ValueError("invalid materializer turn")
        if self.reporting_materialization_id is not None:
            reporting_identifier(self.reporting_materialization_id, maximum=255)


@runtime_checkable
class ReportingMaterializerStore(ReportingRevisionRowReader, Protocol):
    """Opt-in service primitives. Old structural store implementations stay valid."""

    async def claim_materialization(
        self, *, keys: tuple[ReportingVerificationKey, ...], lease_seconds: int = 30
    ) -> ReportingMaterializerLease | ReportingMaterializerTurn: ...

    async def renew_materialization(
        self, lease: ReportingMaterializerLease, *, lease_seconds: int
    ) -> bool: ...

    async def authorize_materialization(self, lease: ReportingMaterializerLease) -> None: ...

    async def finish_materialization(
        self,
        lease: ReportingMaterializerLease,
        *,
        prepared: ReportingPreparedRevision | None = None,
        verified: ReportingVerifiedDestination | None = None,
        error: ReportingWriterFailure | None = None,
    ) -> ReportingMaterializerTurn: ...


def validate_lease_seconds(value: int) -> None:
    if type(value) is not int or not 3 <= value <= 300:
        raise ReportingMaterializerUsageError("materializer leases require 3..300 seconds")


def public_failure(error: ReportingWriterFailure) -> MaterializationFailure:
    if error.code in {"DESTINATION_CORRUPT", "SOURCE_INVALID"}:
        return "CONTENT_CORRUPT"
    if error.code == "WRITE_FAILED":
        return "WRITE_FAILED"
    if error.code in {
        "RESOURCE_UNAVAILABLE",
        "CURRENT_REVISION_CHANGED",
        "REVISION_NOT_READY",
        "AUTHORIZATION_DENIED",
        "BINDING_MISMATCH",
        "HISTORY_CORRUPT",
    }:
        return "RESOURCE_UNAVAILABLE"
    return "VERIFICATION_FAILED"
