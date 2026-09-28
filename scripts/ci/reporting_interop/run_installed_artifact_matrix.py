#!/usr/bin/env python3
"""Run #1199's installed Python seller x TypeScript client 2x2 matrix.

The four cells in this runner are deliberately distinct from the foundation
Q1-Q4 language-role quadrants.  Each cell owns a fresh PostgreSQL database and
Python seller process. Final acceptance requires positive semantic results
from all four cells and distinct durable resources for each cell.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEFAULT_CONTRACT = HERE / "installed_artifact_cells.json"
CLIENT_TIMEOUT_SECONDS = 60
BASELINE_PHASE = "baseline_before_rc7_and_1172"
foundation: Any = None


class MatrixError(RuntimeError):
    """The installed-artifact matrix did not match its declared contract."""


def _load_foundation() -> Any:
    spec = importlib.util.spec_from_file_location(
        "reporting_interop_foundation", HERE / "run_foundation_matrix.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the reporting interop foundation runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _path(value: str) -> Path:
    path = Path(os.path.abspath(value))
    if not path.exists():
        raise argparse.ArgumentTypeError(f"path does not exist: {path}")
    return path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=_path, default=DEFAULT_CONTRACT)
    parser.add_argument("--python-stable-runtime", type=_path, required=True)
    parser.add_argument("--python-stable-artifact", type=_path, required=True)
    parser.add_argument("--python-candidate-runtime", type=_path, required=True)
    parser.add_argument("--python-candidate-artifact", type=_path, required=True)
    parser.add_argument("--typescript-stable-install", type=_path, required=True)
    parser.add_argument("--typescript-stable-archive", type=_path, required=True)
    parser.add_argument("--typescript-candidate-install", type=_path, required=True)
    parser.add_argument("--typescript-candidate-archive", type=_path, required=True)
    parser.add_argument("--node-runtime", type=_path, required=True)
    parser.add_argument("--pg-admin-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-databases", action="store_true")
    return parser.parse_args()


def _load_contract(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    cells = value.get("cells")
    expected_ids = [
        "python-stable__typescript-stable",
        "python-stable__typescript-candidate",
        "python-candidate__typescript-stable",
        "python-candidate__typescript-candidate",
    ]
    if (
        value.get("schema_version") != 1
        or not isinstance(cells, list)
        or [cell.get("id") for cell in cells] != expected_ids
        or any(cell.get("required") is not True for cell in cells)
        or any(cell.get("required_polarity") != "positive_semantic" for cell in cells)
    ):
        raise MatrixError("installed-artifact contract must retain the exact required 2x2")
    return value


def _retained(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "sha256": foundation._sha256(path),
    }


def _validate_artifact_contract(
    contract: dict[str, Any],
    *,
    python_inputs: dict[str, tuple[foundation.PythonRuntime, foundation.PythonArtifact]],
    typescript_inputs: dict[str, tuple[foundation.TypeScriptInstall, foundation.TypeScriptArchive]],
    node: Path,
) -> dict[str, Any]:
    observed_python: dict[str, Any] = {}
    for role, (runtime, artifact) in python_inputs.items():
        declared = contract["artifacts"]["python"][role]
        if (
            foundation._sha256(artifact.path) != declared["wheel_sha256"]
            or artifact.path.stat().st_size != declared["wheel_bytes"]
        ):
            raise MatrixError(f"Python {role} artifact differs from the contract")
        identity = foundation._runtime_identity(runtime, artifact)
        if (
            identity["adcp"] != declared["version"]
            or identity["sdk_member_count"] != declared["sdk_member_count"]
            or identity["sdk_member_manifest_sha256"] != declared["sdk_member_manifest_sha256"]
        ):
            raise MatrixError(f"Python {role} installed members differ from the contract")
        observed_python[role] = identity

    observed_typescript: dict[str, Any] = {}
    for role, (install, archive) in typescript_inputs.items():
        identity = foundation._typescript_archive_identity(install, archive)
        declared = contract["artifacts"]["typescript"][role]
        if (
            identity["archive_bytes"] != declared["tarball_bytes"]
            or identity["installed_member_count"] != declared["installed_member_count"]
            or identity["installed_member_manifest_sha256"]
            != declared["installed_member_manifest_sha256"]
        ):
            raise MatrixError(f"TypeScript {role} installed members differ from the contract")
        observed_typescript[role] = identity
    return {
        "python": observed_python,
        "typescript": observed_typescript,
        "node": foundation._node_identity(node),
    }


def _client_result(
    command: list[str],
    *,
    cwd: Path,
    token: str,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[int, dict[str, Any] | None]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=foundation._buyer_environment(token),
        text=True,
        capture_output=True,
        timeout=CLIENT_TIMEOUT_SECONDS,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = None
    return completed.returncode, result


def _classify_client_result(exit_code: int, result: dict[str, Any] | None) -> str:
    if (
        exit_code == 0
        and result is not None
        and result.get("status") == "passed"
        and result.get("semantic_lane_complete") is True
    ):
        return "positive_semantic"
    if (
        exit_code == 0
        and result is not None
        and result.get("status") == "unsupported"
        and result.get("semantic_lane_complete") is False
        and result.get("http_exercised") is True
    ):
        return "expected_unsupported_typescript_client"
    return "unexpected_client_failure"


def _classify_seller_failure(stderr: str) -> str:
    expected = "No module named 'adcp.reporting.ledger.status_server'"
    return (
        "expected_unsupported_python_seller" if expected in stderr else "unexpected_seller_failure"
    )


def _cross_cell_isolation_errors(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    databases = []
    for row in rows:
        identity = row.get("database_identity", {})
        cluster = identity.get("cluster_identity", {})
        databases.append(
            (
                identity.get("name"),
                cluster.get("system_identifier"),
                cluster.get("postmaster_started_at"),
            )
        )
    sellers = [
        (
            row.get("seller_process_identity", {}).get("pid"),
            row.get("seller_process_identity", {}).get("start_token"),
        )
        for row in rows
    ]
    output_directories = [row.get("output_directory") for row in rows]
    if any(not all(identity) for identity in databases):
        errors.append("every cell must retain its database and cluster identity")
    if len(set(databases)) != len(databases):
        errors.append("required cells must not reuse a database identity")
    if any(not pid or not start for pid, start in sellers):
        errors.append("every cell must retain its seller process identity")
    if len(set(sellers)) != len(sellers):
        errors.append("required cells must not reuse a seller process identity")
    if any(not directory for directory in output_directories):
        errors.append("every cell must retain its output directory identity")
    if len(set(output_directories)) != len(output_directories):
        errors.append("required cells must not reuse an output directory")
    return errors


def _gate_passes(*, phase: str, baseline_complete: bool, issue_acceptance: bool) -> bool:
    return baseline_complete if phase == BASELINE_PHASE else issue_acceptance


def _seller_wire_version(contract: dict[str, Any], python_role: str) -> str:
    """Send the installed seller's protocol, which can differ across Python lanes."""
    version = contract["artifacts"]["python"][python_role]["protocol"]
    if not isinstance(version, str) or not version:
        raise MatrixError(f"Python {python_role} artifact has no protocol version")
    return version


