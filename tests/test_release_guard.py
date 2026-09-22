"""Deterministic adversarial tests: no network, dispatch, tags or publication."""

from __future__ import annotations

import base64
import copy
import gzip
import io
import json
import os
import tarfile
import urllib.error
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import release_artifacts as artifacts
from scripts import release_gate as policy
from scripts import release_publish as publisher

ROOT = Path(__file__).resolve().parent.parent
TARGET = "a" * 40
TREE = "b" * 40
OTHER = "c" * 40
NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
STAMP = (NOW - timedelta(minutes=10)).isoformat()
VERSION = "1.2.3rc1"
REJECTED = policy.ReleaseRejectedError


class Clock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return NOW


def workflow_run(path: str, event: str, *, status: str = "completed") -> dict[str, Any]:
    return {
        "repository": {"full_name": policy.REPOSITORY},
        "head_repository": {"full_name": policy.REPOSITORY},
        "head_branch": "main",
        "head_sha": TARGET,
        "path": path,
        "event": event,
        "run_attempt": 1,
        "status": status,
        "conclusion": "success" if status == "completed" else None,
        "created_at": STAMP,
        "updated_at": STAMP,
        "referenced_workflows": [
            {"path": f"{policy.REPOSITORY}/{policy.ACCEPTANCE_PATH}@refs/heads/main", "sha": TARGET}
        ],
    }


