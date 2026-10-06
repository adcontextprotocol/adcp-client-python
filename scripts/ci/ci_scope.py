"""Select CI lanes from a complete PR diff; unclassified inputs require full CI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

DOCUMENTATION_FILES = {"AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "CONTRIBUTING.md", "LADON.md"}
SHA = re.compile(r"[0-9a-f]{40}")
STABLE_VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
PRERELEASE = re.compile(r"(" + STABLE_VERSION + r")(a|b|rc)(0|[1-9][0-9]*)")
PROJECT_VERSION = re.compile(
    r'(version\s*=\s*")(' + STABLE_VERSION + r'(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?)("\s*)'
)
RELEASE_FILES = {"pyproject.toml", ".release-please-manifest.json", "CHANGELOG.md"}
RELEASE_BRANCHES = {
    "release-please--branches--main",
    "release-please--branches--main--components--adcp",
}

# Keep required-check names stable; aggregate gates verify each selected lane.
JOB_GROUPS = {
    "test": "run_tests",
    "schema-check": "run_tests",
    "pg-conformance": "run_postgres",
    "pg-reporting-status": "run_postgres",
    "pg-reporting-materializer": "run_postgres",
    "pg-reporting-receipts": "run_postgres",
    "pg-reporting-receipt-compatibility": "run_postgres",
    "pg-reporting-feed": "run_postgres",
    "pg-reporting-feed-compatibility": "run_postgres",
    "pg-reporting-feed-installed": "run_postgres",
    "pg-reporting-production": "run_postgres",
    "pg-reporting-production-installed": "run_postgres",
    "pg-reporting-production-compatibility": "run_postgres",
    "downstream-imports": "run_packaging",
    "reporting-installed-artifact-matrix": "run_packaging",
    "storyboard": "run_storyboards",
    "v3-reference-seller-tests": "run_storyboards",
    "storyboard-v3-reference-seller": "run_storyboards",
    "storyboard-multi-platform-seller": "run_storyboards",
    "storyboard-sales-proposal-mode": "run_storyboards",
}


@dataclass(frozen=True)
class Scope:
    run_tests: bool = False
    full_matrix: bool = False
    run_postgres: bool = False
    run_packaging: bool = False
    run_storyboards: bool = False
    release_metadata: bool = False

    @classmethod
    def full(cls) -> Scope:
        return cls(True, True, True, True, True)

    def expected_results(self) -> dict[str, str]:
        return {
            job: "success" if getattr(self, group) else "skipped"
            for job, group in JOB_GROUPS.items()
        }


def scope_for_paths(paths: list[str]) -> Scope:
    if not paths:
        return Scope.full()
    selected = Scope()
    for path in paths:
        if path in DOCUMENTATION_FILES or (path.startswith("docs/") and path.endswith(".md")):
            continue
        if path.startswith("examples/"):
            current = Scope(run_tests=True, run_storyboards=True)
        elif path.startswith(
            (
                "src/adcp/reporting/",
                "src/adcp/decisioning/",
                "src/adcp/signing/",
                "src/adcp/server/",
                "src/adcp/protocols/",
                "src/adcp/notification_outbox",
                "tests/conformance/reporting/",
                "tests/conformance/decisioning/",
                "tests/conformance/signing/",
            )
        ) or path.startswith(
            (
                "tests/test_reporting_",
                "tests/test_decisioning_",
                "tests/test_notification_outbox",
                "src/adcp/webhook",
            )
        ):
            current = Scope(True, False, True, True, True)
        elif path in {
            "src/adcp/client.py",
            "src/adcp/simple.py",
            "src/adcp/accounts.py",
        } or path.startswith("src/adcp/compat/"):
            current = Scope(run_tests=True, run_packaging=True, run_storyboards=True)
        elif path == "src/adcp/adagents.py":
            current = Scope(run_tests=True)
        elif (
            path.startswith("tests/test_")
            and "/" not in path.removeprefix("tests/")
            and path.endswith(".py")
            and not path.startswith(
                (
                    "tests/test_schema",
                    "tests/test_ci_",
                    "tests/test_native_ci",
                    "tests/test_release",
                    "tests/test_main_release",
                    "tests/test_version",
                    "tests/test_public_api",
                )
            )
        ):
            # Test-only edits still run the complete canonical suite. Runtime
            # edits in the same PR contribute their own specialized lanes.
            current = Scope(run_tests=True)
        else:
            # Schemas, shared types, fixtures, dependencies, build/CI files,
            # renames out of known paths, and newly introduced paths fail closed.
            return Scope.full()
        selected = Scope(
            **{name: value or getattr(current, name) for name, value in asdict(selected).items()}
        )
    return selected


def project_version(text: str) -> tuple[str, str] | None:
    """Remove one normalized [project] version while preserving every other byte."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return None  # The CI scope host has stdlib TOML; older hosts fail closed.
    try:
        version = tomllib.loads(text).get("project", {}).get("version")
    except (ValueError, AttributeError):
        return None
    section = ""
    found: list[str] = []
    normalized: list[str] = []
    for line in text.splitlines(keepends=True):
        heading = re.fullmatch(r"\[([^\]]+)\]\s*", line)
        if heading:
            section = heading[1]
        match = PROJECT_VERSION.fullmatch(line) if section == "project" else None
        if match:
            found.append(match[2])
            line = match[1] + "<release-version>" + match[3]
        normalized.append(line)
    if len(found) != 1 or found[0] != version:
        return None
    return found[0], "".join(normalized)