def _run_cell(
    cell: dict[str, Any],
    *,
    wire_adcp_version: str,
    python_input: tuple[foundation.PythonRuntime, foundation.PythonArtifact],
    typescript_input: tuple[foundation.TypeScriptInstall, foundation.TypeScriptArchive],
    node: Path,
    admin_url: str,
    database: str,
    output: Path,
) -> dict[str, Any]:
    python_role = cell["python_role"]
    typescript_role = cell["typescript_role"]
    runtime, _artifact = python_input
    install, _archive = typescript_input
    database_identity = foundation._create_database(admin_url, database)
    database_url = foundation._node_database_url(admin_url, database)
    port = foundation._free_port()
    stdout_path = output / "seller.stdout.log"
    stderr_path = output / "seller.stderr.log"
    client_stdout_path = output / "client.stdout.json"
    client_stderr_path = output / "client.stderr.log"
    expected = cell["baseline_expected_outcome"]
    process: subprocess.Popen[bytes] | None = None
    process_identity: dict[str, Any] | None = None
    actual_outcome: str
    result: dict[str, Any] | None = None
    client_exit: int | None = None
    with stdout_path.open("wb") as seller_stdout, stderr_path.open("wb") as seller_stderr:
        process = subprocess.Popen(
            [
                str(runtime.executable),
                "-I",
                str(foundation.PYTHON_SERVER),
                "--port",
                str(port),
                "--database-url",
                database_url,
            ],
            cwd=output,
            stdin=subprocess.DEVNULL,
            stdout=seller_stdout,
            stderr=seller_stderr,
            start_new_session=True,
        )
        try:
            process_identity = {
                "pid": process.pid,
                "start_token": foundation._linux_process_start_token(process.pid),
            }
            try:
                foundation._wait_port(process, port)
            except foundation.HarnessError:
                process.wait(timeout=10)
                seller_stderr.flush()
                actual_outcome = _classify_seller_failure(stderr_path.read_text(encoding="utf-8"))
            else:
                command = foundation._ts_core_buyer_command(
                    node,
                    install,
                    endpoint=f"http://127.0.0.1:{port}/mcp",
                    expect_missing=expected == "expected_unsupported_typescript_client",
                    wire_adcp_version=wire_adcp_version,
                )
                client_exit, result = _client_result(
                    command,
                    cwd=output,
                    token="interop-token-a",
                    stdout_path=client_stdout_path,
                    stderr_path=client_stderr_path,
                )
                actual_outcome = _classify_client_result(client_exit, result)
        finally:
            foundation._stop(process)

    evidence = [_retained(stdout_path, output), _retained(stderr_path, output)]
    if client_stdout_path.is_file():
        evidence.extend(
            [
                _retained(client_stdout_path, output),
                _retained(client_stderr_path, output),
            ]
        )
    baseline_match = actual_outcome == expected
    positive = actual_outcome == "positive_semantic"
    return {
        "id": cell["id"],
        "required": True,
        "required_polarity": "positive_semantic",
        "baseline_expected_outcome": expected,
        "actual_outcome": actual_outcome,
        "baseline_match": baseline_match,
        "positive_semantic": positive,
        "acceptance_credit": positive,
        "execution_attempted": True,
        "real_mcp_http": client_exit is not None,
        "fresh_database": True,
        "fresh_seller_process": True,
        "python_role": python_role,
        "typescript_role": typescript_role,
        "database_identity": database_identity,
        "seller_process_identity": process_identity,
        "output_directory": output.name,
        "client_exit_code": client_exit,
        "result": result,
        "evidence": evidence,
    }


