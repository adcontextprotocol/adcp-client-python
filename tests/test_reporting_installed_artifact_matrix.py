from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/reporting_interop/run_installed_artifact_matrix.py"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
SPEC = importlib.util.spec_from_file_location("installed_artifact_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
matrix = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = matrix
SPEC.loader.exec_module(matrix)


def test_installed_artifact_contract_is_distinct_from_language_role_quadrants() -> None:
    contract = matrix._load_contract(matrix.DEFAULT_CONTRACT)

    assert [cell["id"] for cell in contract["cells"]] == [
        "python-stable__typescript-stable",
        "python-stable__typescript-candidate",
        "python-candidate__typescript-stable",
        "python-candidate__typescript-candidate",
    ]
    assert all(cell["required_polarity"] == "positive_semantic" for cell in contract["cells"])
    assert all(not cell["id"].startswith("Q") for cell in contract["cells"])


def test_negative_controls_cannot_be_classified_as_positive_semantic() -> None:
    unsupported = {
        "status": "unsupported",
        "semantic_lane_complete": False,
        "http_exercised": True,
    }

    assert matrix._classify_client_result(0, unsupported) == (
        "expected_unsupported_typescript_client"
    )
    assert (
        matrix._classify_client_result(
            0, {"status": "unsupported", "semantic_lane_complete": False}
        )
        == "unexpected_client_failure"
    )
    assert (
        matrix._classify_client_result(0, {"status": "passed", "semantic_lane_complete": True})
        == "positive_semantic"
    )


def test_stable_python_failure_matches_only_the_pinned_missing_surface() -> None:
    expected = (
        "Traceback... ModuleNotFoundError: No module named 'adcp.reporting.ledger.status_server'"
    )

    assert matrix._classify_seller_failure(expected) == "expected_unsupported_python_seller"
    assert matrix._classify_seller_failure("connection refused") == "unexpected_seller_failure"


def test_manifest_requires_four_positive_installed_artifact_cells() -> None:
    contract = json.loads(matrix.DEFAULT_CONTRACT.read_text(encoding="utf-8"))

    assert contract["acceptance_contract"]["required_cell_count"] == 4
    assert contract["phase"] != matrix.BASELINE_PHASE
    assert [cell["baseline_expected_outcome"] for cell in contract["cells"]].count(
        "positive_semantic"
    ) == 4
    assert contract["artifacts"]["python"]["stable"]["version"] == "8.0.0b16"
    assert contract["artifacts"]["python"]["candidate"]["version"] == "8.0.0rc3"
    assert contract["artifacts"]["python"]["candidate"]["protocol"] == "3.2.0-rc.7"
    assert contract["artifacts"]["typescript"]["stable"]["version"] == "14.0.0-rc.47"
    assert "integrated_main_rerun" in contract["acceptance_contract"]["final_rerun_prerequisites"]
    assert (
        "positive_typescript_stable_artifact_selected"
        in contract["acceptance_contract"]["final_rerun_prerequisites"]
    )


def test_wire_version_tracks_the_python_seller_in_each_skew_lane() -> None:
    contract = matrix._load_contract(matrix.DEFAULT_CONTRACT)
    contract["artifacts"]["python"]["candidate"]["protocol"] = "3.2.0-rc.7"

    assert matrix._seller_wire_version(contract, "stable") == "3.2.0-rc.6"
    assert matrix._seller_wire_version(contract, "candidate") == "3.2.0-rc.7"
    assert (
        matrix._seller_wire_version(contract, "candidate")
        != contract["artifacts"]["typescript"]["stable"]["protocol"]
    )


def test_gate_requires_issue_acceptance_after_baseline_phase() -> None:
    assert matrix._gate_passes(
        phase=matrix.BASELINE_PHASE,
        baseline_complete=True,
        issue_acceptance=False,
    )
    assert not matrix._gate_passes(
        phase="integrated_required_acceptance",
        baseline_complete=True,
        issue_acceptance=False,
    )
    assert matrix._gate_passes(
        phase="integrated_required_acceptance",
        baseline_complete=True,
        issue_acceptance=True,
    )


def test_cross_cell_isolation_rejects_reused_database_or_process() -> None:
    rows = [
        {
            "database_identity": {
                "name": f"db_{index}",
                "cluster_identity": {
                    "system_identifier": "cluster-a",
                    "postmaster_started_at": "2026-09-27 00:00:00+00",
                },
            },
            "seller_process_identity": {"pid": 100 + index, "start_token": f"start-{index}"},
            "output_directory": f"cell-{index}",
        }
        for index in range(4)
    ]
    assert matrix._cross_cell_isolation_errors(rows) == []

    rows[1]["database_identity"] = rows[0]["database_identity"]
    rows[3]["seller_process_identity"] = rows[2]["seller_process_identity"]
    rows[2]["output_directory"] = rows[1]["output_directory"]
    assert matrix._cross_cell_isolation_errors(rows) == [
        "required cells must not reuse a database identity",
        "required cells must not reuse a seller process identity",
        "required cells must not reuse an output directory",
    ]


@pytest.mark.parametrize(
    ("missing_key", "expected_error"),
    [
        ("database_identity", "every cell must retain its database and cluster identity"),
        ("seller_process_identity", "every cell must retain its seller process identity"),
        ("output_directory", "every cell must retain its output directory identity"),
    ],
)
def test_cross_cell_isolation_requires_observed_identities(
    missing_key: str, expected_error: str
) -> None:
    row = {
        "database_identity": {
            "name": "db",
            "cluster_identity": {
                "system_identifier": "cluster",
                "postmaster_started_at": "2026-09-27 00:00:00+00",
            },
        },
        "seller_process_identity": {"pid": 100, "start_token": "start"},
        "output_directory": "cell",
    }
    del row[missing_key]

    assert expected_error in matrix._cross_cell_isolation_errors([row])


def test_required_postgres_gate_depends_on_installed_artifact_matrix() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    gate_start = workflow.index("  pg-conformance-required-gate:")
    gate_end = workflow.index("\n  pg-reporting-status:", gate_start)
    gate = workflow[gate_start:gate_end]

    assert "reporting-installed-artifact-matrix" in gate
    assert (
        "REPORTING_INTEROP_RESULT: ${{ needs.reporting-installed-artifact-matrix.result }}" in gate
    )
    assert '[ "$REPORTING_INTEROP_RESULT" != "success" ]' in gate
    assert (
        "tests/test_reporting_storyboard_orchestration.py::"
        "test_database_owner_returns_node_postgres_uri_after_psycopg_validation" in workflow
    )
    assert (
        "tests/test_reporting_storyboard_orchestration.py::"
        "test_node_database_url_materializes_libpq_default_user" in workflow
    )
