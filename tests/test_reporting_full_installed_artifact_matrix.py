"""Fail-closed controls for the installed full reporting lifecycle gate."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "ci" / "reporting_interop"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "reporting_full_installed_matrix", SCRIPTS / "run_full_installed_artifact_matrix.py"
)
assert SPEC is not None and SPEC.loader is not None
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def _cell(index: int) -> dict:
    return {
        "positive_full_lifecycle": True,
        "database_identity": {
            "name": f"adcp_1199_full_cell_{index}",
            "cluster_identity": {
                "system_identifier": "cluster-1",
                "postmaster_started_at": "2026-09-28T00:00:00Z",
            },
        },
        "seller_process_identity": {"pid": 1000 + index, "start_token": str(2000 + index)},
        "output_directory": f"cell-{index}",
    }


def test_full_gate_requires_four_positive_isolated_cells() -> None:
    cells = [_cell(index) for index in range(4)]
    assert matrix._blocking_acceptance(cells)

    omitted = copy.deepcopy(cells)
    omitted.pop()
    assert not matrix._blocking_acceptance(omitted)

    failed = copy.deepcopy(cells)
    failed[2]["positive_full_lifecycle"] = False
    assert not matrix._blocking_acceptance(failed)

    leaked = copy.deepcopy(cells)
    leaked[3]["database_identity"] = leaked[0]["database_identity"]
    assert not matrix._blocking_acceptance(leaked)


def test_required_installed_job_runs_both_core_and_full_lifecycle_stages() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job_start = workflow.index("  reporting-installed-artifact-matrix:")
    job_end = workflow.index("\n  reporting-installed-artifact-required-gate:", job_start)
    job = workflow[job_start:job_end]
    gate_end = workflow.index("\n  v3-reference-seller-tests:", job_end)
    gate = workflow[job_end:gate_end]

    assert "run_installed_artifact_matrix.py" in job
    assert "run_full_installed_artifact_matrix.py" in job
    assert "pytest==9.0.2" in job
    assert "needs: reporting-installed-artifact-matrix" in gate
    assert 'needs.reporting-installed-artifact-matrix.result }}" != "success"' in gate


def _full_evidence() -> dict:
    return {
        "receipt": {
            "status": "passed",
            "exact_revision_read": True,
            "accepted_receipt_count": 1,
            "reconciliation_status": "accepted",
            "reporting_revision_id": "production-revision",
        },
        "buyer": {
            "status": "passed",
            "inspection_calls": 0,
            "reconciliation": {
                "definitive": True,
                "obligations": [{"reportingObligationId": "rpo_acct_a"}],
            },
        },
        "webhook": {
            "attempt_count": 2,
            "body_unchanged": True,
            "idempotency_key_unchanged": True,
            "verified_signature_count": 2,
            "signature_generations": [1, 2],
        },
    }


@pytest.mark.parametrize(
    ("stage", "field", "value"),
    [
        ("receipt", "exact_revision_read", False),
        ("receipt", "reconciliation_status", "pending"),
        ("buyer", "inspection_calls", 1),
        ("webhook", "body_unchanged", False),
        ("webhook", "verified_signature_count", 1),
        ("webhook", "signature_generations", [1, 1]),
    ],
)
def test_full_gate_refuses_missing_lifecycle_evidence(
    stage: str, field: str, value: object
) -> None:
    evidence = _full_evidence()
    assert matrix._full_result(**evidence)
    evidence[stage][field] = value
    assert not matrix._full_result(**evidence)
