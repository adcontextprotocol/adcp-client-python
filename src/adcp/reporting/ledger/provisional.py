"""Durable scheduling identities and immutable provisional observation metadata.

These records are private persistence metadata, not new reporting wire fields.
A source execution's semantic request is frozen; its deadline is an attempt budget.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from adcp.reporting.ledger.finality import ReportingStabilizedPolicy
from adcp.reporting.source import ReportingSourceSliceRequestV1

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import (
        ReportingConfigurationGenerationKey,
        ReportingObligationRecord,
        ReportingRevisionRecord,
    )
    from adcp.reporting.source import SourceBatchManifestV1


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provisional observation timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProvisionalPolicy:
    window: timedelta
    cadence: timedelta
    official_close_lag: timedelta | None = None
    until_stabilized: bool = False

    def __post_init__(self) -> None:
        if self.window < timedelta(0) or self.cadence <= timedelta(0):
            raise ValueError("provisional window must be nonnegative and cadence positive")
        if self.official_close_lag is not None and self.official_close_lag < timedelta(0):
            raise ValueError("official close lag must be nonnegative")

    def to_wire(self) -> dict[str, int | None]:
        return {
            **({"until_stabilized": True} if self.until_stabilized else {}),
            "window_us": self.window // timedelta(microseconds=1),
            "cadence_us": self.cadence // timedelta(microseconds=1),
            "official_close_lag_us": (
                self.official_close_lag // timedelta(microseconds=1)
                if self.official_close_lag is not None
                else None
            ),
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> ProvisionalPolicy:
        lag = value["official_close_lag_us"]
        return cls(
            timedelta(microseconds=value["window_us"]),
            timedelta(microseconds=value["cadence_us"]),
            timedelta(microseconds=lag) if lag is not None else None,
            value.get("until_stabilized", False),
        )


@dataclass(frozen=True)
class ProvisionalAcquisition:
    request_json: str
    ordinal: int
    policy: ProvisionalPolicy
    predecessor_revision_id: str | None = None
    previous_boundary: datetime | None = None
    output_finality: Literal["official"] | None = None

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("observation ordinal must be a nonnegative integer")
        self.request()
        if self.output_finality not in {None, "official"}:
            raise ValueError("invalid provisional acquisition output finality")
        if self.previous_boundary is not None:
            utc(self.previous_boundary)

    def request(self, *, deadline_at: datetime | None = None) -> ReportingSourceSliceRequestV1:
        # Parse on every access: callers cannot mutate the durable request's lists.
        request = ReportingSourceSliceRequestV1.model_validate_json(self.request_json)
        if deadline_at is not None:
            request = request.model_copy(update={"deadline_at": utc(deadline_at)})
        return request

    @property
    def account_id(self) -> str:
        return self.request().identity.account_id

    @property
    def obligation_id(self) -> str:
        return self.request().identity.reporting_obligation_id

    @property
    def execution_key(self) -> str:
        return self.request().identity.source_execution_key

    @property
    def finality(self) -> Literal["snapshot", "official"]:
        if self.output_finality or self.request().publication_class == "AUTHORITATIVE":
            return "official"
        return "snapshot"

    @property
    def legacy_stabilized_close(self) -> bool:
        return (
            self.output_finality == "official"
            and not self.policy.until_stabilized
            and self.request().publication_class == "PROVISIONAL_SNAPSHOT"
        )

    @property
    def is_scheduled_snapshot(self) -> bool:
        request = self.request()
        return (
            request.trigger == "scheduled_poll"
            and request.publication_class == "PROVISIONAL_SNAPSHOT"
            and self.output_finality is None
        )

    def requires_snapshot_origin(
        self, obligation: ReportingObligationRecord, *, explicitly_tracked: bool = False
    ) -> bool:
        definition = obligation.definition
        return self.policy.until_stabilized or (
            definition is not None
            and isinstance(definition.finality_policy, ReportingStabilizedPolicy)
            and (
                explicitly_tracked
                or obligation.required_finality == "official"
                or self.policy.official_close_lag is not None
                or not self.is_scheduled_snapshot
            )
        )

    def stabilization_schedule(
        self, required_finality: str, *, pending: bool
    ) -> ProvisionalPolicy | None:
        if not self.is_scheduled_snapshot and not (
            pending and self.legacy_stabilized_close and self.request().trigger == "scheduled_poll"
        ):
            return None
        policy = self.policy
        if (
            required_finality == "official"
            or policy.until_stabilized
            or policy.official_close_lag is not None
        ):
            return policy
        return None

    def observation_deadlines(
        self, checked_at: datetime, manifest: SourceBatchManifestV1
    ) -> tuple[datetime, datetime | None]:
        boundary = utc(
            manifest.finality_evidence.provisional_until
            or (utc(manifest.period.end) + self.policy.window)
        )
        if self.finality != "snapshot" or (
            not self.policy.until_stabilized and checked_at >= boundary
        ):
            return boundary, None
        next_poll = utc(checked_at) + self.policy.cadence
        if self.policy.until_stabilized:
            return boundary, next_poll
        return boundary, min(next_poll, boundary)

    def binds(self, obligation: ReportingObligationRecord) -> bool:
        request = self.request()
        identity = request.identity
        return (
            identity.account_id == obligation.account_id
            and identity.consumer_id == obligation.consumer_id
            and identity.reporting_obligation_id == obligation.reporting_obligation_id
            and identity.delivery_config_id == obligation.delivery_config_id
            and identity.delivery_config_version == obligation.delivery_config_version
            and identity.report_definition_id == obligation.report_definition_id
            and request.period.start == obligation.period.start
            and request.period.end == obligation.period.end
            and request.currency == obligation.currency
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "request": json.loads(self.request_json),
            **({"output_finality": self.output_finality} if self.output_finality else {}),
            "ordinal": self.ordinal,
            "policy": self.policy.to_wire(),
            "predecessor_revision_id": self.predecessor_revision_id,
            "previous_boundary": (
                utc(self.previous_boundary).isoformat()
                if self.previous_boundary is not None
                else None
            ),
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> ProvisionalAcquisition:
        request = dict(value["request"])
        request.pop("require_finality_evidence", None)
        return cls(
            json.dumps(request, sort_keys=True, separators=(",", ":")),
            value["ordinal"],
            ProvisionalPolicy.from_wire(value["policy"]),
            value["predecessor_revision_id"],
            (
                datetime.fromisoformat(value["previous_boundary"])
                if value["previous_boundary"] is not None
                else None
            ),
            value.get("output_finality"),
        )


@dataclass(frozen=True)
class ProvisionalObservation:
    acquisition: ProvisionalAcquisition
    revision_id: str
    checked_at: datetime
    provisional_until: datetime
    next_due_at: datetime | None
    manifest_json: str
    revision_publication_id: str | None = None

    @property
    def is_scheduled(self) -> bool:
        return self.acquisition.request().trigger == "scheduled_poll"

    @property
    def is_scheduled_snapshot(self) -> bool:
        return self.acquisition.is_scheduled_snapshot

    @property
    def next_stabilization_due_at(self) -> datetime:
        return self.next_due_at or self.checked_at + self.acquisition.policy.cadence

    @property
    def snapshot_origin(self) -> str | None:
        if self.acquisition.predecessor_revision_id != self.revision_id:
            origin = json.loads(self.manifest_json).get("publication_id")
            if isinstance(origin, str):
                return origin
        return self.revision_publication_id

    def bind_revision(
        self, revision: ReportingRevisionRecord, *, require_origin: bool = False
    ) -> ProvisionalObservation:
        """Keep the full originating publication ID when an unchanged read reuses a snapshot."""
        if revision.finality == "snapshot" and (
            require_origin
            or self.acquisition.policy.until_stabilized
            or (
                self.acquisition.predecessor_revision_id == revision.reporting_revision_id
                and revision.source_publication_id is not None
            )
        ):
            if (
                self.revision_publication_id is not None
                and self.revision_publication_id != revision.source_publication_id
            ):
                from adcp.reporting.ledger.store import LedgerConflictError

                raise LedgerConflictError(
                    "REVISION_IMMUTABLE", "observation snapshot origin differs"
                )
            return replace(
                self,
                revision_id=revision.reporting_revision_id,
                revision_publication_id=revision.source_publication_id,
            )
        return self

    @classmethod
    def from_manifest(
        cls,
        acquisition: ProvisionalAcquisition,
        revision_id: str,
        checked_at: datetime,
        manifest: SourceBatchManifestV1,
    ) -> ProvisionalObservation:
        checked_at = utc(checked_at)
        boundary, next_due = acquisition.observation_deadlines(checked_at, manifest)
        return cls(
            acquisition, revision_id, checked_at, boundary, next_due, manifest.model_dump_json()
        )

    def __post_init__(self) -> None:
        utc(self.checked_at)
        utc(self.provisional_until)
        if self.next_due_at is not None:
            utc(self.next_due_at)
        object.__setattr__(
            self,
            "manifest_json",
            json.dumps(json.loads(self.manifest_json), sort_keys=True, separators=(",", ":")),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "acquisition": self.acquisition.to_wire(),
            "revision_id": self.revision_id,
            **(
                {"revision_publication_id": self.revision_publication_id}
                if self.revision_publication_id is not None
                else {}
            ),
            "checked_at": utc(self.checked_at).isoformat(),
            "provisional_until": utc(self.provisional_until).isoformat(),
            "next_due_at": (
                utc(self.next_due_at).isoformat() if self.next_due_at is not None else None
            ),
            "manifest": json.loads(self.manifest_json),
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> ProvisionalObservation:
        return cls(
            ProvisionalAcquisition.from_wire(value["acquisition"]),
            value["revision_id"],
            datetime.fromisoformat(value["checked_at"]),
            datetime.fromisoformat(value["provisional_until"]),
            (
                datetime.fromisoformat(value["next_due_at"])
                if value["next_due_at"] is not None
                else None
            ),
            json.dumps(value["manifest"], sort_keys=True, separators=(",", ":")),
            value.get("revision_publication_id"),
        )


@runtime_checkable
class SnapshotOriginStore(Protocol):
    """Check a revision against the first retained witness of its full publication ID."""

    async def verify_snapshot_origin(self, revision: ReportingRevisionRecord) -> None: ...


@runtime_checkable
class StabilizationScheduleStore(Protocol):
    """Optional generation-scoped lookup of an admitted scheduled snapshot's policy."""

    async def get_stabilization_schedule(
        self, generation: ReportingConfigurationGenerationKey
    ) -> ProvisionalPolicy | None: ...