def main() -> None:
    global foundation
    foundation = _load_foundation()
    args = _arguments()
    contract = _load_contract(args.contract)
    args.output = args.output.absolute()
    args.output.mkdir(parents=True, exist_ok=False)
    python_inputs = {
        "stable": (
            foundation.PythonRuntime("previous", args.python_stable_runtime),
            foundation.PythonArtifact("previous", args.python_stable_artifact),
        ),
        "candidate": (
            foundation.PythonRuntime("wheel", args.python_candidate_runtime),
            foundation.PythonArtifact("wheel", args.python_candidate_artifact),
        ),
    }
    typescript_inputs = {
        "stable": (
            foundation.TypeScriptInstall("previous", args.typescript_stable_install),
            foundation.TypeScriptArchive("previous", args.typescript_stable_archive),
        ),
        "candidate": (
            foundation.TypeScriptInstall("candidate", args.typescript_candidate_install),
            foundation.TypeScriptArchive("candidate", args.typescript_candidate_archive),
        ),
    }
    artifact_identities = _validate_artifact_contract(
        contract,
        python_inputs=python_inputs,
        typescript_inputs=typescript_inputs,
        node=args.node_runtime,
    )
    run_id = secrets.token_hex(6)
    created: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        for index, cell in enumerate(contract["cells"]):
            cell_output = args.output / cell["id"]
            cell_output.mkdir()
            database = f"adcp_1199_{run_id}_{index}"
            created.append(database)
            rows.append(
                _run_cell(
                    cell,
                    wire_adcp_version=_seller_wire_version(contract, cell["python_role"]),
                    python_input=python_inputs[cell["python_role"]],
                    typescript_input=typescript_inputs[cell["typescript_role"]],
                    node=args.node_runtime,
                    admin_url=args.pg_admin_url,
                    database=database,
                    output=cell_output,
                )
            )
    finally:
        if not args.keep_databases:
            foundation._drop_databases(args.pg_admin_url, created)

    isolation_errors = _cross_cell_isolation_errors(rows)
    baseline_complete = (
        len(rows) == 4 and all(row["baseline_match"] for row in rows) and not isolation_errors
    )
    positive_cells = [row["id"] for row in rows if row["positive_semantic"]]
    issue_acceptance = baseline_complete and len(positive_cells) == 4
    gate_passed = _gate_passes(
        phase=contract["phase"],
        baseline_complete=baseline_complete,
        issue_acceptance=issue_acceptance,
    )
    if contract["phase"] == BASELINE_PHASE:
        status = "baseline_passed" if baseline_complete else "baseline_failed"
    else:
        status = "acceptance_passed" if issue_acceptance else "acceptance_failed"
    report = {
        "schema_version": 1,
        "phase": contract["phase"],
        "run_id": run_id,
        "baseline_complete": baseline_complete,
        "status": status,
        "acceptance": issue_acceptance,
        "blocking_acceptance": issue_acceptance,
        "gate_passed": gate_passed,
        "cross_cell_isolation_errors": isolation_errors,
        "required_cell_count": 4,
        "executed_cell_count": len(rows),
        "positive_semantic_cells": positive_cells,
        "missing_positive_semantic_cells": [
            row["id"] for row in rows if not row["positive_semantic"]
        ],
        "final_rerun_prerequisites": contract["acceptance_contract"]["final_rerun_prerequisites"],
        "artifact_identities": artifact_identities,
        "cells": rows,
        "harness": {
            **foundation._git_identity(),
            "contract": {
                "path": str(args.contract),
                "sha256": foundation._sha256(args.contract),
            },
            "entrypoints": {
                "seller": {
                    "path": str(foundation.PYTHON_SERVER.relative_to(ROOT)),
                    "sha256": foundation._sha256(foundation.PYTHON_SERVER),
                },
                "buyer": {
                    "path": str(foundation.TS_CORE_BUYER.relative_to(ROOT)),
                    "sha256": foundation._sha256(foundation.TS_CORE_BUYER),
                },
            },
        },
    }
    report_path = args.output / "results.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    if not gate_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