class FakeGitHub(policy.GitHub):
    def __init__(self) -> None:
        super().__init__("fixture-token")
        checks = [
            {
                "id": index,
                "name": name,
                "head_sha": TARGET,
                "app": {"id": 15368},
                "status": "completed",
                "conclusion": "success",
                "completed_at": STAMP,
            }
            for index, name in enumerate(
                sorted(
                    policy.CI_FLOOR
                    | {"IPR Policy / Signature", "Validate conventional commit format"}
                ),
                1,
            )
        ]
        self.data: dict[str, Any] = {
            "": {"id": 123, "full_name": policy.REPOSITORY},
            "git/ref/heads/main": {"object": {"sha": TARGET, "type": "commit"}},
            "branches/main": {
                "name": "main",
                "commit": {"sha": TARGET},
                "protection": {
                    "required_status_checks": {
                        "contexts": [check["name"] for check in checks],
                        "checks": [{"context": check["name"], "app_id": 15368} for check in checks],
                    }
                },
            },
            "actions/workflows/204238826": {"state": "disabled_manually"},
            "actions/workflows/204238826/runs": [],
            "rules/branches/main": [
                {"type": "update", "ruleset_source_type": "Repository", "ruleset_id": 9},
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": check["name"], "integration_id": 15368} for check in checks
                        ]
                    },
                },
            ],
            "rulesets/9": {
                "id": 9,
                "updated_at": STAMP,
                "enforcement": "active",
                "target": "branch",
                "bypass_actors": [],
                "rules": [
                    {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}}
                ],
            },
            "actions/runs/900": workflow_run(
                policy.WORKFLOWS["publish"], "workflow_dispatch", status="in_progress"
            ),
            "actions/runs/800": workflow_run(policy.CI_PATH, "push"),
            "actions/runs/800/attempts/1/jobs": [
                {
                    **check,
                    "check_run_url": f"https://api.github.com/repos/{policy.REPOSITORY}/check-runs/{check['id']}",
                }
                for check in checks
            ]
            + [
                {
                    "name": "Workflow security",
                    "head_sha": TARGET,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": STAMP,
                    "check_run_url": "security-check",
                }
            ],
            f"commits/{TARGET}/check-runs?filter=latest": checks,
            "actions/runs/900/attempts/1/jobs": [
                {
                    "name": "acceptance / Build and accept distributions",
                    "head_sha": TARGET,
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
            "pulls/700": {
                "merged": True,
                "merge_commit_sha": TARGET,
                "base": {"ref": "main", "repo": {"full_name": policy.REPOSITORY}},
                "head": {
                    "ref": "release-please--branches--main",
                    "repo": {"full_name": policy.REPOSITORY},
                },
                "labels": [{"name": "autorelease:pending"}],
            },
        }
        for name in ("release-proposal", "release-publish"):
            self.data[f"environments/{name}"] = {
                "can_admins_bypass": False,
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "prevent_self_review": True,
                        "reviewers": [{"id": 1}],
                    }
                ],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            }
            self.data[f"environments/{name}/deployment-branch-policies"] = [
                {"name": "main", "type": "branch"}
            ]
        self.writes: list[tuple[str, str, Any]] = []
        self.reads: list[str] = []
        self.downloads: dict[int, bytes] = {}

    def repo(self, suffix: str, method: str = "GET", body: Any = None) -> Any:
        if method == "GET":
            self.reads.append(suffix)
            return copy.deepcopy(self.data[suffix])
        self.writes.append((suffix, method, copy.deepcopy(body)))
        if suffix == "git/refs":
            self.data["git/ref/" + body["ref"].removeprefix("refs/")] = {
                "object": {"type": "commit", "sha": body["sha"]}
            }
        elif suffix == "releases":
            self.data["releases/tags/" + body["tag_name"]] = {**body, "id": 200}
        elif suffix == "issues/700/labels":
            self.data["pulls/700"]["labels"].append({"name": "autorelease:tagged"})
        elif suffix == "issues/700/labels/autorelease%3Apending":
            self.data["pulls/700"]["labels"] = [{"name": "autorelease:tagged"}]
        return {}

    def pages(self, suffix: str, key: str | None = None) -> list[Any]:
        return self.repo(suffix)

    def optional(self, suffix: str) -> Any:
        return self.repo(suffix) if suffix in self.data else None

    def artifact_bytes(self, artifact_id: int) -> bytes:
        return self.downloads[artifact_id]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "datetime", Clock)
    monkeypatch.setattr(artifacts, "datetime", Clock)


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> policy.Context:
    environment = {
        "TARGET_SHA": TARGET,
        "OPERATION": "publish",
        "GITHUB_REPOSITORY": policy.REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": TARGET,
        "GITHUB_WORKFLOW_SHA": TARGET,
        "GITHUB_WORKFLOW_REF": f"{policy.REPOSITORY}/{policy.WORKFLOWS['publish']}@refs/heads/main",
        "GITHUB_RUN_ID": "900",
        "CI_RUN_ID": "800",
        "CI_RUN_ATTEMPT": "1",
        "RELEASE_PR": "700",
        "RECOVER_FROM": "",
        "PUBLICATION_ENABLED": "true",
        "MAIN_FREEZE_ATTESTATION": json.dumps(
            {
                "schema": 1,
                "repository": policy.REPOSITORY,
                "target_sha": TARGET,
                "ruleset_id": 9,
                "ruleset_updated_at": STAMP,
                "observed_at": STAMP,
                "bypass_actors": [],
            }
        ),
        "ARTIFACT_ID": "81",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return policy.Context.from_env()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TARGET_SHA", ""),
        ("TARGET_SHA", "main"),
        ("TARGET_SHA", TARGET[:12]),
        ("TARGET_SHA", OTHER),
        ("GITHUB_SHA", OTHER),
        ("GITHUB_WORKFLOW_SHA", OTHER),
        ("GITHUB_REF", "refs/tags/main"),
        ("GITHUB_REF", "refs/heads/release"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_EVENT_NAME", "workflow_run"),
        ("GITHUB_EVENT_NAME", "repository_dispatch"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_RUN_ATTEMPT", "50"),
        ("GITHUB_REPOSITORY", "fork/adcp-client-python"),
        ("GITHUB_WORKFLOW_REF", "legacy@main"),
        ("CI_RUN_ID", "latest"),
        ("CI_RUN_ATTEMPT", "0"),
        ("PUBLICATION_ENABLED", ""),
        ("PUBLICATION_ENABLED", "false"),
        ("RELEASE_PR", ""),
        ("OPERATION", "recover"),
    ],
)
def test_invocation_rejects_implicit_historical_or_unapproved_targets(
    context: policy.Context, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(REJECTED):
        policy.Context.from_env()


def test_valid_proposal_does_not_require_publication_switch(
    context: policy.Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPERATION", "proposal")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF", f"{policy.REPOSITORY}/{policy.WORKFLOWS['proposal']}@refs/heads/main"
    )
    monkeypatch.setenv("RELEASE_PR", "")
    monkeypatch.delenv("PUBLICATION_ENABLED")
    proposal = policy.Context.from_env()
    api = FakeGitHub()
    api.data["actions/runs/900"]["path"] = policy.WORKFLOWS["proposal"]
    policy.gate(api, proposal)
    assert api.writes == []
    monkeypatch.setenv("RECOVER_FROM", "899")
    with pytest.raises(REJECTED, match="proposal"):
        policy.Context.from_env()


def test_exact_main_and_complete_evidence_pass_read_only(context: policy.Context) -> None:
    api = FakeGitHub()
    assert policy.gate(api, context) == {"ci_run_id": 800, "ci_run_attempt": 1}
    assert api.writes == []
    assert api.reads.count("git/ref/heads/main") == 2


@pytest.mark.parametrize("suffix", ["", "@main", "@refs/heads/main", "@" + TARGET])
def test_documented_workflow_path_variants_are_bound_to_main(
    context: policy.Context, suffix: str
) -> None:
    api = FakeGitHub()
    api.data["actions/runs/800"]["path"] += suffix
    policy.gate(api, context)


@pytest.mark.parametrize("suffix", ["@refs/tags/main", "@release", "@" + OTHER])
def test_workflow_path_cannot_select_another_revision(context: policy.Context, suffix: str) -> None:
    api = FakeGitHub()
    api.data["actions/runs/800"]["path"] += suffix
    with pytest.raises(REJECTED, match="another revision"):
        policy.gate(api, context)


@pytest.mark.parametrize(
    ("age", "status"),
    [(0, "completed"), (29, "completed"), (30, "completed"), (31, "in_progress"), (40, "waiting")],
)
def test_disabled_legacy_workflow_is_not_proof_of_retirement(
    context: policy.Context, age: int, status: str
) -> None:
    api = FakeGitHub()
    api.data["actions/workflows/204238826/runs"] = [
        {"created_at": (NOW - timedelta(days=age)).isoformat(), "status": status}
    ]
    with pytest.raises(REJECTED, match="legacy release invocation"):
        policy.gate(api, context)
    assert api.writes == []


def test_completed_legacy_runs_outside_rerun_window_do_not_block(context: policy.Context) -> None:
    api = FakeGitHub()
    api.data["actions/workflows/204238826/runs"] = [
        {"created_at": (NOW - timedelta(days=31)).isoformat(), "status": "completed"}
    ]
    policy.gate(api, context)


def test_proposal_normalization_can_only_update_the_normal_pr_file(context: policy.Context) -> None:
    proposal = replace(context, operation="proposal", release_pr=None)
    reader, writer = FakeGitHub(), FakeGitHub()
    reader.data["actions/runs/900"]["path"] = policy.WORKFLOWS["proposal"]
    reader.data["pulls/700"]["merged"] = False
    reader.data["contents/pyproject.toml?ref=release-please--branches--main"] = {
        "sha": OTHER,
        "content": base64.b64encode(b'[project]\nversion = "1.2.3-rc.1"\n').decode(),
    }
    policy.normalize_proposal(reader, writer, proposal, [{"number": 700}])
    assert reader.writes == []
    assert len(writer.writes) == 1
    path, method, body = writer.writes[0]
    assert (path, method) == ("contents/pyproject.toml", "PUT")
    assert body["sha"] == OTHER and body["branch"] == "release-please--branches--main"
    assert base64.b64decode(body["content"]) == b'[project]\nversion = "1.2.3rc1"\n'


@pytest.mark.parametrize(
    "conclusion",
    [None, "failure", "neutral", "skipped", "cancelled", "timed_out", "action_required", "stale"],
)
def test_every_required_check_must_succeed(context: policy.Context, conclusion: str | None) -> None:
    api = FakeGitHub()
    api.data[f"commits/{TARGET}/check-runs?filter=latest"][0]["conclusion"] = conclusion
    with pytest.raises(REJECTED, match="required check"):
        policy.gate(api, context)


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_incomplete_checks_cannot_be_accepted(context: policy.Context, status: str) -> None:
    api = FakeGitHub()
    api.data[f"commits/{TARGET}/check-runs?filter=latest"][0]["status"] = status
    with pytest.raises(REJECTED):
        policy.gate(api, context)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "duplicate",
        "wrong_app",
        "wrong_sha",
        "old",
        "future",
        "pending",
        "unbound_app",
        "empty_rules",
        "protection_removed",
        "wrong_ci_check",
    ],
)
def test_bad_protected_check_inventory_is_not_success(context: policy.Context, case: str) -> None:
    api = FakeGitHub()
    checks = api.data[f"commits/{TARGET}/check-runs?filter=latest"]
    if case == "missing":
        checks.pop()
    elif case == "duplicate":
        checks.append(copy.deepcopy(checks[0]))
    elif case == "wrong_app":
        checks[0]["app"]["id"] = 999
    elif case == "wrong_sha":
        checks[0]["head_sha"] = OTHER
    elif case in {"old", "future"}:
        checks[0]["completed_at"] = (NOW + timedelta(days=-2 if case == "old" else 1)).isoformat()
    elif case == "pending":
        checks[0]["status"] = "in_progress"
    elif case == "unbound_app":
        api.data["rules/branches/main"][1]["parameters"]["required_status_checks"][0][
            "integration_id"
        ] = None
    elif case == "empty_rules":
        api.data["rules/branches/main"] = []
    elif case == "protection_removed":
        api.data["rules/branches/main"][1]["parameters"]["required_status_checks"] = []
    else:
        next(check for check in checks if check["name"] in policy.CI_FLOOR)["id"] = 999
    with pytest.raises(REJECTED):
        policy.gate(api, context)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("head_sha", OTHER),
        ("head_branch", "release"),
        ("path", "other.yml"),
        ("event", "pull_request"),
        ("run_attempt", 2),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("head_repository", {"full_name": "fork/sdk"}),
        ("updated_at", (NOW - timedelta(days=2)).isoformat()),
    ],
)
def test_ci_run_provenance_and_freshness(context: policy.Context, key: str, value: Any) -> None:
    api = FakeGitHub()
    api.data["actions/runs/800"][key] = value
    with pytest.raises(REJECTED):
        policy.gate(api, context)


