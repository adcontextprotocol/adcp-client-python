"""Differential contract between structural models and canonical schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import adcp.types as public_types
from adcp import get_adcp_spec_version
from adcp.validation import validate_request, validate_response

_FIXTURES = Path(__file__).parent / "fixtures"
_CASES: list[dict[str, Any]] = json.loads((_FIXTURES / "validation-parity-cases.json").read_text())
_ALLOWED_DIVERGENCES: dict[str, dict[str, str]] = json.loads(
    (_FIXTURES / "validation-model-divergences.json").read_text()
)


def _pydantic_accepts(model: type[BaseModel], payload: dict[str, Any]) -> bool:
    try:
        model.model_validate(payload)
    except ValidationError:
        return False
    return True


def _canonical_accepts(case: dict[str, Any]) -> bool:
    validate = validate_request if case["direction"] == "request" else validate_response
    return validate(case["tool"], case["payload"]).valid


def test_validation_parity_fixtures_match_both_runtime_paths() -> None:
    """Each fixture pins the current Pydantic and canonical outcomes."""
    for case in _CASES:
        model = getattr(public_types, case["model"])
        assert _pydantic_accepts(model, case["payload"]) is case["pydantic_valid"], case["id"]
        assert _canonical_accepts(case) is case["canonical_valid"], case["id"]


def test_every_model_schema_divergence_is_explicitly_allowlisted() -> None:
    """No canonical negative may silently pass Pydantic without a rationale."""
    actual = {
        case["id"] for case in _CASES if case["pydantic_valid"] and not case["canonical_valid"]
    }
    assert actual == set(_ALLOWED_DIVERGENCES)
    for case_id, decision in _ALLOWED_DIVERGENCES.items():
        assert decision["keyword"], case_id
        assert decision["reason"], case_id


def test_no_fixture_is_stricter_in_canonical_schema_than_declared() -> None:
    """A changed generator outcome requires updating the reviewed decision."""
    declared = {case["id"]: case for case in _CASES}
    assert set(_ALLOWED_DIVERGENCES) <= set(declared)
    for case_id in _ALLOWED_DIVERGENCES:
        case = declared[case_id]
        assert case["pydantic_valid"] is True
        assert case["canonical_valid"] is False


def test_high_risk_behavioral_extension_has_targeted_runtime_validator() -> None:
    """Commitment-critical x-adcp-validation rules also fail at construction."""
    spec_version = get_adcp_spec_version()
    bundle = spec_version if "-" in spec_version else ".".join(spec_version.split(".")[:2])
    schema_path = (
        Path(__file__).parents[1] / "schemas" / "cache" / bundle / "media-buy" / "change-term.json"
    )
    schema = json.loads(schema_path.read_text())
    constraints = schema["x-adcp-validation"]["verifier_constraints"]
    assert "constraint_action_compatibility" in constraints

    with pytest.raises(ValidationError, match="incompatible with action"):
        public_types.MediaBuyChangeTerm.model_validate(
            {
                "term_id": "right_pause",
                "action": "pause",
                "service_mode": "self_serve",
                "constraints": {"kind": "budget", "max_delta_percent": 10},
            }
        )
