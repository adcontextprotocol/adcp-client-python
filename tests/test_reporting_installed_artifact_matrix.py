from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/reporting_interop/run_installed_artifact_matrix.py"
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
        "Traceback... ModuleNotFoundError: No module named " "'adcp.reporting.ledger.status_server'"
    )

    assert matrix._classify_seller_failure(expected) == "expected_unsupported_python_seller"
    assert matrix._classify_seller_failure("connection refused") == "unexpected_seller_failure"


def test_manifest_keeps_baseline_expectations_separate_from_acceptance() -> None:
    contract = json.loads(matrix.DEFAULT_CONTRACT.read_text(encoding="utf-8"))

    assert contract["acceptance_contract"]["required_cell_count"] == 4
    assert [cell["baseline_expected_outcome"] for cell in contract["cells"]].count(
        "positive_semantic"
    ) == 1
    assert "integrated_main_rerun" in contract["acceptance_contract"]["final_rerun_prerequisites"]