@pytest.mark.parametrize("case", ["missing", "skipped", "wrong_sha", "old", "failed"])
def test_ci_summary_cannot_hide_an_incomplete_job(context: policy.Context, case: str) -> None:
    api = FakeGitHub()
    jobs = api.data["actions/runs/800/attempts/1/jobs"]
    if case == "missing":
        jobs.clear()
    elif case == "wrong_sha":
        jobs[0]["head_sha"] = OTHER
    elif case == "old":
        jobs[0]["completed_at"] = (NOW - timedelta(days=2)).isoformat()
    else:
        jobs[0]["conclusion"] = "skipped" if case == "skipped" else "failure"
    with pytest.raises(REJECTED):
        policy.gate(api, context)


def test_ci_rerun_requires_explicit_new_attempt_and_all_its_jobs(context: policy.Context) -> None:
    api = FakeGitHub()
    api.data["actions/runs/800"]["run_attempt"] = 2
    api.data["actions/runs/800/attempts/2/jobs"] = api.data.pop("actions/runs/800/attempts/1/jobs")
    with pytest.raises(REJECTED, match="attempt changed"):
        policy.gate(api, context)
    policy.gate(api, replace(context, ci_attempt=2))


def test_read_only_freeze_uses_a_current_operator_attestation(
    context: policy.Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    del api.data["rulesets/9"]["bypass_actors"]
    policy.gate(api, context)
    assert api.writes == []
    monkeypatch.delenv("MAIN_FREEZE_ATTESTATION")
    with pytest.raises(REJECTED, match="attestation is missing"):
        policy.gate(api, context)


def test_an_incomplete_freeze_attestation_is_not_permission(
    context: policy.Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation = json.loads(os.environ["MAIN_FREEZE_ATTESTATION"])
    del attestation["bypass_actors"]
    monkeypatch.setenv("MAIN_FREEZE_ATTESTATION", json.dumps(attestation))
    with pytest.raises(REJECTED, match="attestation is incomplete"):
        policy.gate(FakeGitHub(), context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", 2),
        ("repository", "another/repository"),
        ("target_sha", OTHER),
        ("ruleset_id", 10),
        ("ruleset_updated_at", (NOW - timedelta(hours=1)).isoformat()),
        ("ruleset_updated_at", NOW.isoformat()),
        ("observed_at", (NOW - timedelta(days=2)).isoformat()),
        ("observed_at", (NOW + timedelta(minutes=1)).isoformat()),
        ("bypass_actors", [{"actor_type": "OrganizationAdmin"}]),
        ("bypass_actors", None),
    ],
)
def test_freeze_attestation_cannot_authorize_another_revision_or_window(
    context: policy.Context, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    attestation = json.loads(os.environ["MAIN_FREEZE_ATTESTATION"])
    attestation[field] = value
    monkeypatch.setenv("MAIN_FREEZE_ATTESTATION", json.dumps(attestation))
    api = FakeGitHub()
    del api.data["rulesets/9"]["bypass_actors"]
    with pytest.raises(REJECTED):
        policy.gate(api, context)
    assert api.writes == []


def test_rules_after_the_first_page_also_require_success(
    context: policy.Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    pages = api.pages

    def later_page(suffix: str, key: str | None = None) -> list[Any]:
        items = pages(suffix, key)
        if suffix == "rules/branches/main":
            items.append(
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "Extra policy", "integration_id": 1}]
                    },
                }
            )
        return items

    monkeypatch.setattr(api, "pages", later_page)
    with pytest.raises(REJECTED, match="Extra policy"):
        policy.gate(api, context)


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "moved",
        "no_freeze",
        "bypass",
        "inactive_freeze",
        "wrong_freeze_rule",
        "freeze_revision_changed",
        "freeze_allows_upstream_updates",
        "no_review",
        "self_review",
        "admin_bypass",
        "tag_environment",
        "extra_branch",
        "re_enabled_legacy",
        "caller_rerun",
        "old_caller",
        "wrong_acceptance",
        "old_acceptance",
        "release_parent",
        "unmerged_release",
        "wrong_release_branch",
        "fork_release",
    ],
)
def test_gate_rejects_configuration_and_target_bypasses(
    context: policy.Context, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    if case == "stale":
        api.data["git/ref/heads/main"]["object"]["sha"] = OTHER
    elif case == "moved":
        read = api.repo
        count = 0

        def moving(suffix: str, method: str = "GET", body: Any = None) -> Any:
            nonlocal count
            if suffix == "git/ref/heads/main":
                count += 1
                if count == 2:
                    api.data[suffix]["object"]["sha"] = OTHER
            return read(suffix, method, body)

        monkeypatch.setattr(api, "repo", moving)
    elif case == "no_freeze":
        api.data["rules/branches/main"].pop(0)
    elif case == "bypass":
        api.data["rulesets/9"]["bypass_actors"] = [{"actor_type": "OrganizationAdmin"}]
    elif case == "inactive_freeze":
        api.data["rulesets/9"]["enforcement"] = "evaluate"
    elif case == "wrong_freeze_rule":
        api.data["rulesets/9"]["rules"] = [{"type": "non_fast_forward"}]
    elif case == "freeze_revision_changed":
        api.data["rulesets/9"]["updated_at"] = NOW.isoformat()
    elif case == "freeze_allows_upstream_updates":
        api.data["rulesets/9"]["rules"][0]["parameters"]["update_allows_fetch_and_merge"] = True
    elif case == "no_review":
        api.data["environments/release-publish"]["protection_rules"] = []
    elif case == "self_review":
        api.data["environments/release-publish"]["protection_rules"][0][
            "prevent_self_review"
        ] = False
    elif case == "admin_bypass":
        api.data["environments/release-publish"]["can_admins_bypass"] = True
    elif case == "tag_environment":
        api.data["environments/release-publish/deployment-branch-policies"][0]["type"] = "tag"
    elif case == "extra_branch":
        api.data["environments/release-publish/deployment-branch-policies"].append(
            {"name": "*", "type": "branch"}
        )
    elif case == "re_enabled_legacy":
        api.data["actions/workflows/204238826"]["state"] = "active"
    elif case == "caller_rerun":
        api.data["actions/runs/900"]["run_attempt"] = 2
    elif case == "old_caller":
        api.data["actions/runs/900"]["created_at"] = (NOW - timedelta(days=2)).isoformat()
    elif case == "wrong_acceptance":
        api.data["actions/runs/900"]["referenced_workflows"][0]["path"] = "unapproved.yml"
    elif case == "old_acceptance":
        api.data["actions/runs/900"]["referenced_workflows"][0]["sha"] = OTHER
    elif case == "release_parent":
        api.data["pulls/700"]["merge_commit_sha"] = OTHER
    elif case == "unmerged_release":
        api.data["pulls/700"]["merged"] = False
    elif case == "wrong_release_branch":
        api.data["pulls/700"]["head"]["ref"] = "feature"
    elif case == "fork_release":
        api.data["pulls/700"]["head"]["repo"]["full_name"] = "fork/sdk"
    with pytest.raises(REJECTED):
        policy.gate(api, context)
    assert api.writes == []


def write_zip(path: Path, contents: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in contents.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), value)


