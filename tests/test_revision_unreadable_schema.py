"""Unreadable revisions survive the public model and canonical wire validator."""

from __future__ import annotations

import pytest

from adcp.types import ReportingStatusIssue
from adcp.validation.schema_loader import get_named_validator


def unreadable_issue(**overrides):
    return {
        "issue_id": "revision_unreadable_1",
        "code": "REVISION_UNREADABLE",
        "severity": "delayed",
        "responsible_party": "seller",
        "recommended_action": "wait_for_retry",
        "reporting_obligation_id": "obligation-billing",
        "reporting_revision_id": "revision-august-official",
        **overrides,
    }


@pytest.mark.parametrize(
    "severity,action",
    [("delayed", "wait_for_retry"), ("action_required", "contact_seller")],
)
def test_unreadable_revision_public_model_round_trip(severity, action):
    raw = unreadable_issue(severity=severity, recommended_action=action)
    validator = get_named_validator("core/reporting-status-issue.json")
    assert validator is not None
    assert not list(validator.iter_errors(raw))
    model = ReportingStatusIssue.model_validate(raw)
    assert model.model_dump(mode="json", exclude_none=True) == raw
    restored = ReportingStatusIssue.model_validate_json(model.model_dump_json(exclude_none=True))
    assert restored.model_dump(mode="json", exclude_none=True) == raw


@pytest.mark.parametrize(
    "change",
    [
        {"responsible_party": "buyer"},
        {"responsible_party": "provider"},
        {"reporting_obligation_id": None},
        {"reporting_revision_id": None},
        {"reporting_revision_id": ""},
        {"reporting_revision_id": "https://seller.example/revision"},
        {"reporting_revision_id": "x" * 256},
        {"recommended_action": "contact_seller"},
        {"severity": "action_required", "recommended_action": "wait_for_retry"},
    ],
)
def test_unreadable_revision_wire_rejects_unattributed_or_invalid_revision(change):
    validator = get_named_validator("core/reporting-status-issue.json")
    assert validator is not None
    raw = unreadable_issue(**change)
    raw = {key: value for key, value in raw.items() if value is not None}
    assert list(validator.iter_errors(raw))
