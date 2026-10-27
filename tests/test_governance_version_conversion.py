"""Governance response semantics survive both sides of the 3.0/3.1 hop."""

from copy import deepcopy

import pytest

from adcp.compat.governance import convert_governance_fields
from adcp.compat.versions import convert_payload
from adcp.exceptions import ADCPFeatureUnsupportedError
from adcp.validation import validate_response


@pytest.mark.parametrize("verdict", ["approved", "denied", "conditions"])
def test_governance_verdict_round_trips_without_replacing_the_task_status(verdict):
    old = {
        "check_id": "check-one",
        "plan_id": "plan-one",
        "status": verdict,
        "explanation": "Governance evaluation completed",
    }
    if verdict in {"approved", "conditions"}:
        old["expires_at"] = "2027-01-01T00:00:00Z"
    if verdict == "denied":
        old["findings"] = [
            {"category_id": "budget", "severity": "critical", "explanation": "Over budget"}
        ]
    elif verdict == "conditions":
        old["conditions"] = [{"field": "budget", "reason": "Reduce budget"}]
    original = deepcopy(old)
    assert validate_response("check_governance", old, version="3.0").valid

    upgraded = convert_payload(
        "check_governance", old, direction="sync", source_version="3.0", target_version="3.2"
    )

    assert old == original
    assert upgraded.payload["verdict"] == verdict
    assert upgraded.payload["status"] == "completed"
    assert validate_response("check_governance", upgraded.payload, version="3.2").valid
    downgraded = convert_payload(
        "check_governance",
        upgraded.payload,
        direction="sync",
        source_version="3.2",
        target_version="3.0",
    )
    assert downgraded.payload == old
    assert validate_response("check_governance", downgraded.payload, version="3.0").valid


@pytest.mark.parametrize("state", ["accepted", "findings"])
def test_outcome_acknowledgement_round_trips_to_its_original_state(state):
    old = {"outcome_id": "outcome-one", "status": state}
    assert validate_response("report_plan_outcome", old, version="3.0").valid

    upgraded = convert_payload(
        "report_plan_outcome", old, direction="sync", source_version="3.0", target_version="3.2"
    )

    assert upgraded.payload["outcome_state"] == state
    assert upgraded.payload["status"] == "completed"
    assert validate_response("report_plan_outcome", upgraded.payload, version="3.2").valid
    downgraded = convert_payload(
        "report_plan_outcome",
        upgraded.payload,
        direction="sync",
        source_version="3.2",
        target_version="3.0",
    )
    assert downgraded.payload == old
    assert validate_response("report_plan_outcome", downgraded.payload, version="3.0").valid


def test_audit_check_verdict_round_trips_without_changing_other_lifecycle_states():
    old = {
        "plans": [
            {
                "plan_id": "plan-one",
                "plan_version": 1,
                "status": "active",
                "budget": {},
                "governed_actions": [],
                "summary": {},
                "entries": [
                    {
                        "id": "check-one",
                        "type": "check",
                        "timestamp": "2026-10-22T00:00:00Z",
                        "status": "approved",
                    },
                    {
                        "id": "outcome-one",
                        "type": "outcome",
                        "timestamp": "2026-10-22T00:00:00Z",
                        "outcome_status": "accepted",
                    },
                ],
            }
        ]
    }
    assert validate_response("get_plan_audit_logs", old, version="3.0").valid

    upgraded = convert_payload(
        "get_plan_audit_logs", old, direction="sync", source_version="3.0", target_version="3.2"
    )

    plan = upgraded.payload["plans"][0]
    assert plan["status"] == "active"
    assert plan["entries"][0]["verdict"] == "approved"
    assert "status" not in plan["entries"][0]
    assert plan["entries"][1] == old["plans"][0]["entries"][1]
    assert validate_response("get_plan_audit_logs", upgraded.payload, version="3.2").valid
    downgraded = convert_payload(
        "get_plan_audit_logs",
        upgraded.payload,
        direction="sync",
        source_version="3.2",
        target_version="3.0",
    )
    assert downgraded.payload == old
    assert validate_response("get_plan_audit_logs", downgraded.payload, version="3.0").valid


@pytest.mark.parametrize("direction", ["request", "sync"])
def test_other_version_hops_and_request_extension_values_remain_opaque(direction):
    payload = {"status": "approved", "ext": {"status": "approved"}}
    original = deepcopy(payload)
    convert_governance_fields("check_governance", payload, "3.1", "3.2", direction)
    assert payload == original
    if direction == "request":
        convert_governance_fields("check_governance", payload, "3.0", "3.1", direction)
        assert payload == original


def test_conflicting_governance_fields_are_rejected_without_overwriting_a_decision():
    payload = {"status": "approved", "verdict": "denied"}
    with pytest.raises(ADCPFeatureUnsupportedError, match="/verdict"):
        convert_governance_fields("check_governance", payload, "3.0", "3.1", "sync")
    assert payload == {"status": "approved", "verdict": "denied"}