@pytest.fixture
def candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context: policy.Context
) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "source"
    source = {
        "src/adcp/__init__.py": b"value = 1\n",
        "src/adcp/py.typed": b"",
        "src/adcp/ADCP_VERSION": b"3.2.0-rc.3\n",
        "pyproject.toml": f'[project]\nname = "adcp"\nversion = "{VERSION}"\n'.encode(),
        ".release-please-manifest.json": b'{".": "1.2.3-rc.1"}',
        "setup.py": b"# fixture\n",
        "MANIFEST.in": b"# fixture\n",
        "tests/conformance/reporting/test_contract.py": b"def test_contract(): pass\n",
    }
    for version in ("2.5", "3.0", "3.1", "3.2.0-rc.3"):
        source[f"schemas/cache/{version}/schema.json"] = b"{}\n"
    for name, value in source.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    monkeypatch.setattr(artifacts, "ROOT", root)

    def fake_git(*args: str) -> str:
        if args == ("rev-parse", "HEAD^{tree}"):
            return TREE
        if args == ("rev-parse", "HEAD"):
            return TARGET
        if args[:2] == ("ls-files", "-z"):
            return "\0".join(sorted(path for path in source if path.startswith(args[2] + "/")))
        if args[0] == "diff":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(artifacts, "git", fake_git)
    directory = tmp_path / "candidate"
    directory.mkdir()
    package = artifacts.expected_package()
    wheel_files = {
        name: (
            (root / name.replace("adcp/_schemas/", "schemas/cache/", 1)).read_bytes()
            if name.startswith("adcp/_schemas/")
            else (root / "src" / name).read_bytes()
        )
        for name in package
    }
    wheel_files[f"adcp-{VERSION}.dist-info/METADATA"] = f"Name: adcp\nVersion: {VERSION}\n".encode()
    write_zip(directory / f"adcp-{VERSION}-py3-none-any.whl", wheel_files)
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
        sdist_files = {f"adcp-{VERSION}/{name}": value for name, value in source.items()}
        sdist_files[f"adcp-{VERSION}/PKG-INFO"] = f"Name: adcp\nVersion: {VERSION}\n".encode()
        for name, value in sdist_files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    (directory / f"adcp-{VERSION}.tar.gz").write_bytes(gzip.compress(tar_bytes.getvalue(), mtime=0))
    files = artifacts.snapshot(directory, VERSION)
    installed = {
        name: {
            "version": VERSION,
            "python": "3.10.21",
            "artifact_sha256": file["sha256"],
            "package_inventory_sha256": artifacts.digest(artifacts.canonical(package)),
            "test_inventory_sha256": artifacts.digest(
                artifacts.canonical(artifacts.test_inventory())
            ),
            "junit_sha256": "d" * 64,
            "result": {"tests": 49, "errors": 0, "failures": 0, "skipped": 0},
        }
        for name, file in files.items()
    }
    manifest = {
        "schema": 1,
        "repository": policy.REPOSITORY,
        "target_sha": TARGET,
        "source_tree": TREE,
        "run_id": 900,
        "run_attempt": 1,
        "operation": "publish",
        "release_pr": 700,
        "recover_from": None,
        "accepted_at": STAMP,
        "version": VERSION,
        "tag": "v1.2.3-rc.1",
        "files": files,
        "installed": installed,
        "ci_run_id": 800,
        "ci_run_attempt": 1,
    }
    (directory / artifacts.MANIFEST).write_bytes(artifacts.canonical(manifest))
    return directory, manifest


def attach_artifact(
    api: FakeGitHub, candidate: tuple[Path, dict[str, Any]], run_id: int = 900
) -> str:
    directory, manifest = candidate
    evidence = {**manifest, "run_id": run_id}
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for path in directory.iterdir():
            value = (
                artifacts.canonical(evidence)
                if path.name == artifacts.MANIFEST
                else path.read_bytes()
            )
            archive.writestr(zipfile.ZipInfo(path.name, date_time=(2020, 1, 1, 0, 0, 0)), value)
    sha = artifacts.digest(content.getvalue())
    api.downloads[81] = content.getvalue()
    metadata = {
        "id": 81,
        "name": f"release-acceptance-{run_id}-1",
        "expired": False,
        "digest": "sha256:" + sha,
        "workflow_run": {
            "id": run_id,
            "head_sha": TARGET,
            "head_branch": "main",
            "repository_id": 123,
            "head_repository_id": 123,
        },
    }
    api.data["actions/artifacts/81"] = metadata
    api.data[f"actions/runs/{run_id}/artifacts"] = [metadata]
    return sha


