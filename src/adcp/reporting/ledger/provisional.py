"""Durable scheduling identities and immutable provisional observation metadata.

These records are private persistence metadata, not new reporting wire fields.
A source execution's semantic request is frozen; its deadline is an attempt budget.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from adcp.reporting.source import ReportingSourceSliceRequestV1

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import ReportingObligationRecord, ReportingRevisionRecord


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provisional observation timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProvisionalPolicy:
    window: timedelta
    cadence: timedelta
    official_close_lag: timedelta | None = None

    def __post_init__(self) -> None:
        if self.window < timedelta(0) or self.cadence <= timedelta(0):
            raise ValueError("provisional window must be nonnegative and cadence positive")
        if self.official_close_lag is not None and self.official_close_lag < timedelta(0):
            raise ValueError("official close lag must be nonnegative")

    def to_wire(self) -> dict[str, int | None]:
        return {
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
        )


@dataclass(frozen=True)
class ProvisionalAcquisition:
    request_json: str
    ordinal: int
    policy: ProvisionalPolicy
    predecessor_revision_id: str | None = None
    previous_boundary: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("observation ordinal must be a nonnegative integer")
        self.request()
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

    def binds(self, obligation: ReportingObligationRecord) -> bool:
        request = self.request()
        identity = request.identity
        return (
            identity.account_id == obligation.account_id
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
        return cls(
            json.dumps(value["request"], sort_keys=True, separators=(",", ":")),
            value["ordinal"],
            ProvisionalPolicy.from_wire(value["policy"]),
            value["predecessor_revision_id"],
            (
                datetime.fromisoformat(value["previous_boundary"])
                if value["previous_boundary"] is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ProvisionalObservation:
    acquisition: ProvisionalAcquisition
    revision_id: str
    checked_at: datetime
    provisional_until: datetime
    next_due_at: datetime | None
    manifest_json: str

    def __post_init__(self) -> None:
        utc(self.checked_at)
        utc(self.provisional_until)
        if self.next_due_at is not None:
            utc(self.next_due_at)

    def to_wire(self) -> dict[str, Any]:
        return {
            "acquisition": self.acquisition.to_wire(),
            "revision_id": self.revision_id,
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
        )


@runtime_checkable
class ProvisionalObservationStore(Protocol):
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
        revision: ReportingRevisionRecord,
        rows: Sequence[dict[str, Any]],
    ) -> ReportingRevisionRecord:
        pass
