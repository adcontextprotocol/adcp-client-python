"""Real diffs and aggregate checks must never silently bypass SDK failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_scope


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI Fixture")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/package.py").write_text("VALUE = 1\n")
    scope = tmp_path / "scripts/ci/ci_scope.py"
    scope.parent.mkdir(parents=True)
    scope.write_text(Path(ci_scope.__file__).read_text())
    git("add", ".")
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    return tmp_path, git("rev-parse", "HEAD")


def commit(repository: tuple[Path, str]) -> dict:
    root, base = repository
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "change",
        ],
        cwd=root,
        check=True,
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("docs/guide.md", False),
        ("CONTRIBUTING.md", False),
        ("README.md", True),
        ("MIGRATION.md", True),
        ("LICENSE", True),
        ("docs/example.py", True),
        ("src/package.py", True),
        ("schemas/cache/3.2/core/example.json", True),
        (".github/workflows/ci.yml", True),
        ("scripts/ci/ci_scope.py", True),
        ("pyproject.toml", True),
        ("unexpected-file", True),
        ("docs/a name\nwith a newline.md", False),
    ],
)
def test_scope_from_complete_git_diff(
    repository: tuple[Path, str], path: str, expected: bool
) -> None:
    root, _ = repository
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    event = commit(repository)
    assert ci_scope.select_scope("pull_request", event, cwd=root).run_tests is expected
    assert ci_scope.select_scope("push", event, cwd=root) == ci_scope.Scope.full()


def test_rename_from_sdk_to_documentation_still_runs_tests(repository: tuple[Path, str]) -> None:
    root, _ = repository
    (root / "docs").mkdir()
    (root / "src/package.py").rename(root / "docs/removed-code.md")
    assert (
        ci_scope.select_scope("pull_request", commit(repository), cwd=root) == ci_scope.Scope.full()
    )


def test_large_documentation_diff_cannot_hide_source_change(repository: tuple[Path, str]) -> None:
    root, _ = repository
    (root / "docs").mkdir()
    for i in range(350):
        (root / f"docs/guide-{i}.md").write_text("documentation\n")
    (root / "src/package.py").write_text("VALUE = 2\n")
    assert (
        ci_scope.select_scope("pull_request", commit(repository), cwd=root) == ci_scope.Scope.full()
    )


def test_missing_history_and_empty_diffs_require_full_matrix(repository: tuple[Path, str]) -> None:
    root, base = repository
    for event in (
        {},
        {"pull_request": {"base": {"sha": base}, "head": {"sha": base}}},
        {"pull_request": {"base": {"sha": "f" * 40}, "head": {"sha": base}}},
        {"pull_request": {"base": {"sha": "--help"}, "head": {"sha": base}}},
        {"pull_request": {"base": {"sha": None}, "head": {"sha": base}}},
    ):
        assert ci_scope.select_scope("pull_request", event, cwd=root) == ci_scope.Scope.full()


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["docs/guide.md"], ci_scope.Scope()),
        (["src/adcp/adagents.py"], ci_scope.Scope(run_tests=True)),
        (["tests/test_client.py"], ci_scope.Scope(run_tests=True)),
        (["examples/seller_agent.py"], ci_scope.Scope(run_tests=True, run_storyboards=True)),
        (["src/adcp/client.py"], ci_scope.Scope(True, False, False, True, True)),
        (["src/adcp/compat/legacy/v2_5/client.py"], ci_scope.Scope(True, False, False, True, True)),
        (["src/adcp/reporting/outbox/status.py"], ci_scope.Scope(True, False, True, True, True)),
        (["src/adcp/server/serve.py"], ci_scope.Scope(True, False, True, True, True)),
        (["src/adcp/signing/replay.py"], ci_scope.Scope(True, False, True, True, True)),
        (["src/adcp/notification_outbox_pg.py"], ci_scope.Scope(True, False, True, True, True)),
        (
            ["tests/test_notification_outbox_artifacts.py"],
            ci_scope.Scope(True, False, True, True, True),
        ),
        (["src/adcp/decisioning/platform.py"], ci_scope.Scope(True, False, True, True, True)),
        (
            ["tests/conformance/reporting/test_status.py"],
            ci_scope.Scope(True, False, True, True, True),
        ),
        (
            ["docs/guide.md", "src/adcp/adagents.py", "examples/seller_agent.py"],
            ci_scope.Scope(run_tests=True, run_storyboards=True),
        ),
        (["src/adcp/client.py", "schemas/cache/3.2/request.json"], ci_scope.Scope.full()),
        (["src/adcp/types/guards.py"], ci_scope.Scope.full()),
        (["src/adcp/__init__.py"], ci_scope.Scope.full()),
        (["src/adcp/utils/http.py"], ci_scope.Scope.full()),
        (["pyproject.toml"], ci_scope.Scope.full()),
        (["README.md"], ci_scope.Scope.full()),
        (["MIGRATION_4.md"], ci_scope.Scope.full()),
        ([".github/workflows/ci.yml"], ci_scope.Scope.full()),
        (["tests/conftest.py"], ci_scope.Scope.full()),
        (["tests/fixtures/example.json"], ci_scope.Scope.full()),
        (["new-runtime/module.py"], ci_scope.Scope.full()),
        ([], ci_scope.Scope.full()),
    ],
)
def test_specialized_scope_is_explicit_and_unknown_inputs_require_every_lane(
    repository: tuple[Path, str], paths: list[str], expected: ci_scope.Scope
) -> None:
    root, _ = repository
    if not paths:
        assert ci_scope.scope_for_paths(paths) == expected
        return
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n")
    event = commit(repository)
    assert ci_scope.select_scope("pull_request", event, cwd=root) == expected
    for event_name in ("push", "workflow_dispatch", "release", "schedule", "merge_group"):
        assert ci_scope.select_scope(event_name, event, cwd=root) == ci_scope.Scope.full()


@pytest.mark.parametrize("path,expected", [("docs/guide.md", "false"), ("src/package.py", "true")])
def test_scope_cli_in_shallow_sparse_pr_merge_checkout(
    repository: tuple[Path, str], tmp_path: Path, path: str, expected: str
) -> None:
    root, base = repository
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    event = commit(repository)
    head = event["pull_request"]["head"]["sha"]
    subprocess.run(["git", "checkout", "-qb", "integration", base], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--no-ff",
            "-qm",
            "merge",
            head,
        ],
        cwd=root,
        check=True,
    )
    clone = tmp_path / "sparse-checkout"
    subprocess.run(
        ["git", "clone", "--depth=2", "--no-checkout", root.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "sparse-checkout", "set", "--no-cone", "scripts/ci/ci_scope.py"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "-q", "HEAD"], cwd=clone, check=True)
    assert not (clone / "src/package.py").exists()
    event_path, output = tmp_path / "event.json", tmp_path / "scope-output"
    event_path.write_text(json.dumps(event))
    env = dict(
        os.environ,
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_EVENT_PATH=str(event_path),
        GITHUB_OUTPUT=str(output),
    )
    result = subprocess.run(
        [sys.executable, "scripts/ci/ci_scope.py"], cwd=clone, env=env, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["run_tests"] == expected
    assert values["full_matrix"] == expected
    results = json.loads(values["expected_results"])
    assert set(results.values()) == {"success" if expected == "true" else "skipped"}


@pytest.mark.parametrize(
    "gate",
    [
        "pg-conformance-required-gate",
        "storyboard-required-gate",
        "reporting-installed-artifact-required-gate",
    ],
)
@pytest.mark.parametrize(
    "scope_result,decision,lane_result,expected_success",
    [
        ("success", "false", "skipped", True),
        ("success", "true", "success", True),
        ("success", "true", "skipped", False),
        ("success", "true", "failure", False),
        ("success", "true", "cancelled", False),
        ("success", "false", "failure", False),
        ("failure", "false", "skipped", False),
        ("cancelled", "false", "skipped", False),
        ("success", "", "skipped", False),
    ],
)
def test_actual_required_gate_cannot_hide_failures(
    gate: str, scope_result: str, decision: str, lane_result: str, expected_success: bool
) -> None:
    root = Path(__file__).resolve().parent.parent
    workflow = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"][gate]
    needs = {name: {"result": lane_result} for name in job["needs"]}
    expected = ci_scope.Scope.full() if decision == "true" else ci_scope.Scope()
    needs["changes"] = {
        "result": scope_result,
        "outputs": {
            "expected_results": json.dumps(expected.expected_results()) if decision else "{}"
        },
    }
    env = dict(os.environ, CI_NEEDS=json.dumps(needs))
    result = subprocess.run(["bash", "-c", job["steps"][0]["run"]], env=env, capture_output=True)
    assert (result.returncode == 0) is expected_success, result.stderr


@pytest.mark.parametrize(
    "corrupt", ["none", "failed-selected", "ran-unselected", "missing", "invalid"]
)
def test_postgres_gate_accepts_mixed_scopes_and_rejects_incorrect_results(corrupt: str) -> None:
    root = Path(__file__).resolve().parent.parent
    workflow = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["pg-conformance-required-gate"]
    selected = ci_scope.Scope(run_tests=True, run_packaging=True).expected_results()
    needs = {name: {"result": selected[name]} for name in job["needs"] if name != "changes"}
    if corrupt == "failed-selected":
        needs["reporting-installed-artifact-matrix"]["result"] = "failure"
    elif corrupt == "ran-unselected":
        needs["pg-conformance"]["result"] = "success"
    elif corrupt == "missing":
        selected.pop("pg-conformance")
    elif corrupt == "invalid":
        selected["pg-conformance"] = "cancelled"
    needs["changes"] = {"result": "success", "outputs": {"expected_results": json.dumps(selected)}}
    result = subprocess.run(
        ["bash", "-c", job["steps"][0]["run"]],
        env=dict(os.environ, CI_NEEDS=json.dumps(needs)),
        capture_output=True,
    )
    assert (result.returncode == 0) is (corrupt == "none"), result.stderr


def test_every_sdk_lane_uses_verified_scope_and_policy_jobs_always_run() -> None:
    root = Path(__file__).resolve().parent.parent
    workflow = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    policy_jobs = {"workflow-security", "ipr-policy", "conventional-commits", "changes"}
    for name, job in workflow["jobs"].items():
        if name in policy_jobs:
            assert "needs" not in job
        elif not name.endswith("required-gate"):
            assert job["needs"] == "changes"
            assert job["if"] == f"needs.changes.outputs.{ci_scope.JOB_GROUPS[name]} == 'true'"
    assert set(ci_scope.JOB_GROUPS) == {
        name
        for name in workflow["jobs"]
        if name not in policy_jobs and not name.endswith("required-gate")
    }


def test_scope_cli_falls_back_to_full_matrix_on_unreadable_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    ci_scope.main()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["full_matrix"] == "true"
    assert json.loads(values["expected_results"]) == ci_scope.Scope.full().expected_results()


@pytest.mark.parametrize("explicit_cache,exit_code", [(False, 0), (True, 0), (False, 7)])
def test_harness_cache_is_private_preserves_caller_override_and_exit_status(
    tmp_path: Path, explicit_cache: bool, exit_code: int
) -> None:
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, TMPDIR=str(tmp_path))
    env.pop("ADCP_REPORTING_DISTRIBUTION", None)
    if explicit_cache:
        env["ADCP_REPORTING_DISTRIBUTION"] = str(tmp_path / "caller-cache")
    probe = (
        "import json,os,sys;print(json.dumps({'cache':os.environ['ADCP_REPORTING_DISTRIBUTION']}));"
        f"sys.exit({exit_code})"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/reporting_test_harness.py"),
            sys.executable,
            "-c",
            probe,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == exit_code, result.stderr
    header, child, footer = [json.loads(line) for line in result.stdout.splitlines()]
    harness = Path(header["reporting_harness"])
    assert harness.is_relative_to(tmp_path)
    assert harness.stat().st_mode & 0o777 == 0o700
    assert child["cache"] == str(
        tmp_path / "caller-cache" if explicit_cache else harness / "distribution"
    )
    assert footer["exit_status"] == exit_code