def test_exact_artifact_acceptance(
    context: policy.Context,
    candidate: tuple[Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeGitHub()
    monkeypatch.setenv("ARTIFACT_DIGEST", attach_artifact(api, candidate))
    directory, manifest = candidate
    artifacts.validate_manifest(manifest, directory, context)
    assert artifacts.verified_candidate(api, context, tmp_path / "downloaded") == manifest
    assert api.writes == []


@pytest.mark.parametrize("suffix", [".whl", ".tar.gz"])
@pytest.mark.parametrize("recovery", [False, True])
def test_same_version_rebuild_cannot_replace_accepted_distribution_bytes(
    context: policy.Context,
    candidate: tuple[Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    recovery: bool,
) -> None:
    directory, manifest = candidate
    name = next(name for name in manifest["files"] if name.endswith(suffix))
    path = directory / name
    # Another build can have the same filename, version, metadata and source
    # inventory. It still needs its own acceptance; a version-only lookup cannot
    # substitute its bytes, including when recovering an interrupted release.
    if suffix == ".whl":
        with zipfile.ZipFile(path, "a") as archive:
            archive.comment = b"independent same-version build"
    else:
        path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes()), mtime=1))
    assert artifacts.inventory(path, VERSION) == manifest["files"][name]["inventory"]
    assert artifacts.file_digest(path) != manifest["files"][name]["sha256"]
    api = FakeGitHub()
    run_id = 899 if recovery else context.run_id
    monkeypatch.setenv("ARTIFACT_DIGEST", attach_artifact(api, candidate, run_id=run_id))
    with pytest.raises(REJECTED, match="artifact hash or inventory mismatch"):
        if recovery:
            api.data["actions/runs/899"] = {
                **workflow_run(policy.WORKFLOWS["publish"], "workflow_dispatch"),
                "conclusion": "failure",
            }
            artifacts.recover_candidate(
                api, replace(context, recover_from=899), tmp_path / "recovery"
            )
        else:
            artifacts.verified_candidate(api, context, tmp_path / "downloaded")
    assert api.writes == []


@pytest.mark.parametrize(
    "case",
    [
        "missing_wheel",
        "extra_file",
        "wheel_hash",
        "sdist_hash",
        "inventory",
        "version",
        "tree",
        "wrong_sha",
        "old_run",
        "rerun",
        "proposal",
        "ci_attempt",
        "expired",
        "no_install",
        "installed_hash",
        "installed_version",
        "installed_inventory",
        "installed_suite",
        "installed_skip",
        "installed_empty",
        "missing_report",
        "wrong_python",
        "parent_acceptance",
        "recover_identity",
    ],
)
def test_artifact_and_installed_evidence_fail_closed(
    context: policy.Context, candidate: tuple[Path, dict[str, Any]], case: str
) -> None:
    directory, original = candidate
    manifest = copy.deepcopy(original)
    wheel = next(name for name in manifest["files"] if name.endswith(".whl"))
    sdist = next(name for name in manifest["files"] if name.endswith(".tar.gz"))
    if case == "missing_wheel":
        (directory / wheel).unlink()
    elif case == "extra_file":
        (directory / "extra.whl").write_bytes(b"not accepted")
    elif case in {"wheel_hash", "sdist_hash"}:
        manifest["files"][wheel if case == "wheel_hash" else sdist]["sha256"] = "e" * 64
    elif case == "inventory":
        manifest["files"][wheel]["inventory"].pop("adcp/__init__.py")
    elif case == "version":
        manifest["version"] = "1.2.4"
    elif case == "tree":
        manifest["source_tree"] = OTHER
    elif case == "wrong_sha":
        manifest["target_sha"] = OTHER
    elif case == "old_run":
        manifest["run_id"] = 899
    elif case == "rerun":
        manifest["run_attempt"] = 2
    elif case == "proposal":
        manifest["operation"] = "proposal"
    elif case == "ci_attempt":
        manifest["ci_run_attempt"] = 2
    elif case == "expired":
        manifest["accepted_at"] = (NOW - timedelta(days=2)).isoformat()
    elif case == "no_install":
        manifest["installed"].pop(sdist)
    elif case == "installed_hash":
        manifest["installed"][sdist]["artifact_sha256"] = "e" * 64
    elif case == "installed_version":
        manifest["installed"][wheel]["version"] = "1.2.4"
    elif case == "installed_inventory":
        manifest["installed"][wheel]["package_inventory_sha256"] = "e" * 64
    elif case == "installed_suite":
        manifest["installed"][wheel]["test_inventory_sha256"] = "e" * 64
    elif case == "installed_skip":
        manifest["installed"][wheel]["result"]["skipped"] = 1
    elif case == "installed_empty":
        manifest["installed"][wheel]["result"]["tests"] = 0
    elif case == "missing_report":
        manifest["installed"][wheel]["junit_sha256"] = ""
    elif case == "wrong_python":
        manifest["installed"][wheel]["python"] = "3.12.1"
    elif case == "parent_acceptance":
        manifest["release_pr"] = 699
    elif case == "recover_identity":
        manifest["recover_from"] = 898
    with pytest.raises(REJECTED):
        artifacts.validate_manifest(manifest, directory, context)


@pytest.mark.parametrize(
    "case",
    [
        "expired",
        "missing_digest",
        "wrong_digest",
        "wrong_id",
        "wrong_run",
        "wrong_sha",
        "fork",
        "wrong_branch",
        "wrong_name",
        "tampered_archive",
        "needs_digest",
    ],
)
def test_artifact_download_provenance(
    context: policy.Context, candidate: tuple[Path, dict[str, Any]], tmp_path: Path, case: str
) -> None:
    api = FakeGitHub()
    expected = attach_artifact(api, candidate)
    metadata = api.data["actions/artifacts/81"]
    if case == "expired":
        metadata["expired"] = True
    elif case == "missing_digest":
        metadata["digest"] = ""
    elif case == "wrong_digest":
        metadata["digest"] = "sha256:" + "e" * 64
    elif case == "wrong_id":
        metadata["id"] = 82
    elif case == "wrong_run":
        metadata["workflow_run"]["id"] = 899
    elif case == "wrong_sha":
        metadata["workflow_run"]["head_sha"] = OTHER
    elif case == "fork":
        metadata["workflow_run"]["head_repository_id"] = 999
    elif case == "wrong_branch":
        metadata["workflow_run"]["head_branch"] = "release"
    elif case == "wrong_name":
        metadata["name"] = "release-acceptance-900-2"
    elif case == "tampered_archive":
        api.downloads[81] += b"tampered"
    elif case == "needs_digest":
        expected = "e" * 64
    with pytest.raises(REJECTED):
        artifacts.download(api, context, 81, tmp_path / "downloaded", expected)