@runtime_checkable
class PendingProvisionalAcquisitionStore(Protocol):
    """Optional lookup of a frozen acquisition before selecting its readiness window."""

    async def get_provisional_acquisition(
        self, *, account_id: str, reporting_obligation_id: str, ordinal: int
    ) -> ProvisionalAcquisition | None:
        pass


@runtime_checkable
class ProvisionalObservationPublicationStore(Protocol):
    """Reserve, read the latest, and commit scheduled snapshot observations."""

    async def reserve_provisional_acquisition(
        self, acquisition: ProvisionalAcquisition
    ) -> ProvisionalAcquisition:
        pass

    async def get_provisional_observation(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> ProvisionalObservation | None:
        pass

    async def commit_provisional_observation(
        self,
        observation: ProvisionalObservation,
        revision: ReportingRevisionRecord | None,
        rows: Sequence[dict[str, Any]],
    ) -> ReportingRevisionRecord:
        pass


@runtime_checkable
class ProvisionalObservationStore(ProvisionalObservationPublicationStore, Protocol):
    async def list_provisional_observations(
        self,
        *,
        account_id: str,
        reporting_obligation_id: str,
        limit: int,
        scheduled_snapshots_only: bool = False,
    ) -> tuple[ProvisionalObservation, ...]:
        """Return up to ``limit`` latest observations in ascending ordinal order.

        The read is scoped to the requested account and reporting obligation.
        A scheduled snapshot filter applies before the limit.
        ``limit`` must be a positive integer.
        """
        ...


@runtime_checkable
class StabilizationStore(
    ProvisionalObservationStore,
    SnapshotOriginStore,
    PendingProvisionalAcquisitionStore,
    StabilizationScheduleStore,
    Protocol,
):
    """Durable evidence and recovery methods required for stabilized publication."""
