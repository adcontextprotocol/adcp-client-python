"""Retaining each official finality policy on a definition binding."""

from typing_extensions import assert_type

from adcp.reporting.ledger import (
    ReportingContractualCutoffPolicy,
    ReportingDefinitionBinding,
    ReportingFinalityPolicy,
    ReportingSourceFinalPolicy,
    ReportingStabilizedPolicy,
)


def definition_with_policy(policy: ReportingFinalityPolicy) -> ReportingDefinitionBinding:
    binding = ReportingDefinitionBinding(
        report_definition_uri="https://contracts.example.test/reporting/daily-delivery-v1",
        report_definition_sha256="a" * 64,
        schema_version="1.0.0",
        schema_uri="https://contracts.example.test/reporting/rows-v1.json",
        schema_sha256="b" * 64,
        finality_policy=policy,
    )
    assert_type(binding.finality_policy, ReportingFinalityPolicy | None)
    return binding


source: ReportingFinalityPolicy = ReportingSourceFinalPolicy("source-v1", "close")
cutoff: ReportingFinalityPolicy = ReportingContractualCutoffPolicy("cutoff-v1", "PT4H")
stabilized: ReportingFinalityPolicy = ReportingStabilizedPolicy("daily-stable-v1", "P2D", "PT12H")

definition_with_policy(source)
definition_with_policy(cutoff)
definition_with_policy(stabilized)
