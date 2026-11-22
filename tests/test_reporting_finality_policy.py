"""Private selected finality policies validate and round-trip through ledger storage."""

from __future__ import annotations

from dataclasses import replace

import pytest

from adcp.reporting.ledger import (
    ReportingContractualCutoffPolicy,
    ReportingDefinitionBinding,
    ReportingFinalityPolicy,
    ReportingSourceFinalPolicy,
    ReportingStabilizedPolicy,
    reporting_finality_policy_from_storage,
)
from adcp.reporting.ledger._record_rows import _definition_from_payload

DEFINITION = ReportingDefinitionBinding(
    report_definition_uri="https://contracts.example.test/reporting/daily-delivery-v1",
    report_definition_sha256="a" * 64,
    schema_version="1.0.0",
    schema_uri="https://contracts.example.test/reporting/rows-v1.json",
    schema_sha256="b" * 64,
)


def test_definition_binding_retains_selected_finality_policy_off_wire() -> None:
    policy = ReportingStabilizedPolicy(
        finality_policy_id="daily-stable-v1",
        minimum_age="P2D",
        unchanged_for="PT12H",
    )
    binding = replace(DEFINITION, finality_policy=policy)

    assert binding.to_wire() == DEFINITION.to_wire()
    assert binding.to_storage()["finality_policy"] == {
        "finality_policy_id": "daily-stable-v1",
        "basis": "stabilized",
        "minimum_age": "P2D",
        "unchanged_for": "PT12H",
    }
    assert _definition_from_payload(binding.to_storage()) == binding


@pytest.mark.parametrize("value", ["P1M", "P1Y", "not-a-duration", "PT1H\n"])
def test_finality_policy_refuses_unsupported_duration(value: str) -> None:
    with pytest.raises(ValueError, match="supported reporting duration"):
        ReportingContractualCutoffPolicy("cutoff-v1", value)


@pytest.mark.parametrize("field", ["duration_after_period_end", "minimum_age", "unchanged_for"])
def test_finality_policy_rejects_durations_beyond_the_datetime_range(field: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        if field == "duration_after_period_end":
            ReportingContractualCutoffPolicy("cutoff", "P99999999D")
        elif field == "minimum_age":
            ReportingStabilizedPolicy("stable", "P99999999D", "PT1H")
        else:
            ReportingStabilizedPolicy("stable", "PT1H", "P99999999D")


@pytest.mark.parametrize(
    ("policy", "payload"),
    [
        (
            ReportingSourceFinalPolicy("daily.source:v1", "source_closed"),
            {
                "finality_policy_id": "daily.source:v1",
                "basis": "source_final",
                "source_signal": "source_closed",
            },
        ),
        (
            ReportingContractualCutoffPolicy("cutoff-v1", "PT4H"),
            {
                "finality_policy_id": "cutoff-v1",
                "basis": "contractual_cutoff",
                "duration_after_period_end": "PT4H",
            },
        ),
    ],
)
def test_finality_policy_storage_round_trip(
    policy: ReportingFinalityPolicy, payload: dict[str, str]
) -> None:
    assert policy.to_storage() == payload
    assert reporting_finality_policy_from_storage(payload) == policy


@pytest.mark.parametrize(
    "payload",
    [
        {"finality_policy_id": "daily-v1", "basis": "unknown"},
        {"finality_policy_id": "daily-v1", "basis": "source_final"},
        {"finality_policy_id": "daily-v1", "basis": "source_final", "source_signal": ["close"]},
        {
            "finality_policy_id": "daily-v1",
            "basis": "source_final",
            "source_signal": "close",
            "duration_after_period_end": "PT4H",
        },
        None,
    ],
)
def test_definition_binding_rejects_invalid_finality_policy_storage(payload: object) -> None:
    with pytest.raises(ValueError, match="finality policy"):
        _definition_from_payload({**DEFINITION.to_storage(), "finality_policy": payload})


def test_definition_binding_reads_storage_without_a_finality_policy() -> None:
    assert DEFINITION.to_storage() == DEFINITION.to_wire()
    assert _definition_from_payload(DEFINITION.to_storage()) == DEFINITION


def test_definition_binding_rejects_finality_policy_subclass() -> None:
    class CustomPolicy(ReportingSourceFinalPolicy):
        pass

    with pytest.raises(ValueError, match="finality_policy"):
        replace(DEFINITION, finality_policy=CustomPolicy("daily-v1", "close"))


@pytest.mark.parametrize("policy_id", ["", "a" * 256, "contains spaces", "close\n"])
def test_finality_policy_refuses_invalid_identifier(policy_id: str) -> None:
    with pytest.raises(ValueError, match="finality_policy_id"):
        ReportingSourceFinalPolicy(policy_id, "close")


@pytest.mark.parametrize("source_signal", ["", "a" * 513])
def test_source_finality_policy_refuses_invalid_signal(source_signal: str) -> None:
    with pytest.raises(ValueError, match="source_signal must contain 1 to 512 characters"):
        ReportingSourceFinalPolicy("daily-v1", source_signal)