@pytest.mark.parametrize("conclusion", ["skipped", "failure", None])
def test_writer_cannot_reuse_needs_output_from_incomplete_acceptance(
    context: policy.Context,
    candidate: tuple[Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conclusion: str | None,
) -> None:
    api = FakeGitHub()
    monkeypatch.setenv("ARTIFACT_DIGEST", attach_artifact(api, candidate))
    api.data["actions/runs/900/attempts/1/jobs"][0]["conclusion"] = conclusion
    with pytest.raises(REJECTED, match="acceptance job"):
        artifacts.verified_candidate(api, context, tmp_path / "downloaded")


def test_recovery_reuses_only_bytes_and_runs_new_installed_acceptance(
    context: policy.Context,
    candidate: tuple[Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeGitHub()
    attach_artifact(api, candidate, run_id=899)
    api.data["actions/runs/899"] = {
        **workflow_run(policy.WORKFLOWS["publish"], "workflow_dispatch"),
        "conclusion": "failure",
    }
    installs: list[str] = []

    def accept(path: Path, version: str) -> dict[str, Any]:
        installs.append(path.name)
        return candidate[1]["installed"][path.name]

    monkeypatch.setattr(artifacts, "installed_acceptance", accept)
    recovered = artifacts.build_candidate(
        api, replace(context, recover_from=899), tmp_path / "recovered"
    )
    assert set(installs) == artifacts.distribution_names(VERSION)
    assert recovered["run_id"] == 900
    assert recovered["recover_from"] == 899
    assert recovered["files"] == candidate[1]["files"]
    assert recovered["ci_run_id"] == 800
    assert recovered["accepted_at"] == NOW.isoformat()
    assert api.writes == []


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "still_running",
        "legacy",
        "wrong_sha",
        "rerun",
        "missing_candidate",
        "duplicate_candidate",
        "expired_candidate",
    ],
)
def test_guarded_recovery_cannot_adopt_arbitrary_or_successful_runs(
    context: policy.Context, candidate: tuple[Path, dict[str, Any]], tmp_path: Path, case: str
) -> None:
    api = FakeGitHub()
    attach_artifact(api, candidate, run_id=899)
    old = {
        **workflow_run(policy.WORKFLOWS["publish"], "workflow_dispatch"),
        "conclusion": "failure",
    }
    api.data["actions/runs/899"] = old
    if case == "success":
        old["conclusion"] = "success"
    elif case == "still_running":
        old["status"] = "in_progress"
    elif case == "legacy":
        old["path"] = ".github/workflows/release-please.yml"
    elif case == "wrong_sha":
        old["head_sha"] = OTHER
    elif case == "rerun":
        old["run_attempt"] = 2
    elif case == "missing_candidate":
        api.data["actions/runs/899/artifacts"] = []
    elif case == "duplicate_candidate":
        api.data["actions/runs/899/artifacts"] *= 2
    else:
        api.data["actions/artifacts/81"]["expired"] = True
    with pytest.raises(REJECTED):
        artifacts.recover_candidate(api, replace(context, recover_from=899), tmp_path / "recovery")


def published(manifest: dict[str, Any], names: list[str] | None = None) -> dict[str, Any]:
    return {
        "info": {"name": "adcp", "version": manifest["version"]},
        "urls": [
            {
                "filename": name,
                "yanked": False,
                "digests": {"sha256": manifest["files"][name]["sha256"]},
            }
            for name in (names if names is not None else manifest["files"])
        ],
    }


def test_recovery_uploads_only_missing_exact_files(
    context: policy.Context,
    candidate: tuple[Path, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, manifest = candidate
    api = FakeGitHub()
    wheel = next(name for name in manifest["files"] if name.endswith(".whl"))
    monkeypatch.setattr(publisher, "pypi_version", lambda _: published(manifest, [wheel]))
    with pytest.raises(REJECTED, match="replay"):
        publisher.prepare_pypi(api, context, manifest, directory, tmp_path / "normal")
    manifest["recover_from"] = 899
    missing = publisher.prepare_pypi(
        api, replace(context, recover_from=899), manifest, directory, tmp_path / "upload"
    )
    assert missing == [f"adcp-{VERSION}.tar.gz"]
    assert (tmp_path / "upload" / missing[0]).read_bytes() == (directory / missing[0]).read_bytes()
    assert api.writes == []


@pytest.mark.parametrize("case", ["wrong_hash", "extra", "duplicate", "yanked", "wrong_version"])
def test_existing_pypi_files_never_qualify_by_filename_alone(
    candidate: tuple[Path, dict[str, Any]], case: str
) -> None:
    manifest = candidate[1]
    remote = published(manifest)
    if case == "wrong_hash":
        remote["urls"][0]["digests"]["sha256"] = "e" * 64
    elif case == "extra":
        remote["urls"][0]["filename"] = "unexpected.whl"
    elif case == "duplicate":
        remote["urls"].append(remote["urls"][0])
    elif case == "yanked":
        remote["urls"][0]["yanked"] = True
    else:
        remote["info"]["version"] = "other"
    with pytest.raises(REJECTED):
        publisher.missing_files(manifest, remote)


def test_exact_release_publishes_once_and_complete_recovery_is_a_noop(
    context: policy.Context, candidate: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, manifest = candidate
    api = FakeGitHub()
    monkeypatch.setenv("ARTIFACT_DIGEST", "f" * 64)
    monkeypatch.setattr(publisher, "pypi_version", lambda _: published(manifest))
    publisher.publish_github(api, context, manifest, directory)
    assert api.writes[0] == ("git/refs", "POST", {"ref": "refs/tags/v1.2.3-rc.1", "sha": TARGET})
    releases = [body for path, method, body in api.writes if path == "releases"]
    assert len(releases) == 1
    assert releases[0]["target_commitish"] == TARGET
    assert (
        json.loads(releases[0]["body"])["acceptance"]["files"]
        == publisher.receipt(manifest)["files"]
    )
    before = copy.deepcopy(api.writes)
    with pytest.raises(REJECTED, match="replay"):
        publisher.publish_github(api, context, manifest, directory)
    manifest["recover_from"] = 899
    publisher.publish_github(api, replace(context, recover_from=899), manifest, directory)
    assert api.writes == before


def test_existing_release_target_commitish_is_not_a_substitute_for_the_tag(
    candidate: tuple[Path, dict[str, Any]],
) -> None:
    manifest = candidate[1]
    api = FakeGitHub()
    api.data["git/ref/tags/" + manifest["tag"]] = {"object": {"type": "commit", "sha": TARGET}}
    api.data["releases/tags/" + manifest["tag"]] = {
        "tag_name": manifest["tag"],
        "target_commitish": "main",
        "draft": False,
        "prerelease": True,
        "body": json.dumps({"acceptance": publisher.receipt(manifest)}),
    }
    publisher.release_state(api, manifest)
    api.data["git/ref/tags/" + manifest["tag"]]["object"]["sha"] = OTHER
    with pytest.raises(REJECTED, match="different target"):
        publisher.release_state(api, manifest)


@pytest.mark.parametrize(
    "case", ["wrong_tag", "missing_pypi", "stale_main", "proposal", "incomplete_ci"]
)
def test_publication_rechecks_before_any_mutation(
    context: policy.Context,
    candidate: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    directory, manifest = candidate
    api = FakeGitHub()
    monkeypatch.setattr(publisher, "pypi_version", lambda _: published(manifest))
    if case == "wrong_tag":
        api.data["git/ref/tags/" + manifest["tag"]] = {"object": {"type": "commit", "sha": OTHER}}
    elif case == "missing_pypi":
        monkeypatch.setattr(publisher, "pypi_version", lambda _: None)
    elif case == "stale_main":
        api.data["git/ref/heads/main"]["object"]["sha"] = OTHER
    elif case == "proposal":
        context = replace(context, operation="proposal", release_pr=None)
    else:
        api.data["actions/runs/800"]["status"] = "in_progress"
    with pytest.raises(REJECTED):
        publisher.publish_github(api, context, manifest, directory)
    assert api.writes == []


@pytest.mark.parametrize("name", ["../escape", "/escape", "a/../b", "a//b", "a\\b"])
def test_archive_paths_cannot_escape(name: str) -> None:
    with pytest.raises(REJECTED):
        artifacts.safe_name(name)


def test_duplicate_wheel_members_are_rejected(candidate: tuple[Path, dict[str, Any]]) -> None:
    path = next(candidate[0].glob("*.whl"))
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(path, "a") as archive:
        archive.writestr("adcp/__init__.py", b"replacement")
    with pytest.raises(REJECTED, match="duplicate"):
        artifacts.inventory(path, VERSION)


def test_wheel_cannot_install_an_untracked_startup_hook(
    candidate: tuple[Path, dict[str, Any]],
) -> None:
    path = next(candidate[0].glob("*.whl"))
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("injected.pth", b"import unexpected\n")
    with pytest.raises(REJECTED, match="outside the SDK"):
        artifacts.inventory(path, VERSION)


@pytest.mark.parametrize("case", ["no_version", "wrong_version", "semver"])
def test_project_and_manifest_version_must_match(
    candidate: tuple[Path, dict[str, Any]], case: str
) -> None:
    if case == "no_version":
        text = '[project]\nname="adcp"\n[unrelated]\nversion="1.2.3rc1"\n'
    elif case == "wrong_version":
        text = '[project]\nversion="1.2.4"\n'
    else:
        text = '[project]\nversion="1.2.3-rc.1"\n'
    (artifacts.ROOT / "pyproject.toml").write_text(text)
    with pytest.raises(REJECTED):
        artifacts.versions()


@pytest.mark.parametrize(("count", "skipped", "failures"), [(0, 0, 0), (1, 1, 0), (1, 0, 1)])
def test_installed_junit_cannot_report_empty_or_incomplete_success(
    tmp_path: Path, count: int, skipped: int, failures: int
) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        f'<testsuites><testsuite tests="{count}" failures="{failures}" '
        f'errors="0" skipped="{skipped}"/></testsuites>'
    )
    with pytest.raises(REJECTED):
        artifacts.junit_result(report)


def test_api_errors_and_pagination_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    api = policy.GitHub("fixture-token")
    batches = [{"total_count": 101, "jobs": list(range(100))}, {"total_count": 101, "jobs": [100]}]
    monkeypatch.setattr(api, "repo", lambda _: batches.pop(0))
    assert api.pages("runs/1/jobs", "jobs") == list(range(101))
    monkeypatch.setattr(api, "repo", lambda _: {"total_count": 1, "jobs": []})
    with pytest.raises(REJECTED, match="incomplete API"):
        api.pages("runs/1/jobs", "jobs")

    def forbidden(_: str) -> Any:
        raise urllib.error.HTTPError("https://api.github.com", 403, "forbidden", {}, None)

    monkeypatch.setattr(api, "repo", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        api.optional("environments/release-publish")


@pytest.mark.parametrize("name", ["IPR Policy / Signature", "Validate conventional commit format"])
def test_policy_check_must_belong_to_selected_main_ci_attempt(
    context: policy.Context, name: str
) -> None:
    api = FakeGitHub()
    job = next(job for job in api.data["actions/runs/800/attempts/1/jobs"] if job["name"] == name)
    job["check_run_url"] = "https://api.github.com/repos/unrelated/run/check-runs/1"
    with pytest.raises(REJECTED, match="selected CI attempt"):
        policy.gate(api, context)


@pytest.mark.parametrize(
    ("name", "app_id", "other_name"),
    [
        ("check / check", 15368, "check"),
        ("CodeQL", 57789, "Analyze (python)"),
        ("GitGuardian Security Checks", 46505, "Security scan"),
    ],
)
@pytest.mark.parametrize("case", ["valid", "missing", "wrong_name", "wrong_app", "failed", "stale"])
def test_branch_summary_checks_are_required_in_addition_to_rulesets(
    context: policy.Context, name: str, app_id: int, other_name: str, case: str
) -> None:
    api = FakeGitHub()
    summary = api.data["branches/main"]["protection"]["required_status_checks"]
    summary["contexts"].append(name)
    summary["checks"].append({"context": name, "app_id": app_id})
    check = {
        "id": 700,
        "name": other_name if case == "wrong_name" else name,
        "head_sha": TARGET,
        "app": {"id": (15368 if app_id != 15368 else 1) if case == "wrong_app" else app_id},
        "status": "completed",
        "conclusion": "failure" if case == "failed" else "success",
        "completed_at": (NOW - timedelta(days=2)).isoformat() if case == "stale" else STAMP,
    }
    if case != "missing":
        api.data[f"commits/{TARGET}/check-runs?filter=latest"].append(check)
    if case == "valid":
        policy.gate(api, context)
    else:
        with pytest.raises(REJECTED):
            policy.gate(api, context)
    assert api.writes == []


@pytest.mark.parametrize(
    "case",
    [
        "missing_contexts",
        "missing_checks",
        "unbound_context",
        "missing_app",
        "wrong_target",
        "wrong_branch",
    ],
)
def test_incomplete_branch_summary_cannot_be_treated_as_no_classic_protection(
    context: policy.Context, case: str
) -> None:
    api = FakeGitHub()
    branch = api.data["branches/main"]
    summary = branch["protection"]["required_status_checks"]
    if case == "missing_contexts":
        summary.pop("contexts")
    elif case == "missing_checks":
        summary.pop("checks")
    elif case == "unbound_context":
        summary["contexts"].append("CodeQL")
    elif case == "missing_app":
        summary["checks"][0].pop("app_id")
    elif case == "wrong_target":
        branch["commit"]["sha"] = OTHER
    else:
        branch["name"] = "another-branch"
    with pytest.raises(REJECTED):
        policy.gate(api, context)
    assert api.writes == []


def test_branch_summary_permission_denial_is_not_an_empty_protection_inventory(
    context: policy.Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeGitHub()
    read = api.repo

    def denied(suffix: str, method: str = "GET", body: Any = None) -> Any:
        if suffix == "branches/main":
            raise urllib.error.HTTPError("https://api.github.com", 403, "forbidden", {}, None)
        return read(suffix, method, body)

    monkeypatch.setattr(api, "repo", denied)
    with pytest.raises(urllib.error.HTTPError):
        policy.gate(api, context)
    assert api.writes == []


def workflow(name: str) -> dict[str, Any]:
    # BaseLoader preserves the YAML 1.2 Actions key `on`, unlike PyYAML's
    # YAML 1.1 boolean resolver. String booleans are intentional below.
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_retired_workflow_is_a_failing_tombstone() -> None:
    retired = workflow("release-please.yml")
    assert set(retired["on"]) == {"workflow_dispatch"}
    assert retired["permissions"] == {}
    assert list(retired["jobs"]) == ["retired"]
    steps = retired["jobs"]["retired"]["steps"]
    assert len(steps) == 1 and "exit 1" in steps[0]["run"]
    assert "uses" not in steps[0]


def test_proposal_has_no_publication_code_path_or_pypi_identity() -> None:
    proposal = workflow("release-proposal.yml")
    assert set(proposal["on"]) == {"workflow_dispatch"}
    assert set(proposal["on"]["workflow_dispatch"]["inputs"]) == {
        "target_sha",
        "ci_run_id",
        "ci_run_attempt",
    }
    assert proposal["permissions"] == {}
    job = proposal["jobs"]["propose"]
    assert job["needs"] == "acceptance"
    assert job["environment"] == "release-proposal"
    assert job["env"]["MAIN_FREEZE_ATTESTATION"] == "${{ vars.RELEASE_MAIN_FREEZE }}"
    assert all(value == "read" for value in job["permissions"].values())
    release = next(step for step in job["steps"] if step.get("id") == "release")
    assert release["with"]["skip-github-release"] == "true"
    assert release["with"]["skip-github-pull-request"] == "false"
    assert release["with"]["target-branch"] == "main"
    steps = job["steps"]
    verify_index = next(
        i
        for i, step in enumerate(steps)
        if "scripts.release_artifacts verify" in step.get("run", "")
    )
    token_index = next(i for i, step in enumerate(steps) if step.get("id") == "app-token")
    assert verify_index < token_index < steps.index(release)
    text = json.dumps(proposal)
    for forbidden in (
        "release_publish",
        "pypi-publish",
        "id-token",
        "PYPY_API_TOKEN",
        "IPR_APP_PRIVATE_KEY",
        "publish=true",
    ):
        assert forbidden not in text


def test_all_writers_depend_on_current_invocation_acceptance_and_approval() -> None:
    publish = workflow("release-publish.yml")
    proposal = workflow("release-proposal.yml")
    assert publish["env"]["MAIN_FREEZE_ATTESTATION"] == "${{ vars.RELEASE_MAIN_FREEZE }}"
    for document in (publish, proposal):
        assert set(document["on"]) == {"workflow_dispatch"}
        assert document["permissions"] == {}
        assert document["concurrency"] == {
            "group": "adcp-release-main",
            "cancel-in-progress": "false",
        }
        acceptance = document["jobs"]["acceptance"]
        assert acceptance["uses"] == "./.github/workflows/release-acceptance.yml"
        assert "secrets" not in acceptance
        for name, job in document["jobs"].items():
            if name == "acceptance":
                continue
            assert "acceptance" in job["needs"]
            assert "github.run_attempt == 1" in job["if"]
            assert "github.ref == 'refs/heads/main'" in job["if"]
            assert "github.sha == inputs.target_sha" in job["if"]
            assert "github.workflow_sha == inputs.target_sha" in job["if"]
            assert job["environment"] in {"release-proposal", "release-publish"}
            assert job["env"]["ARTIFACT_ID"] == "${{ needs.acceptance.outputs.artifact_id }}"
            assert (
                job["env"]["ARTIFACT_DIGEST"] == "${{ needs.acceptance.outputs.artifact_digest }}"
            )
    assert publish["jobs"]["pypi"]["permissions"]["contents"] == "read"
    assert publish["jobs"]["pypi"]["permissions"]["id-token"] == "write"
    assert "id-token" not in publish["jobs"]["github-release"]["permissions"]
    upload = next(
        step
        for step in publish["jobs"]["pypi"]["steps"]
        if "pypa/gh-action" in step.get("uses", "")
    )
    assert upload["with"]["skip-existing"] == "false"
    assert upload["with"]["packages-dir"] == "publish-dist/"
    assert "password" not in upload["with"]


def test_acceptance_is_read_only_uncached_and_cannot_be_called_with_old_artifacts() -> None:
    acceptance = workflow("release-acceptance.yml")
    assert acceptance["env"]["MAIN_FREEZE_ATTESTATION"] == "${{ vars.RELEASE_MAIN_FREEZE }}"
    assert set(acceptance["on"]) == {"workflow_call"}
    assert all(value == "read" for value in acceptance["permissions"].values())
    assert "secrets" not in acceptance["on"]["workflow_call"]
    assert "artifact_id" not in acceptance["on"]["workflow_call"]["inputs"]
    assert acceptance["jobs"]["accept"]["needs"] == "preflight"
    upload = next(
        step for step in acceptance["jobs"]["accept"]["steps"] if step.get("id") == "upload"
    )
    assert upload["with"]["overwrite"] == "false"
    assert upload["with"]["archive"] == "true"
    assert upload["with"]["name"] == "release-acceptance-${{ github.run_id }}-1"
    assert upload["with"]["if-no-files-found"] == "error"


def test_release_actions_are_pinned_and_all_workflows_are_security_scanned() -> None:
    import re

    paths = [
        "release-please.yml",
        "release-acceptance.yml",
        "release-proposal.yml",
        "release-publish.yml",
    ]
    for path in paths:
        document = workflow(path)
        for job in document["jobs"].values():
            for step in job.get("steps", []):
                if "uses" not in step:
                    continue
                assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", step["uses"])
                assert "actions/cache" not in step["uses"]
                if step["uses"].startswith("actions/checkout@"):
                    assert step["with"]["ref"] == "${{ github.sha }}"
                    assert step["with"]["persist-credentials"] == "false"
    security = workflow("ci.yml")["jobs"]["workflow-security"]
    audit = next(step for step in security["steps"] if step.get("name") == "Audit workflows")
    assert all(f".github/workflows/{path}" in audit["with"]["inputs"] for path in paths)
