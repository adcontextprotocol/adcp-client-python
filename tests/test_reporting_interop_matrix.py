from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "reporting_interop_matrix.py"
MANIFEST = SCRIPT.with_name("reporting_interop_inputs.json")
SPEC = importlib.util.spec_from_file_location("reporting_interop_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
matrix = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = matrix
SPEC.loader.exec_module(matrix)


def inputs() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_preparation_manifest_keeps_exact_required_cross_product() -> None:
    result = matrix.validate_manifest(inputs(), acceptance=False)

    assert result.ready is False
    assert result.missing == ("python.stable", "python.candidate", "typescript.candidate")
    assert [
        tuple(cell[key] for key in ("id", "python", "typescript", "protocol"))
        for cell in inputs()["cells"]
    ] == list(matrix.EXPECTED_CELLS)
    assert all(cell["required"] is True for cell in inputs()["cells"])


def test_acceptance_fails_closed_while_coordinates_are_missing() -> None:
    with pytest.raises(matrix.InputError, match="acceptance inputs are incomplete"):
        matrix.validate_manifest(inputs(), acceptance=True)


@pytest.mark.parametrize("mutation", ["delete", "optional", "wrong_protocol", "duplicate"])
def test_controlling_cells_cannot_be_weakened(mutation: str) -> None:
    value = inputs()
    if mutation == "delete":
        value["cells"].pop()
    elif mutation == "optional":
        value["cells"][1]["required"] = False
    elif mutation == "wrong_protocol":
        value["cells"][2]["protocol"] = "candidate"
    else:
        value["cells"][3] = copy.deepcopy(value["cells"][0])

    with pytest.raises(matrix.InputError):
        matrix.validate_manifest(value, acceptance=False)


def test_mutable_or_source_only_inputs_are_rejected() -> None:
    typescript = copy.deepcopy(inputs()["artifacts"]["typescript"]["stable"])
    typescript["version"] = "latest"
    with pytest.raises(matrix.InputError, match="exact package version"):
        matrix._validate_typescript("stable", typescript)

    python = {
        "package": "adcp",
        "version": "8.0.0rc1",
        "registry": "https://pypi.example.invalid/",
        "wheel_url": "https://example.invalid/adcp-8.0.0rc1-py3-none-any.whl",
        "wheel_sha256": "a" * 64,
        "protocol": "3.2.0-rc.4",
        "source_kind": "pr",
    }
    with pytest.raises(matrix.InputError, match="source-only"):
        matrix._validate_python("candidate", python)


def test_rc41_cannot_be_substituted_for_corrected_candidate() -> None:
    value = inputs()
    pending = value["pending"]["typescript_candidate"]
    pending["proposed_version"] = pending["forbidden_substitute"]

    with pytest.raises(matrix.InputError, match="forbidden prior release"):
        matrix.monitor_typescript(value)

    candidate = copy.deepcopy(value["artifacts"]["typescript"]["stable"])
    with pytest.raises(matrix.InputError, match="cannot substitute"):
        matrix._validate_typescript("candidate", candidate)


def test_npm_integrity_must_be_sha512() -> None:
    typescript = copy.deepcopy(inputs()["artifacts"]["typescript"]["stable"])
    typescript["integrity"] = "sha256-deadbeef"

    with pytest.raises(matrix.InputError, match="must be sha512"):
        matrix._validate_typescript("stable", typescript)


def test_baseline_is_explicitly_non_acceptance() -> None:
    baseline = inputs()["baseline_inputs"]["python_registry"]

    assert baseline["version"] == "8.0.0b15"
    assert baseline["protocol"] == "3.2.0-rc.3"
    assert baseline["acceptance"] is False


def complete_inputs() -> dict:
    value = inputs()
    value["phase"] = "acceptance"
    seller_entrypoint = SCRIPT.relative_to(ROOT)
    buyer_entrypoint = Path(__file__).resolve().relative_to(ROOT)
    value["execution"].update(
        {
            "postgresql_version": "16.14",
            "os_release_sha256": hashlib.sha256(Path("/etc/os-release").read_bytes()).hexdigest(),
            "seller_entrypoint": str(seller_entrypoint),
            "seller_entrypoint_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
            "buyer_entrypoint": str(buyer_entrypoint),
            "buyer_entrypoint_sha256": hashlib.sha256(
                Path(__file__).resolve().read_bytes()
            ).hexdigest(),
        }
    )
    for role, version, digest, protocol in (
        ("stable", "8.0.0rc1", "1" * 64, "3.2.0-rc.3"),
        ("candidate", "8.0.0rc2", "2" * 64, "3.2.0-rc.4"),
    ):
        value["artifacts"]["python"][role] = {
            "package": "adcp",
            "version": version,
            "registry": "https://pypi.example.invalid/",
            "wheel_url": f"https://packages.example.invalid/adcp-{version}-py3-none-any.whl",
            "wheel_sha256": digest,
            "protocol": protocol,
            "python_version": "3.12.14",
            "requirements_lock_url": (
                f"https://packages.example.invalid/adcp-{version}-requirements.txt"
            ),
            "requirements_lock_sha256": ("7" if role == "stable" else "8") * 64,
        }
    candidate = copy.deepcopy(value["artifacts"]["typescript"]["stable"])
    candidate["version"] = "14.0.0-rc.42"
    candidate["tarball_url"] = "https://registry.npmjs.org/@adcp/sdk/-/sdk-14.0.0-rc.42.tgz"
    candidate["shasum"] = "4" * 40
    value["artifacts"]["typescript"]["candidate"] = candidate
    for role in ("stable", "candidate"):
        item = value["artifacts"]["typescript"][role]
        item.update(
            {
                "node_version": "24.19.0",
                "npm_version": "10.9.4",
                "lock_url": f"https://packages.example.invalid/sdk-{item['version']}-package-lock.json",
                "lock_sha256": ("9" if role == "stable" else "a") * 64,
            }
        )
    return value


def retained(path: Path, root: Path) -> dict:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(root)),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def passing_results(value: dict, run_root: Path) -> dict:
    aggregate = run_root / "aggregate.log"
    aggregate.write_text("four required cells passed\n", encoding="utf-8")
    cells = []
    for index, expected in enumerate(value["cells"]):
        log = run_root / f"cell-{index}.log"
        log.write_text(f"cell {index}\n", encoding="utf-8")
        python = value["artifacts"]["python"][expected["python"]]
        typescript = value["artifacts"]["typescript"][expected["typescript"]]
        protocol = value["protocols"][expected["protocol"]]
        cells.append(
            {
                "id": expected["id"],
                "status": "passed",
                "acceptance": True,
                "seller_pid": 1000 + index,
                "database": f"reporting_interop_{index}",
                "state_directory": f"state/{index}",
                "assertions": {name: True for name in matrix.REQUIRED_ASSERTIONS},
                "inputs": {
                    "python": {
                        "version": python["version"],
                        "wheel_sha256": python["wheel_sha256"],
                        "requirements_lock_sha256": python["requirements_lock_sha256"],
                    },
                    "typescript": {
                        "version": typescript["version"],
                        "integrity": typescript["integrity"],
                        "lock_sha256": typescript["lock_sha256"],
                    },
                    "protocol": {
                        "version": protocol["version"],
                        "sha256": protocol["sha256"],
                    },
                },
                "runtime": {
                    "python": python["python_version"],
                    "node": typescript["node_version"],
                    "npm": typescript["npm_version"],
                    "postgresql": value["execution"]["postgresql_version"],
                    "os_release_sha256": value["execution"]["os_release_sha256"],
                    "database_encoding": "UTF8",
                    "database_collation": "C",
                },
                "evidence": [retained(log, run_root)],
            }
        )
    return {
        "acceptance": True,
        "status": "passed",
        "cells": cells,
        "evidence": [retained(aggregate, run_root)],
    }


