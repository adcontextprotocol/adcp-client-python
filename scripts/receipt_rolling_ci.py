"""Fail-closed orchestration for the eight unchanged receipt rolling tests.

The 15-minute child deadline plus 30-second termination allowance is below the
18-minute step and unchanged 35-minute job budget. Only allowlisted progress,
build identities and completed scenario projections are uploaded. Raw output
and JUnit remain private in runner.temp. Platform hard cancellation can prevent
upload entirely; missing or incomplete artifacts are fatal to the aggregate.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/conformance/reporting/test_reporting_receipt_rolling.py"
FUNCTION = "test_actual_old_readers_and_writers_before_and_after_receipt_migration"
NODE = TEST + "::" + FUNCTION
PINS = {
    "beta15": "3e76aa54623529a3dda01cd690b8a5c287c75641",
    "records": "3c405a21f978ed9d3208611bb4a7a8434a056933",
    "integration": "037de4ac822ecefb2f95d32c15c297fb4c45d683",
    "a": "21bf443e7d850d1800ec8a6f2e4abec1c8f85541",
    "b": "198d50e61c74fb82aedbf2c77e06a0e200b91db6",
    "c": "ea150fabd5ad90e3abf93f89729d2919f1c61798",
    "b1": "1c91311ec28d25506d5db43f59d0c34936ecb8f7",
    "b21": "8e18ca12b9a0c3750f80aa822058c02982ab3e52",
}
IDS = tuple(PINS)
FILES = {"attempted.json", "collection.json", "manifest.json", "stdout.log", "stderr.log"}
RUN_COMMAND = "python scripts/receipt_rolling_ci.py run"


class ContractError(Exception):
    """Fixed diagnostic codes only; never expose provider/output values."""


def require(condition, code):
    if not condition:
        raise ContractError(code)


def closed(value, keys, code):
    require(type(value) is dict and set(value) == set(keys), code)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def invalid_number(_):
        raise ContractError("nonfinite_json")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_number)


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def node(artifact):
    require(artifact in PINS, "unknown_artifact")
    return NODE + "[" + artifact + "]"


def notifications(artifact):
    return [False, True] if artifact in {"b", "c", "b1", "b21"} else [False]


def identity(env):
    value = {
        key: env[name]
        for key, name in (
            ("source_head", "RECEIPT_SOURCE_HEAD"),
            ("event_sha", "GITHUB_SHA"),
            ("event_name", "GITHUB_EVENT_NAME"),
            ("run_id", "GITHUB_RUN_ID"),
            ("run_attempt", "GITHUB_RUN_ATTEMPT"),
            ("repository", "GITHUB_REPOSITORY"),
        )
    }
    require(value["repository"] == "adcontextprotocol/adcp-client-python", "wrong_repository")
    for key in ("source_head", "event_sha"):
        require(re.fullmatch(r"[0-9a-f]{40}", value[key]) is not None, "invalid_sha")
    for key in ("run_id", "run_attempt"):
        require(re.fullmatch(r"[1-9][0-9]{0,19}", value[key]) is not None, "invalid_run_identity")
    require(value["event_name"] in {"pull_request", "push"}, "unsupported_event")
    require(
        value["event_name"] != "push" or value["source_head"] == value["event_sha"],
        "push_sha_mismatch",
    )
    return value


def artifact_name(scope, artifact):
    return f"receipt-rolling-shard-{scope['run_id']}-{scope['run_attempt']}-{artifact}"


def verify_history(root=ROOT):
    def assigned(path, name):
        tree = ast.parse(path.read_text())
        values = [
            ast.literal_eval(s.value)
            for s in tree.body
            if isinstance(s, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in s.targets)
        ]
        require(len(values) == 1, "historical_assignment")
        return values[0]

    actual = assigned(
        root / "tests/conformance/reporting/test_reporting_materializer_rolling.py", "ARTIFACTS"
    )
    require(actual == {k: PINS[k] for k in IDS[:-1]}, "historical_artifact_pins")
    require(assigned(root / TEST, "B21") == PINS["b21"], "historical_b21_pin")


def verify_collection(rows):
    require(type(rows) is list and len(rows) == 8, "collection_count")
    expected = [{"node": node(a), "parameter": a} for a in IDS]
    require(rows == expected, "actual_node_parameter_inventory")


def collect_child():
    import pytest

    class Collector:
        rows = []

        def pytest_collection_finish(self, session):
            self.rows = [
                {
                    "node": item.nodeid,
                    "parameter": item.callspec.params["installed_receipt_history"],
                }
                for item in session.items
            ]

    collector = Collector()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = pytest.main(
            [TEST, "--collect-only", "-q", "--color=no", "-o", "addopts="], plugins=[collector]
        )
    require(code == 0, "collection_exit")
    verify_collection(collector.rows)
    print(json.dumps(collector.rows))


def collect():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "collect-child"],
        cwd=ROOT,
        capture_output=True,
        timeout=60,
        check=False,
    )
    require(result.returncode == 0, "collection_process_failed")
    rows = strict_json(result.stdout)
    verify_collection(rows)
    return rows


def verify_workflow(path=ROOT / ".github/workflows/ci.yml"):
    import yaml

    jobs = yaml.safe_load(path.read_text())["jobs"]
    shard = jobs.get("pg-reporting-receipt-compatibility-shard")
    require(type(shard) is dict, "missing_shard_job")
    require(
        shard["strategy"] == {"fail-fast": False, "matrix": {"artifact": list(IDS)}},
        "workflow_matrix",
    )
    require(
        type(shard["timeout-minutes"]) is int
        and shard["timeout-minutes"] == 35
        and not shard.get("continue-on-error", False),
        "shard_budget_or_masking",
    )
    service = shard["services"]["postgres"]
    require(service["image"] == "postgres:16", "postgres_version")
    require(
        service["env"]["POSTGRES_INITDB_ARGS"] == "--encoding=UTF8 --lc-collate=C --lc-ctype=C",
        "postgres_locale",
    )
    require(
        shard["env"]["RECEIPT_SOURCE_HEAD"]
        == "${{ github.event.pull_request.head.sha || github.sha }}",
        "source_head_expression",
    )
    steps = shard["steps"]
    require(
        sum(s["timeout-minutes"] for s in steps) < shard["timeout-minutes"], "no_upload_headroom"
    )
    require(not any(s.get("continue-on-error", False) for s in steps), "masked_step")
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    require(checkout["with"]["ref"] == "${{ env.RECEIPT_SOURCE_HEAD }}", "checkout_source_identity")
    execution = [s for s in steps if RUN_COMMAND in s.get("run", "")]
    require(
        len(execution) == 1 and execution[0]["run"].splitlines()[-1] == RUN_COMMAND,
        "selected_runner_command",
    )
    require(execution[0]["timeout-minutes"] == 18, "test_step_budget")
    upload = steps[-1]
    require(
        upload["if"] == "always()" and upload["with"]["if-no-files-found"] == "error",
        "upload_not_fail_closed",
    )
    require(
        upload["with"]["name"]
        == (
            "receipt-rolling-shard-${{ github.run_id }}-"
            "${{ github.run_attempt }}-${{ matrix.artifact }}"
        ),
        "artifact_namespace",
    )
    gate = jobs["pg-reporting-receipt-compatibility"]
    require(
        gate["name"] == "Receipt rolling compatibility (eight actual artifacts)",
        "required_context_changed",
    )
    require(gate["needs"] == "pg-reporting-receipt-compatibility-shard", "aggregate_dependency")
    require(gate["if"] == "${{ always() && !cancelled() }}", "aggregate_condition")
    require(not gate.get("continue-on-error", False), "aggregate_masked")
    require(
        gate["env"]["RECEIPT_SOURCE_HEAD"] == shard["env"]["RECEIPT_SOURCE_HEAD"],
        "aggregate_source_expression",
    )
    gate_checkout = next(
        s for s in gate["steps"] if s.get("uses", "").startswith("actions/checkout@")
    )
    require(gate_checkout["with"]["ref"] == checkout["with"]["ref"], "aggregate_source_checkout")
    download = next(s for s in gate["steps"] if s.get("id") == "shard-download")
    require(download["with"]["merge-multiple"] is False, "merged_artifact_directories")
    require(download["with"]["digest-mismatch"] == "error", "download_digest_not_enforced")
    require(
        download["with"]["pattern"]
        == "receipt-rolling-shard-${{ github.run_id }}-${{ github.run_attempt }}-*",
        "download_attempt_namespace",
    )
    check = gate["steps"][-1]
    require(
        check["env"]["RECEIPT_MATRIX_RESULT"]
        == "${{ needs.pg-reporting-receipt-compatibility-shard.result }}",
        "matrix_result_missing",
    )
    require(
        check["env"]["RECEIPT_DOWNLOAD_RESULT"] == "${{ steps.shard-download.outcome }}",
        "download_result_missing",
    )


def junit_result(path, artifact):
    raw = path.read_bytes()
    require(
        len(raw) <= 1048576 and b"<!DOCTYPE" not in raw and b"<!ENTITY" not in raw, "unsafe_junit"
    )
    tree = ET.fromstring(raw)
    suites = tree.findall(".//testsuite")
    cases = tree.findall(".//testcase")
    require(len(suites) == 1 and len(cases) == 1, "junit_case_count")
    suite, case = suites[0], cases[0]
    require(
        all(
            suite.get(k) == v
            for k, v in {"tests": "1", "errors": "0", "failures": "0", "skipped": "0"}.items()
        ),
        "junit_not_one_pass",
    )
    require(case.get("name") == FUNCTION + "[" + artifact + "]", "junit_wrong_node")
    require(case.get("classname") == TEST[:-3].replace("/", "."), "junit_wrong_module")
    require(
        not any(case.find(k) is not None for k in ("failure", "error", "skipped")),
        "junit_case_failed",
    )
    return {"node": node(artifact), "passed": 1, "failed": 0, "errors": 0, "skipped": 0}


class SafeOutput:
    """Upload a closed projection; unknown stdout/stderr never becomes public."""

    def __init__(self, artifact):
        self.artifact = artifact
        self.scenarios = []
        self.builds = []

    def line(self, raw):
        text = raw.decode("utf-8", errors="replace").strip()
        if text.startswith(node(self.artifact)) and re.fullmatch(
            re.escape(node(self.artifact)) + r" (PASSED|FAILED|ERROR|SKIPPED)( \[100%\])?", text
        ):
            return (text + "\n").encode()
        if re.fullmatch(
            r"=+ (?:[0-9]+ (?:passed|failed|skipped|deselected|xfailed|xpassed|errors?|warnings?)"
            r"(?:, )?)+ in [0-9.]+s(?: \([0-9:]+\))? =+",
            text,
        ):
            return (text + "\n").encode()
        if text.startswith(node(self.artifact) + " "):
            text = text[len(node(self.artifact)) + 1 :]
        try:
            data = strict_json(text)
        except (ValueError, ContractError):
            return b"[non-allowlisted output omitted]\n"
        if type(data) is dict and set(data) == {"frozen_build"}:
            source = data["frozen_build"]
            require(type(source) is dict, "build_output_type")
            a = source.get("artifact")
            require(
                a in {self.artifact, "b21"} and source.get("sha") == PINS[a], "wrong_built_history"
            )
            digest = source.get("wheel_sha256")
            require(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest), "wheel_digest")
            projected = {"artifact": a, "pin": PINS[a], "wheel_sha256": digest}
            self.builds.append(projected)
            return (json.dumps({"frozen_build": projected}, sort_keys=True) + "\n").encode()
        if type(data) is dict and "receipt_rolling" in data:
            require(data["receipt_rolling"] == self.artifact, "wrong_scenario_artifact")
            require(type(data.get("notifications")) is bool, "scenario_notifications_type")
            require(data.get("parent") == PINS["b21"], "scenario_parent")
            require(
                all(
                    type(data.get(k)) is int and data[k] == n
                    for k, n in (
                        ("parent_manifest", 187),
                        ("receipt_count", 2),
                        ("capture_count", 2),
                    )
                ),
                "scenario_counts",
            )
            require(data.get("quarantine_preserved") is True, "scenario_incomplete")
            digest = data.get("wheel_sha256")
            require(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest), "scenario_wheel")
            projected = {
                "artifact": self.artifact,
                "notifications": data["notifications"],
                "pin": PINS[self.artifact],
                "parent": PINS["b21"],
                "wheel_sha256": digest,
                "before_after_exercises": 2,
            }
            self.scenarios.append(projected)
            return (json.dumps({"receipt_rolling": projected}, sort_keys=True) + "\n").encode()
        return b"[non-allowlisted output omitted]\n"


def verify_scenarios(artifact, scenarios, builds):
    require(
        type(scenarios) is list and len(scenarios) == len(notifications(artifact)), "scenario_count"
    )
    require(type(builds) is list, "build_inventory_type")
    for b in builds:
        closed(b, ("artifact", "pin", "wheel_sha256"), "build_fields")
        require(type(b["artifact"]) is str and b["artifact"] in PINS, "build_artifact")
    require(sorted(b["artifact"] for b in builds) == sorted({artifact, "b21"}), "build_inventory")
    for b in builds:
        closed(b, ("artifact", "pin", "wheel_sha256"), "build_fields")
        require(
            b["pin"] == PINS[b["artifact"]]
            and type(b["wheel_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", b["wheel_sha256"]),
            "build_identity",
        )
    wheel = next(b["wheel_sha256"] for b in builds if b["artifact"] == artifact)
    observed = []
    for s in scenarios:
        closed(
            s,
            (
                "artifact",
                "notifications",
                "pin",
                "parent",
                "wheel_sha256",
                "before_after_exercises",
            ),
            "scenario_fields",
        )
        require(type(s["notifications"]) is bool, "scenario_notifications_type")
        require(
            s["artifact"] == artifact
            and s["pin"] == PINS[artifact]
            and s["parent"] == PINS["b21"]
            and s["wheel_sha256"] == wheel,
            "scenario_identity",
        )
        require(
            type(s["before_after_exercises"]) is int and s["before_after_exercises"] == 2,
            "scenario_exercises",
        )
        observed.append(s["notifications"])
    require(observed == notifications(artifact), "notification_scenario_inventory")


def terminate_tree(process):
    # Capture descendants before their parent exits. PID start times guard reuse.
    table = {}
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                fields = (p / "stat").read_text().rsplit(") ", 1)[1].split()
                table[int(p.name)] = (int(fields[1]), fields[19])
            except (OSError, IndexError):
                pass
    owned = {process.pid}
    while True:
        expanded = owned | {pid for pid, (parent, _) in table.items() if parent in owned}
        if expanded == owned:
            break
        owned = expanded
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in sorted(owned, reverse=True):
            try:
                current = (
                    (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()[19]
                )
                if pid in table and current == table[pid][1]:
                    os.kill(pid, sig)
            except (OSError, IndexError):
                pass
        if sig == signal.SIGTERM:
            time.sleep(5)
    process.wait(timeout=20)


def execute(command, private, public, projection, timeout=900):
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    selector = selectors.DefaultSelector()
    for name in ("stdout", "stderr"):
        selector.register(getattr(process, name), selectors.EVENT_READ, name)
    raw_files = {n: (private / (n + ".log")).open("wb") for n in ("stdout", "stderr")}
    safe_files = {n: (public / (n + ".log")).open("wb") for n in ("stdout", "stderr")}
    buffers = {n: b"" for n in raw_files}
    timed_out = False
    drain_until = None
    try:
        while selector.get_map():
            if not timed_out and time.monotonic() - started >= timeout:
                timed_out = True
                terminate_tree(process)
                drain_until = time.monotonic() + 1
            if drain_until is not None and time.monotonic() >= drain_until:
                break
            for key, _ in selector.select(0.1):
                chunk = os.read(key.fd, 65536)
                name = key.data
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                raw_files[name].write(chunk)
                buffers[name] += chunk
                require(len(buffers[name]) <= 1048576, "oversized_output_line")
                while b"\n" in buffers[name]:
                    line, buffers[name] = buffers[name].split(b"\n", 1)
                    safe_files[name].write(
                        projection.line(line) if name == "stdout" else b"[stderr output omitted]\n"
                    )
                    safe_files[name].flush()
        code = process.wait(timeout=5)
    finally:
        if process.poll() is None and not timed_out:
            terminate_tree(process)
        selector.close()
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        for name, pending in buffers.items():
            if pending:
                safe_files[name].write(b"[incomplete output line omitted]\n")
        for stream in (*raw_files.values(), *safe_files.values()):
            stream.close()
    return {
        "exit": 124 if timed_out else code,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 3),
        "timeout_seconds": timeout,
    }


def file_identity(path):
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def run_shard():
    scope = identity(os.environ)
    artifact = os.environ["RECEIPT_ARTIFACT"]
    selected = node(artifact)
    evidence = Path(os.environ["RUNNER_TEMP"]) / "receipt-rolling-evidence"
    require(evidence.is_dir(), "missing_attempted_directory")
    attempted = strict_json((evidence / "attempted.json").read_bytes())
    require(attempted == {**scope, "artifact": artifact}, "attempted_identity")
    manifest = {
        "version": 1,
        "identity": scope,
        "artifact": artifact,
        "history_pin": PINS[artifact],
        "selected_node": selected,
        "checked_out_head": None,
        "complete": False,
        "execution": None,
        "command": None,
        "raw_outputs": {},
        "pytest": None,
        "scenarios": [],
        "builds": [],
        "files": {},
        "error": None,
    }
    write_json(evidence / "manifest.json", manifest)
    private = Path(tempfile.mkdtemp(prefix="receipt-shard-private-", dir=os.environ["RUNNER_TEMP"]))
    private.chmod(0o700)
    exit_code = 1
    try:
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        require(actual == scope["source_head"], "checked_out_head_mismatch")
        manifest["checked_out_head"] = actual
        verify_history()
        verify_workflow()
        rows = collect()
        write_json(evidence / "collection.json", rows)
        junit = private / "junit.xml"
        command = [
            sys.executable,
            "scripts/reporting_test_harness.py",
            sys.executable,
            "-m",
            "pytest",
            selected,
            "-v",
            "-s",
            "-ra",
            "--color=no",
            "--junitxml=" + str(junit),
        ]
        manifest["command"] = command
        projection = SafeOutput(artifact)
        manifest["execution"] = execute(command, private, evidence, projection)
        manifest["scenarios"], manifest["builds"] = projection.scenarios, projection.builds
        require(
            manifest["execution"]["exit"] == 0 and not manifest["execution"]["timed_out"],
            "pytest_nonzero_or_timeout",
        )
        manifest["pytest"] = junit_result(junit, artifact)
        verify_scenarios(artifact, projection.scenarios, projection.builds)
        manifest["complete"] = True
        exit_code = 0
    except ContractError as exc:
        manifest["error"] = str(exc)
    except Exception:
        # CLI boundary: malformed input or a child/control error must fail closed
        # without exposing exception values, source snippets or private output.
        manifest["error"] = "control_or_child_incomplete"
    finally:
        manifest["raw_outputs"] = {
            name: file_identity(private / name)
            for name in ("stdout.log", "stderr.log")
            if (private / name).is_file()
        }
        for name in ("stdout.log", "stderr.log"):
            if not (evidence / name).exists():
                (evidence / name).write_bytes(b"")
        manifest["files"] = {
            name: file_identity(evidence / name)
            for name in FILES - {"manifest.json"}
            if (evidence / name).is_file()
        }
        write_json(evidence / "manifest.json", manifest)
        if manifest["execution"] is not None and manifest["execution"]["exit"]:
            child_exit = manifest["execution"]["exit"]
            exit_code = child_exit if child_exit > 0 else 128 - child_exit
    print(
        json.dumps(
            {
                "artifact": artifact,
                "complete": manifest["complete"],
                "exit": exit_code,
                "error": manifest["error"],
            }
        )
    )
    return exit_code


def verify_artifacts(metadata, directory, scope, matrix_result, download_result):
    require(matrix_result == "success", "matrix_not_success")
    require(download_result == "success", "download_not_success")
    require(type(metadata) is list, "artifact_metadata_type")
    require(
        all(type(a) is dict and type(a.get("name")) is str for a in metadata),
        "artifact_metadata_shape",
    )
    prefix = f"receipt-rolling-shard-{scope['run_id']}-{scope['run_attempt']}-"
    rows = [a for a in metadata if a["name"].startswith(prefix)]
    expected = {artifact_name(scope, a): a for a in IDS}
    require(
        len(rows) == 8
        and len({a["id"] for a in rows}) == 8
        and len({a["name"] for a in rows}) == 8,
        "artifact_count_or_duplicate",
    )
    require({a["name"] for a in rows} == set(expected), "artifact_id_inventory")
    require(
        directory.is_dir()
        and not directory.is_symlink()
        and {p.name for p in directory.iterdir()} == set(expected),
        "download_directory_inventory",
    )
    for row in rows:
        require(type(row["id"]) is int and row["id"] > 0, "artifact_numeric_id")
        require(
            type(row["workflow_run"]) is dict and type(row["workflow_run"].get("id")) is int,
            "artifact_run_type",
        )
        require(
            row["expired"] is False and row["workflow_run"]["id"] == int(scope["run_id"]),
            "artifact_run_identity",
        )
        require(row["workflow_run"]["head_sha"] == scope["source_head"], "artifact_head_identity")
        path = directory / row["name"]
        require(path.is_dir() and not path.is_symlink(), "unsafe_artifact_directory")
        require({p.name for p in path.iterdir()} == FILES, "artifact_file_inventory")
        require(
            all(
                p.is_file() and not p.is_symlink() and p.stat().st_size <= 8388608
                for p in path.iterdir()
            ),
            "unsafe_artifact_file",
        )
        artifact = expected[row["name"]]
        require(
            strict_json((path / "attempted.json").read_bytes()) == {**scope, "artifact": artifact},
            "attempted_identity",
        )
        verify_collection(strict_json((path / "collection.json").read_bytes()))
        m = strict_json((path / "manifest.json").read_bytes())
        closed(
            m,
            (
                "version",
                "identity",
                "artifact",
                "history_pin",
                "selected_node",
                "checked_out_head",
                "complete",
                "execution",
                "command",
                "raw_outputs",
                "pytest",
                "scenarios",
                "builds",
                "files",
                "error",
            ),
            "manifest_fields",
        )
        require(
            type(m["version"]) is int and m["version"] == 1 and m["identity"] == scope,
            "manifest_identity",
        )
        require(
            m["artifact"] == artifact
            and m["history_pin"] == PINS[artifact]
            and m["selected_node"] == node(artifact),
            "manifest_selection",
        )
        require(
            m["checked_out_head"] == scope["source_head"]
            and m["complete"] is True
            and m["error"] is None,
            "manifest_incomplete",
        )
        closed(
            m["execution"], ("exit", "timed_out", "seconds", "timeout_seconds"), "execution_fields"
        )
        require(
            type(m["execution"]["timeout_seconds"]) is int
            and m["execution"]["timeout_seconds"] == 900,
            "execution_deadline",
        )
        require(
            type(m["execution"]["exit"]) is int
            and m["execution"]["exit"] == 0
            and m["execution"]["timed_out"] is False,
            "execution_not_success",
        )
        require(
            type(m["execution"]["seconds"]) in (int, float) and 0 < m["execution"]["seconds"] < 960,
            "execution_duration",
        )
        command = m["command"]
        require(
            type(command) is list and len(command) == 11 and all(type(a) is str for a in command),
            "command_shape",
        )
        require(
            command[0] == command[2] and Path(command[0]).name.startswith("python"),
            "command_interpreter",
        )
        require(
            command[1] == "scripts/reporting_test_harness.py"
            and command[3:10] == ["-m", "pytest", node(artifact), "-v", "-s", "-ra", "--color=no"],
            "command_selection",
        )
        require(
            command[10].startswith("--junitxml=") and command[10].endswith("/junit.xml"),
            "command_junit",
        )
        closed(m["raw_outputs"], ("stdout.log", "stderr.log"), "raw_output_inventory")
        for stream in m["raw_outputs"].values():
            closed(stream, ("bytes", "sha256"), "raw_output_fields")
            require(
                type(stream["bytes"]) is int
                and stream["bytes"] >= 0
                and type(stream["sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", stream["sha256"]),
                "raw_output_identity",
            )
        require(m["raw_outputs"]["stdout.log"]["bytes"] > 0, "empty_original_output")
        closed(m["pytest"], ("node", "passed", "failed", "errors", "skipped"), "pytest_fields")
        require(
            all(type(m["pytest"][k]) is int for k in ("passed", "failed", "errors", "skipped")),
            "pytest_count_types",
        )
        require(
            m["pytest"]
            == {"node": node(artifact), "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "pytest_not_one_pass",
        )
        verify_scenarios(artifact, m["scenarios"], m["builds"])
        closed(m["files"], FILES - {"manifest.json"}, "public_file_inventory")
        for blob in m["files"].values():
            closed(blob, ("bytes", "sha256"), "public_file_fields")
            require(
                type(blob["bytes"]) is int and blob["bytes"] >= 0 and type(blob["sha256"]) is str,
                "public_file_types",
            )
        require(m["files"]["stdout.log"]["bytes"] > 0, "empty_public_output")
        require(
            m["files"] == {name: file_identity(path / name) for name in FILES - {"manifest.json"}},
            "artifact_bytes_changed",
        )


def aggregate():
    scope = identity(os.environ)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(actual == scope["source_head"], "aggregate_checkout_identity")
    command = [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{scope['repository']}/actions/runs/{scope['run_id']}/artifacts?per_page=100",
    ]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    require(result.returncode == 0, "artifact_inventory_api_failed")
    pages = strict_json(result.stdout)
    require(type(pages) is list and bool(pages), "artifact_pages")
    rows = [row for page in pages for row in page["artifacts"]]
    require(
        all(page["total_count"] == len(rows) for page in pages), "artifact_pagination_incomplete"
    )
    verify_artifacts(
        rows,
        ROOT / "receipt-rolling-aggregate",
        scope,
        os.environ["RECEIPT_MATRIX_RESULT"],
        os.environ["RECEIPT_DOWNLOAD_RESULT"],
    )
    print(
        json.dumps(
            {
                "identity": scope,
                "shards": 8,
                "scenarios": 12,
                "before_after_exercises": 24,
                "complete": True,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "aggregate", "collect-child", "check"))
    mode = parser.parse_args().mode
    try:
        if mode == "run":
            return run_shard()
        if mode == "aggregate":
            aggregate()
        elif mode == "collect-child":
            collect_child()
        elif mode == "check":
            verify_history()
            verify_workflow()
            print(json.dumps({"actual_nodes_and_parameters": collect()}))
        return 0
    except ContractError as exc:
        print(json.dumps({"receipt_ci_error": str(exc)}))
    except Exception:
        print(json.dumps({"receipt_ci_error": "control_incomplete"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
