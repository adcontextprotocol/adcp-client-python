"""Finality policies selected from a content-addressed report definition."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from adcp.reporting.source import iso_duration_milliseconds_v1

__all__ = [
    "ReportingContractualCutoffPolicy",
    "ReportingFinalityPolicy",
    "ReportingSourceFinalPolicy",
    "ReportingStabilizedPolicy",
    "finality_policy_source_class",
    "reporting_finality_policy_from_storage",
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