def test_result_gate_checks_all_semantics_and_cross_cell_isolation(tmp_path: Path) -> None:
    value = complete_inputs()
    results = passing_results(value, tmp_path)

    matrix.validate_results(value, results, run_root=tmp_path)

    results["cells"][3]["database"] = results["cells"][0]["database"]
    with pytest.raises(matrix.InputError, match="isolated database"):
        matrix.validate_results(value, results, run_root=tmp_path)


def test_acceptance_requires_dependency_locks_and_exact_runtimes() -> None:
    value = complete_inputs()
    del value["artifacts"]["python"]["candidate"]["requirements_lock_sha256"]

    with pytest.raises(matrix.InputError, match="requirements_lock_sha256"):
        matrix.validate_manifest(value, acceptance=True)


def test_result_gate_rejects_runtime_drift(tmp_path: Path) -> None:
    value = complete_inputs()
    results = passing_results(value, tmp_path)
    results["cells"][1]["runtime"]["node"] = "24.19.1"

    with pytest.raises(matrix.InputError, match="runtime identity"):
        matrix.validate_results(value, results, run_root=tmp_path)


def test_result_gate_rejects_one_missing_semantic_assertion(tmp_path: Path) -> None:
    value = complete_inputs()
    results = passing_results(value, tmp_path)
    del results["cells"][2]["assertions"]["account_activity_retry_visible"]

    with pytest.raises(matrix.InputError, match="every required semantic assertion"):
        matrix.validate_results(value, results, run_root=tmp_path)


def test_legacy_validate_results_command_is_explicitly_nonaccepting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    value = complete_inputs()
    results = passing_results(value, tmp_path)
    manifest_path = tmp_path / "manifest.json"
    results_path = tmp_path / "results.json"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    results_path.write_text(json.dumps(results), encoding="utf-8")

    assert (
        matrix.main(
            [
                "--manifest",
                str(manifest_path),
                "validate-results",
                "--results",
                str(results_path),
                "--run-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["acceptance"] is False
    assert emitted["blocking_acceptance"] is False
    assert emitted["status"] == "legacy_result_shape_validated_nonaccepting"