def release_version(version: str) -> tuple[tuple[int, ...], str]:
    """Compare supported PEP 440 versions and recover the SemVer manifest value.

    This script runs alone in CI's sparse checkout, before installing any
    dependencies. Accept exactly the stable and alpha/beta/rc spellings emitted
    by the release normalizer; every other version requires full CI.
    """
    match = PRERELEASE.fullmatch(version)
    if match:
        release, label, number = match.groups()
        labels = {"a": (0, "alpha"), "b": (1, "beta"), "rc": (2, "rc")}
        rank, semver_label = labels[label]
        return (
            (*map(int, release.split(".")), rank, int(number)),
            f"{release}-{semver_label}.{number}",
        )
    if not re.fullmatch(STABLE_VERSION, version):
        raise ValueError("unsupported release version")
    return (*map(int, version.split(".")), 3, 0), version


def is_release_metadata(event: dict, paths: list[str], *, cwd: Path | None) -> bool:
    """Require a same-repository bot PR whose complete diff is version-only."""
    pr = event["pull_request"]
    repository = "adcontextprotocol/adcp-client-python"
    if (
        set(paths) != RELEASE_FILES
        or pr.get("base", {}).get("ref") != "main"
        or pr.get("head", {}).get("ref") not in RELEASE_BRANCHES
        or pr.get("base", {}).get("repo", {}).get("full_name") != repository
        or pr.get("head", {}).get("repo", {}).get("full_name") != repository
        or pr.get("user", {}).get("type") != "Bot"
        or pr.get("user", {}).get("login") not in {"aao-ipr-bot[bot]", "github-actions[bot]"}
    ):
        return False

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=cwd, timeout=30).decode("utf-8")

    base, head = pr["base"]["sha"], pr["head"]["sha"]
    for sha in (base, head):
        entries = git("ls-tree", "-z", sha, "--", *sorted(RELEASE_FILES)).split("\0")
        if len([entry for entry in entries if entry]) != len(RELEASE_FILES):
            return False
        if any(not entry.startswith("100644 blob ") for entry in entries if entry):
            return False
    before = project_version(git("show", f"{base}:pyproject.toml"))
    after = project_version(git("show", f"{head}:pyproject.toml"))
    if before is None or after is None or before[1] != after[1]:
        return False
    before_order, before_manifest = release_version(before[0])
    after_order, after_manifest = release_version(after[0])
    if after_order <= before_order:
        return False
    return json.loads(git("show", f"{base}:.release-please-manifest.json")) == {
        ".": before_manifest
    } and json.loads(git("show", f"{head}:.release-please-manifest.json")) == {".": after_manifest}


def select_scope(event_name: str, event: dict, *, cwd: Path | None = None) -> Scope:
    if event_name != "pull_request":
        return Scope.full()
    try:
        base = event["pull_request"]["base"]["sha"]
        head = event["pull_request"]["head"]["sha"]
        if not isinstance(base, str) or not isinstance(head, str):
            return Scope.full()
        if not SHA.fullmatch(base) or not SHA.fullmatch(head):
            return Scope.full()
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, head],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=30,
        )
        paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
        if is_release_metadata(event, paths, cwd=cwd):
            return Scope(run_tests=True, release_metadata=True)
        return scope_for_paths(paths)
    except (KeyError, TypeError, AttributeError, ValueError, OSError, subprocess.SubprocessError):
        return Scope.full()  # Missing history or metadata must never suppress tests.


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        scope = select_scope(os.environ["GITHUB_EVENT_NAME"], event)
    except (KeyError, OSError, ValueError):
        scope = Scope.full()
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, value in asdict(scope).items():
            output.write(f"{name}={str(value).lower()}\n")
        output.write(
            "expected_results=" + json.dumps(scope.expected_results(), sort_keys=True) + "\n"
        )
    print(json.dumps(asdict(scope), sort_keys=True))


if __name__ == "__main__":
    main()
