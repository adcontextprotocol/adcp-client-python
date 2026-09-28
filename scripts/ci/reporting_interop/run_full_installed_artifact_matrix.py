#!/usr/bin/env python3
"""Run #1199's full installed Python/TypeScript reporting lifecycle matrix.

Each cell owns a fresh PostgreSQL database, installed Python seller process,
installed TypeScript client, and retained output. An error in any cell fails the
single aggregate result; historical and mutable artifacts receive no credit.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SELLER = HERE / "python_full_server.py"
RECEIPT = HERE / "ts_full_receipt.cjs"
BUYER = HERE / "ts_mcp_buyer.cjs"
SCENARIO = HERE / "full_scenario.json"
PINS = HERE / "pins.json"
TOKEN = "full-matrix-token"


def _load_core() -> Any:
    spec = importlib.util.spec_from_file_location(
        "reporting_installed_core_matrix", HERE / "run_installed_artifact_matrix.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load installed Core matrix")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


core = _load_core()


def _client_command(
    script: Path,
    *,
    node: Path,
    install: Any,
    version: str,
    endpoint: str,
) -> list[str]:
    pin = core.foundation._typescript_pin(install)
    return [
        str(node),
        str(script),
        "--package-lock",
        str(install.lock),
        "--expected-version",
        pin["version"],
        "--expected-integrity",
        pin["integrity"],
        "--adcp-version",
        version,
        "--endpoint",
        endpoint,
        "--auth-env",
        "ADCP_INTEROP_BUYER_TOKEN",
    ]


def _full_result(
    *,
    receipt: dict[str, Any] | None,
    buyer: dict[str, Any] | None,
    webhook: dict[str, Any] | None,
) -> bool:
    if not (
        receipt
        and receipt.get("status") == "passed"
        and receipt.get("exact_revision_read") is True
        and receipt.get("accepted_receipt_count") == 1
        and receipt.get("reconciliation_status") == "accepted"
        and buyer
        and buyer.get("status") == "passed"
        and buyer.get("reconciliation", {}).get("definitive") is True
        and buyer.get("inspection_calls") == 0
        and webhook
        and webhook.get("attempt_count", 0) >= 2
        and webhook.get("body_unchanged") is True
        and webhook.get("idempotency_key_unchanged") is True
        and webhook.get("verified_signature_count") == 2
        and webhook.get("signature_generations") == [1, 2]
    ):
        return False
    obligations = buyer["reconciliation"].get("obligations", [])
    return (
        len(obligations) == 1
        and obligations[0].get("reportingObligationId") == "rpo_acct_a"
        and receipt["reporting_revision_id"] == "production-revision"
    )


def _blocking_acceptance(rows: list[dict[str, Any]]) -> bool:
    return (
        len(rows) == 4
        and not core._cross_cell_isolation_errors(rows)
        and all(row["positive_full_lifecycle"] for row in rows)
    )


def _run_cell(
    cell: dict[str, Any],
    *,
    contract: dict[str, Any],
    python_input: tuple[Any, Any],
    typescript_input: tuple[Any, Any],
    node: Path,
    admin_url: str,
    database: str,
    output: Path,
) -> dict[str, Any]:
    foundation = core.foundation
    runtime, _artifact = python_input
    install, _archive = typescript_input
    database_identity = foundation._create_database(admin_url, database)
    database_url = foundation._node_database_url(admin_url, database)
    version = core._seller_wire_version(contract, cell["python_role"])
    port = foundation._free_port()
    endpoint = f"http://127.0.0.1:{port}/mcp"
    scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
    scenario["now"] = datetime.now(timezone.utc).isoformat()
    scenario_path = output / "scenario.json"
    scenario_path.write_text(json.dumps(scenario, sort_keys=True) + "\n", encoding="utf-8")
    seller_out = output / "seller.stdout.log"
    seller_err = output / "seller.stderr.log"
    receipt_out = output / "receipt.stdout.json"
    receipt_err = output / "receipt.stderr.log"
    buyer_out = output / "buyer.stdout.json"
    buyer_err = output / "buyer.stderr.log"
    webhook_path = output / "webhook.json"
    receipt: dict[str, Any] | None = None
    buyer: dict[str, Any] | None = None
    webhook: dict[str, Any] | None = None
    issues: list[str] = []
    process_identity: dict[str, Any] | None = None
    with seller_out.open("wb") as stdout, seller_err.open("wb") as stderr:
        process = subprocess.Popen(
            [
                str(runtime.executable),
                "-I",
                str(SELLER),
                "--port",
                str(port),
                "--database-url",
                database_url,
                "--destination",
                str(output / "destination.sqlite"),
                "--evidence",
                str(webhook_path),
            ],
            cwd=output,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process_identity = {
                "pid": process.pid,
                "start_token": foundation._linux_process_start_token(process.pid),
            }
            foundation._wait_port(process, port)
            receipt_exit, receipt = core._client_result(
                _client_command(
                    RECEIPT, node=node, install=install, version=version, endpoint=endpoint
                ),
                cwd=output,
                token=TOKEN,
                stdout_path=receipt_out,
                stderr_path=receipt_err,
            )
            if receipt_exit != 0 or receipt is None or receipt.get("status") != "passed":
                issues.append("receipt_or_exact_read_failed")
            else:
                buyer_exit, buyer = core._client_result(
                    [
                        *_client_command(
                            BUYER, node=node, install=install, version=version, endpoint=endpoint
                        ),
                        "--input",
                        str(scenario_path),
                    ],
                    cwd=output,
                    token=TOKEN,
                    stdout_path=buyer_out,
                    stderr_path=buyer_err,
                )
                if buyer_exit != 0 or buyer is None or buyer.get("status") != "passed":
                    issues.append("semantic_reconciliation_failed")
        except (
            core.MatrixError,
            foundation.HarnessError,
            OSError,
            subprocess.TimeoutExpired,
        ) as error:
            issues.append(f"cell_process_failed:{type(error).__name__}:{error}")
        finally:
            # Shutdown verifies the durable notification retry against PostgreSQL.
            # Give that bounded work time to write evidence before SIGKILL.
            foundation._stop(process, grace_seconds=90)
    if webhook_path.is_file():
        try:
            webhook = json.loads(webhook_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            issues.append("webhook_evidence_invalid")
    else:
        issues.append("webhook_replay_not_completed")
    positive = not issues and _full_result(receipt=receipt, buyer=buyer, webhook=webhook)
    if not positive and not issues:
        issues.append("full_lifecycle_assertion_failed")
    retained = [scenario_path, seller_out, seller_err]
    retained.extend(
        path
        for path in (receipt_out, receipt_err, buyer_out, buyer_err, webhook_path)
        if path.is_file()
    )
    return {
        "id": cell["id"],
        "python_role": cell["python_role"],
        "typescript_role": cell["typescript_role"],
        "database_identity": database_identity,
        "seller_process_identity": process_identity,
        "output_directory": output.name,
        "positive_full_lifecycle": positive,
        "issues": issues,
        "receipt": receipt,
        "buyer": buyer,
        "webhook": webhook,
        "evidence": [core._retained(path, output) for path in retained],
    }


def main() -> None:
    core.foundation = core._load_foundation()
    foundation = core.foundation
    args = core._arguments()
    contract = core._load_contract(args.contract)
    protocol_source = json.loads(PINS.read_text(encoding="utf-8"))["protocol"]["required_contract"]
    if protocol_source["version"] != contract["artifacts"]["python"]["candidate"]["protocol"]:
        raise core.MatrixError("installed candidate protocol differs from the signed source pin")
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
    identities = core._validate_artifact_contract(
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
            output = args.output / cell["id"]
            output.mkdir()
            database = f"adcp_1199_full_{run_id}_{index}"
            created.append(database)
            rows.append(
                _run_cell(
                    cell,
                    contract=contract,
                    python_input=python_inputs[cell["python_role"]],
                    typescript_input=typescript_inputs[cell["typescript_role"]],
                    node=args.node_runtime,
                    admin_url=args.pg_admin_url,
                    database=database,
                    output=output,
                )
            )
    finally:
        if not args.keep_databases:
            foundation._drop_databases(args.pg_admin_url, created)
    isolation_errors = core._cross_cell_isolation_errors(rows)
    accepted = _blocking_acceptance(rows)
    report = {
        "schema_version": 1,
        "issue": 1199,
        "run_id": run_id,
        "status": "passed" if accepted else "failed",
        "blocking_acceptance": accepted,
        "declared_artifacts": contract["artifacts"],
        "protocol_source": protocol_source,
        "artifact_identities": identities,
        "cross_cell_isolation_errors": isolation_errors,
        "cells": rows,
        "harness": {
            **foundation._git_identity(),
            "entrypoints": {
                name: core._retained(path, ROOT)
                for name, path in {
                    "seller": SELLER,
                    "receipt": RECEIPT,
                    "buyer": BUYER,
                    "scenario": SCENARIO,
                }.items()
            },
            "contract": core._retained(args.contract, ROOT),
            "protocol_pins": core._retained(PINS, ROOT),
        },
    }
    (args.output / "results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "run_id": run_id,
                "cells": [
                    {
                        "id": row["id"],
                        "positive_full_lifecycle": row["positive_full_lifecycle"],
                        "issues": row["issues"],
                    }
                    for row in rows
                ],
            },
            sort_keys=True,
        )
    )
    if not accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
