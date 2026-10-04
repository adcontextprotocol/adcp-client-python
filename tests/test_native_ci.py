"""The smaller matrix must retain every canonical test and combined coverage."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import coverage
import pytest
import yaml

from scripts.ci import run_native_tests as native


def test_canonical_partition_keeps_conformance_sequential_and_appends_coverage() -> None:
    unit, conformance = native.test_commands("full", coverage=True, workers=2)
    assert unit[:3] == ["tests/", "--ignore=tests/conformance", "--ignore=tests/integration"]
    assert unit[unit.index("-n") + 1] == "2"
    assert "--dist=loadfile" in unit
    assert conformance[:2] == ["tests/conformance/", "tests/integration/"]
    assert conformance[conformance.index("-n") + 1] == "0"
    assert "--cov-append" not in unit
    assert "--cov-append" in conformance
    assert "--cov-fail-under=0" in unit
    assert not any(argument.startswith("--cov-fail-under=") for argument in conformance)
    assert all("--cov=src/adcp" in command for command in (unit, conformance))


def test_compatibility_suite_checks_runtime_on_every_interpreter_without_adopter_rebuilds() -> None:
    (command,) = native.test_commands("compatibility")
    assert all(Path(path.split("::", 1)[0]).is_file() for path in native.COMPATIBILITY_TESTS)
    assert "tests/test_client.py" in command
    assert "tests/test_public_api.py" in command
    assert "tests/test_versioned_bases.py" in command
    assert not any(path.startswith("tests/conformance/") for path in command)
    assert command[command.index("-n") + 1] == "0"


@pytest.mark.parametrize(
    "mode,coverage_enabled,workers",
    [("unknown", False, 0), ("full", False, 8), ("compatibility", True, 0)],
)
def test_invalid_native_modes_are_rejected(mode: str, coverage_enabled: bool, workers: int) -> None:
    with pytest.raises(ValueError):
        native.test_commands(mode, coverage=coverage_enabled, workers=workers)


def test_native_runner_preserves_failure_and_does_not_run_later_phases(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    calls = root / "calls.jsonl"
    script = root / "scripts/reporting_test_harness.py"
    script.parent.mkdir()
    script.write_text(
        "import json,sys\n"
        f"with open({str(calls)!r}, 'a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "raise SystemExit(7)\n"
    )
    probe = (
        "from pathlib import Path; from scripts.ci import run_native_tests as n; "
        f"n.ROOT=Path({str(root)!r}); raise SystemExit(n.run_commands([['first'], ['second']]))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True)
    assert result.returncode == 7, result.stderr
    calls_recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(calls_recorded) == 1
    assert calls_recorded[0][-1] == "first"


def test_two_phases_collect_every_native_case_and_merge_real_worker_coverage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "src/adcp").mkdir(parents=True)
    (root / "src/adcp/__init__.py").touch()
    module = root / "src/adcp/probe.py"
    module.write_text(
        "def unit_value():\n    return 1\n\n"
        "def conformance_value():\n    return 2\n\n"
        "def integration_value():\n    return 3\n"
        "\ndef artifact_value():\n    return 4\n"
    )
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath=["src"]\n'
        'addopts="-m \'not integration\'"\nmarkers=["integration"]\n'
        "[tool.coverage.report]\nfail_under=100\n"
    )
    cases = {
        "tests/test_unit.py": "unit_value",
        "tests/conformance/test_conformance.py": "conformance_value",
        "tests/integration/test_unmarked.py": "integration_value",
    }
    for path, function in cases.items():
        test = root / path
        test.parent.mkdir(parents=True, exist_ok=True)
        test.write_text(
            f"from adcp.probe import {function}\ndef test_native(): assert {function}()\n"
        )
    (root / "tests/integration/test_live.py").write_text(
        "import pytest\n@pytest.mark.integration\ndef test_live(): assert False\n"
    )
    (root / "tests/test_fixture_artifacts.py").write_text(
        "from adcp.probe import artifact_value\n"
        "def test_serial_artifact(request):\n"
        "    assert not hasattr(request.config, 'workerinput')\n"
        "    assert artifact_value() == 4\n"
    )
    harness = root / "scripts/reporting_test_harness.py"
    harness.parent.mkdir()
    shutil.copy2(native.ROOT / "scripts/reporting_test_harness.py", harness)
    probe = (
        "from pathlib import Path; from scripts.ci import run_native_tests as n; "
        f"n.ROOT=Path({str(root)!r}); "
        "raise SystemExit(n.run_commands(n.test_commands('full', coverage=True, workers=2)))"
    )
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("COV_CORE_")
    }
    environment.pop("COVERAGE_PROCESS_START", None)
    environment["COVERAGE_FILE"] = str(root / ".coverage")
    result = subprocess.run(
        [sys.executable, "-c", probe], env=environment, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "3 passed, 1 deselected" in result.stdout
    measured = coverage.Coverage(data_file=str(root / ".coverage"))
    measured.load()
    _, executable, _, missing, _ = measured.analysis2(str(module))
    assert executable and not missing


def test_workflow_keeps_canonical_coverage_full_main_matrix_and_named_pr_checks() -> None:
    workflow = yaml.load(
        (native.ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    job = workflow["jobs"]["test"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.10", "3.11", "3.12", "3.13"]
    steps = {step.get("name"): step for step in job["steps"]}
    canonical = steps["Run canonical suite with coverage"]
    assert canonical["if"] == (
        "matrix.python-version == '3.12' && needs.changes.outputs.release_metadata != 'true'"
    )
    assert "--mode full --coverage" in canonical["run"]
    assert "--unit-workers 2" in canonical["run"]
    assert "--unit-workers 2" in steps["Run full native suite"]["run"]
    assert "needs.changes.outputs.full_matrix == 'true'" in steps["Run full native suite"]["if"]
    assert (
        "needs.changes.outputs.full_matrix != 'true'"
        in steps["Run interpreter compatibility checks"]["if"]
    )
    assert "--mode compatibility" in steps["Run interpreter compatibility checks"]["run"]
    assert "workflow_dispatch" in workflow["on"]
