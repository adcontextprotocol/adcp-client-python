"""Finality policies selected from a content-addressed report definition."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from adcp.reporting.source import iso_duration_milliseconds_v1

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import ReportingObligationRecord
    from adcp.reporting.source import SourceBatchManifestV1

__all__ = [
    "FinalityProof",
    "ReportingContractualCutoffPolicy",
    "ReportingFinalityPolicy",
    "ReportingSourceFinalPolicy",
    "ReportingStabilizedPolicy",
    "contractual_cutoff_at",
    "finality_policy_source_class",
    "manifest_finality_proof",
    "reporting_finality_policy_from_storage",
    "source_finality_proof",
]


def _validate_policy_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value) is None:
        raise ValueError("finality_policy_id must match [A-Za-z0-9_.:-]{1,255}")


@dataclass(frozen=True)
class ReportingSourceFinalPolicy:
    """Close on an authoritative source publication with the declared signal."""

    finality_policy_id: str
    source_signal: str
    basis: Literal["source_final"] = field(default="source_final", init=False)

    def __post_init__(self) -> None:
        _validate_policy_id(self.finality_policy_id)
        if not isinstance(self.source_signal, str) or not 1 <= len(self.source_signal) <= 512:
            raise ValueError("source_signal must contain 1 to 512 characters")

    def to_storage(self) -> dict[str, str]:
        return {
            "finality_policy_id": self.finality_policy_id,
            "basis": self.basis,
            "source_signal": self.source_signal,
        }


@dataclass(frozen=True)
class ReportingContractualCutoffPolicy:
    """Close on verified content observed after a fixed period-end cutoff."""

    finality_policy_id: str
    duration_after_period_end: str
    basis: Literal["contractual_cutoff"] = field(default="contractual_cutoff", init=False)

    def __post_init__(self) -> None:
        _validate_policy_id(self.finality_policy_id)
        iso_duration_milliseconds_v1(self.duration_after_period_end)

    def to_storage(self) -> dict[str, str]:
        return {
            "finality_policy_id": self.finality_policy_id,
            "basis": self.basis,
            "duration_after_period_end": self.duration_after_period_end,
        }


@dataclass(frozen=True)
class ReportingStabilizedPolicy:
    """Close when verified content has remained unchanged for the required interval."""

    finality_policy_id: str
    minimum_age: str
    unchanged_for: str
    basis: Literal["stabilized"] = field(default="stabilized", init=False)

    def __post_init__(self) -> None:
        _validate_policy_id(self.finality_policy_id)
        iso_duration_milliseconds_v1(self.minimum_age)
        iso_duration_milliseconds_v1(self.unchanged_for)

    def to_storage(self) -> dict[str, str]:
        return {
            "finality_policy_id": self.finality_policy_id,
            "basis": self.basis,
            "minimum_age": self.minimum_age,
            "unchanged_for": self.unchanged_for,
        }


ReportingFinalityPolicy = (
    ReportingSourceFinalPolicy | ReportingContractualCutoffPolicy | ReportingStabilizedPolicy
)


@dataclass(frozen=True)
class FinalityProof:
    """The selected policy and the instant its publication requirements were met."""

    finality_policy_id: str
    basis: Literal["source_final", "contractual_cutoff", "stabilized"]
    finalized_at: datetime

    def validate(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        created_at: datetime,
    ) -> None:
        from adcp.reporting.ledger.store import LedgerConflictError

        if self.finalized_at.utcoffset() is None or not (
            obligation.period.end.astimezone(timezone.utc)
            <= self.finalized_at.astimezone(timezone.utc)
            <= created_at.astimezone(timezone.utc)
        ):
            raise LedgerConflictError(
                "PUBLICATION_TIME_INVALID", "proven finality is outside the publication time bounds"
            )
        if self != manifest_finality_proof(obligation, manifest, created_at):
            raise LedgerConflictError(
                "FINALITY_POLICY_UNPROVEN", "proof differs from the selected policy or its evidence"
            )


def contractual_cutoff_at(
    obligation: ReportingObligationRecord, policy: ReportingContractualCutoffPolicy
) -> datetime:
    return obligation.period.end.astimezone(timezone.utc) + timedelta(
        milliseconds=iso_duration_milliseconds_v1(policy.duration_after_period_end)
    )


def source_finality_proof(
    obligation: ReportingObligationRecord,
    manifest: SourceBatchManifestV1,
    policy: ReportingSourceFinalPolicy,
) -> FinalityProof:
    from adcp.reporting.ledger.store import LedgerConflictError

    evidence = manifest.finality_evidence
    if (
        manifest.publication_class != "AUTHORITATIVE"
        or evidence.basis not in {"provider_declared", "provider_job_terminal"}
        or evidence.evidence_ref != policy.source_signal
        or evidence.observed_at.astimezone(timezone.utc)
        < obligation.period.end.astimezone(timezone.utc)
    ):
        raise LedgerConflictError(
            "MANIFEST_MISMATCH", "authoritative evidence must prove the selected source signal"
        )
    return FinalityProof(policy.finality_policy_id, policy.basis, evidence.observed_at)


def manifest_finality_proof(
    obligation: ReportingObligationRecord, manifest: SourceBatchManifestV1, published_at: datetime
) -> FinalityProof:
    """Verify the policy that a single complete source publication can prove."""
    from adcp.reporting.ledger.store import LedgerConflictError

    if (
        manifest.coverage.status != "full"
        or manifest.period.start != obligation.period.start
        or manifest.period.end != obligation.period.end
    ):
        raise LedgerConflictError("MANIFEST_MISMATCH", "official proof requires the full period")
    policy = obligation.definition.finality_policy if obligation.definition else None
    if isinstance(policy, ReportingSourceFinalPolicy):
        return source_finality_proof(obligation, manifest, policy)
    if isinstance(policy, ReportingContractualCutoffPolicy):
        cutoff = contractual_cutoff_at(obligation, policy)
        if (
            manifest.publication_class != "PROVISIONAL_SNAPSHOT"
            or not cutoff <= manifest.period.source_read_cutoff_at <= published_at
            or manifest.observed_at < cutoff
        ):
            raise LedgerConflictError(
                "FINALITY_POLICY_UNPROVEN", "contractual cutoff requires a post-cutoff snapshot"
            )
        try:
            manifest.require_full_period_watermarks()
        except ValueError as error:
            raise LedgerConflictError("FINALITY_POLICY_UNPROVEN", str(error)) from error
        return FinalityProof(policy.finality_policy_id, policy.basis, published_at)
    raise LedgerConflictError("FINALITY_POLICY_UNPROVEN", "official finality requires policy proof")


def finality_policy_source_class(
    policy: ReportingFinalityPolicy,
) -> Literal["snapshot", "official"]:
    """Return the source publication class needed to prove the selected policy."""
    return "official" if isinstance(policy, ReportingSourceFinalPolicy) else "snapshot"


def reporting_finality_policy_from_storage(value: Mapping[str, Any]) -> ReportingFinalityPolicy:
    """Decode the exact stored fields for a supported policy basis."""
    match value:
        case {
            "finality_policy_id": str(policy_id),
            "basis": "source_final",
            "source_signal": str(source_signal),
            **extra,
        } if not extra:
            return ReportingSourceFinalPolicy(policy_id, source_signal)
        case {
            "finality_policy_id": str(policy_id),
            "basis": "contractual_cutoff",
            "duration_after_period_end": str(duration),
            **extra,
        } if not extra:
            return ReportingContractualCutoffPolicy(policy_id, duration)
        case {
            "finality_policy_id": str(policy_id),
            "basis": "stabilized",
            "minimum_age": str(minimum_age),
            "unchanged_for": str(unchanged_for),
            **extra,
        } if not extra:
            return ReportingStabilizedPolicy(policy_id, minimum_age, unchanged_for)
        case _:
            raise ValueError("invalid reporting finality policy storage payload")
