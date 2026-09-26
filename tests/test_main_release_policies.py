"""Policy provenance and actual integrated-history regressions, without writes."""

from __future__ import annotations

import base64
import copy
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import check_main_policies as policies
from scripts.release_gate import ReleaseRejectedError

LEDGER_SHA = "d" * 40
BREAKING_SUBJECT = "fix(reporting)!: scope configuration generations by account (#1174)"
BREAKING_FOOTER = (
    "BREAKING CHANGE: generation_key returns ReportingConfigurationGenerationKey "
    "instead of a two-tuple. Use its named fields."
)


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(policies, "ROOT", tmp_path)
    policies.git("config", "user.name", "Policy Fixture")
    policies.git("config", "user.email", "fixture@example.invalid")
    commits = []
    for message in (
        "chore: published baseline",
        "fix: first contribution",
        BREAKING_SUBJECT + "\n\n" + BREAKING_FOOTER,
    ):
        policies.git("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", message)
        commits.append(policies.git("rev-parse", "HEAD"))
    monkeypatch.setattr(policies, "PUBLISHED_BASE", commits[0])
    return commits


class PolicyAPI(policies.GitHub):
    def __init__(self, commits: list[str]) -> None:
        super().__init__("fixture-token")
        self.reads: list[str] = []
        self.records = [
            {
                "id": 7,
                "name": "signed-user",
                "method": "pr_comment",
                "created_at": "2026-04-01T00:00:00Z",
            }
        ]
        self.data: dict[str, Any] = {}
        for number, sha in enumerate(commits[1:], 1173):
            pr = {
                "number": number,
                "base": {"ref": "main", "repo": {"full_name": policies.REPOSITORY}},
                "head": {"sha": "e" * 40},
                "merge_commit_sha": sha,
                "merged_at": "2026-09-22T00:00:00Z",
                "merged": True,
                "state": "closed",
                "user": {"id": 7, "type": "User", "login": "signed-user"},
            }
            self.data[f"commits/{sha}/pulls?per_page=100&page=1"] = [pr]
            self.data[f"pulls/{number}"] = pr

    def request(self, path: str, method: str = "GET", body: Any = None) -> Any:
        assert method == "GET" and body is None, "policy must not write any evidence"
        self.reads.append(path)
        prefix = f"/repos/{policies.LEDGER_REPOSITORY}"
        if path == prefix + "/git/ref/heads/main":
            return {"object": {"type": "commit", "sha": LEDGER_SHA}}
        assert path == prefix + f"/contents/signatures/ipr-signatures.json?ref={LEDGER_SHA}"
        return {
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps({"signedContributors": self.records}).encode()
            ).decode(),
        }

    def repo(self, suffix: str, method: str = "GET", body: Any = None) -> Any:
        assert method == "GET" and body is None, "policy must not write any evidence"
        self.reads.append(suffix)
        return copy.deepcopy(self.data[suffix])


def test_every_post_release_integration_has_real_agreement(history: list[str]) -> None:
    api = PolicyAPI(history)
    evidence = policies.ipr(api, "push", history[-1], {})
    assert evidence["target_sha"] == history[-1]
    assert evidence["ledger_sha"] == LEDGER_SHA
    assert [entry["sha"] for entry in evidence["contributions"]] == history[1:]
    assert [entry["author_id"] for entry in evidence["contributions"]] == [7, 7]
    assert not any("check-runs" in path or "status" in path for path in api.reads)
    assert policies.conventional("push", history[-1], {})["commits"] == history[1:]


@pytest.mark.parametrize(
    "case",
    [
        "unsigned_earlier",
        "renamed_login_wrong_id",
        "unknown_author",
        "fake_bot_login",
        "late_signature",
        "duplicate_signature",
        "empty_ledger",
        "wrong_base",
        "wrong_repository",
        "wrong_merge",
        "not_merged",
        "direct_push",
        "ambiguous_pr",
    ],
)
def test_main_agreement_cannot_be_substituted(history: list[str], case: str) -> None:
    api = PolicyAPI(history)
    earlier = api.data["pulls/1173"]
    if case in {"unsigned_earlier", "renamed_login_wrong_id"}:
        earlier["user"]["id"] = 8
    elif case == "unknown_author":
        earlier["user"] = None
    elif case == "fake_bot_login":
        earlier["user"] = {"id": 8, "type": "User", "login": "github-actions[bot]"}
    elif case == "late_signature":
        api.records[0]["created_at"] = "2026-09-23T00:00:00Z"
    elif case == "duplicate_signature":
        api.records.append(dict(api.records[0]))
    elif case == "empty_ledger":
        api.records.clear()
    elif case == "wrong_base":
        earlier["base"]["ref"] = "other"
    elif case == "wrong_repository":
        earlier["base"]["repo"]["full_name"] = "fork/other"
    elif case == "wrong_merge":
        earlier["merge_commit_sha"] = "c" * 40
    elif case == "not_merged":
        earlier["merged"] = False
    elif case == "direct_push":
        api.data[f"commits/{history[1]}/pulls?per_page=100&page=1"] = []
    else:
        api.data[f"commits/{history[1]}/pulls?per_page=100&page=1"] *= 2
    with pytest.raises(ReleaseRejectedError):
        policies.ipr(api, "push", history[-1], {})


def test_signer_identity_survives_login_rename_and_real_bots_follow_policy(
    history: list[str],
) -> None:
    api = PolicyAPI(history)
    api.data["pulls/1173"]["user"]["login"] = "renamed-user"
    api.data["pulls/1174"]["user"] = {"id": 8, "type": "Bot", "login": "release-app[bot]"}
    evidence = policies.ipr(api, "push", history[-1], {})
    assert evidence["contributions"][0]["author_id"] == 7
    assert evidence["contributions"][1]["agreement"] == "canonical-bot-exemption"


def test_all_pages_are_read_before_selecting_merged_pr(history: list[str]) -> None:
    api = PolicyAPI(history)
    path = f"commits/{history[1]}/pulls?per_page=100&page="
    actual = api.data[path + "1"]
    api.data[path + "1"] = [{**actual[0], "merge_commit_sha": "c" * 40}] * 100
    api.data[path + "2"] = actual
    policies.ipr(api, "push", history[-1], {})
    assert path + "2" in api.reads


def test_ledger_permission_failure_is_not_agreement(
    history: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = PolicyAPI(history)

    def forbidden(*args: Any) -> Any:
        raise urllib.error.HTTPError("https://api.github.com", 403, "forbidden", {}, None)

    monkeypatch.setattr(api, "request", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        policies.ipr(api, "push", history[-1], {})


@pytest.mark.parametrize("case", ["missing_floor", "floor_only", "non_sha"])
def test_incomplete_main_history_rejects(
    history: list[str], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    target = history[-1]
    if case == "missing_floor":
        monkeypatch.setattr(policies, "PUBLISHED_BASE", "c" * 40)
    elif case == "floor_only":
        target = history[0]
    else:
        target = "main"
    with pytest.raises(ReleaseRejectedError):
        policies.main_commits(target)


@pytest.mark.parametrize(
    "message",
    [
        "Merge abc into def",
        "fix: bad (description)",
        'fix: bad "description"',
        "fix: ",
        BREAKING_SUBJECT,
        BREAKING_SUBJECT + "\n\nMigration notes without a footer.",
    ],
)
def test_nonconventional_or_lost_breaking_footer_rejects(message: str) -> None:
    with pytest.raises(ReleaseRejectedError):
        policies.validate_message(message)


def test_actual_main_message_is_checked_not_pr_title(history: list[str]) -> None:
    policies.git(
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "--amend",
        "-m",
        BREAKING_SUBJECT,
    )
    target = policies.git("rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="BREAKING CHANGE footer"):
        policies.conventional("push", target, {})


@pytest.mark.parametrize("keep_footer", [True, False])
def test_two_parent_main_merge_preserves_reviewed_ancestry_and_checks_actual_footer(
    history: list[str], keep_footer: bool
) -> None:
    policies.git("checkout", "-b", "reviewed-reporting", history[0])
    policies.git(
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-m",
        "feat: reviewed reporting",
    )
    reviewed = policies.git("rev-parse", "HEAD")
    policies.git("checkout", "-b", "integration", history[-1])
    message = BREAKING_SUBJECT + ("\n\n" + BREAKING_FOOTER if keep_footer else "")
    policies.git(
        "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "reviewed-reporting", "-m", message
    )
    target = policies.git("rev-parse", "HEAD")
    assert policies.git("show", "-s", "--format=%P", target).split() == [history[-1], reviewed]
    assert policies.main_commits(target) == [*history[1:], target]
    # Policy checks the integrating PR's author for the complete contribution;
    # it does not flatten or rewrite the reviewed branch's commit ancestry.
    api = PolicyAPI([*history, target])
    assert len(policies.ipr(api, "push", target, {})["contributions"]) == 3
    if keep_footer:
        assert policies.conventional("push", target, {})["commits"][-1] == target
    else:
        with pytest.raises(RuntimeError, match="BREAKING CHANGE footer"):
            policies.conventional("push", target, {})


def test_pr_policy_uses_live_numeric_author_and_exact_head(history: list[str]) -> None:
    api = PolicyAPI(history)
    pr = api.data["pulls/1174"]
    pr.update(merged=False, merged_at=None, state="open")
    event = {"pull_request": copy.deepcopy(pr)}
    assert policies.ipr(api, "pull_request", "a" * 40, event)["contributions"][0]["author_id"] == 7
    pr["head"]["sha"] = "f" * 40
    with pytest.raises(ReleaseRejectedError, match="head changed"):
        policies.ipr(api, "pull_request", "a" * 40, event)


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "wrong_repo",
        "wrong_ref",
        "wrong_sha",
        "wrong_workflow_sha",
        "wrong_event_sha",
        "forced",
        "deleted",
        "dispatch",
    ],
)
def test_exact_push_event_is_bound_to_checkout(
    history: list[str], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    event = {"after": history[-1], "ref": "refs/heads/main", "deleted": False, "forced": False}
    environment = {
        "GITHUB_REPOSITORY": policies.REPOSITORY,
        "GITHUB_SHA": history[-1],
        "GITHUB_WORKFLOW_SHA": history[-1],
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
    }
    if case in {"forced", "deleted"}:
        event[case] = True
    elif case == "wrong_event_sha":
        event["after"] = history[1]
    elif case == "wrong_repo":
        environment["GITHUB_REPOSITORY"] = "fork/repository"
    elif case == "wrong_ref":
        environment["GITHUB_REF"] = "refs/heads/other"
    elif case == "wrong_sha":
        environment["GITHUB_SHA"] = history[1]
    elif case == "wrong_workflow_sha":
        environment["GITHUB_WORKFLOW_SHA"] = history[1]
    elif case == "dispatch":
        environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    path = policies.ROOT / "event.json"
    path.write_text(json.dumps(event))
    for key, value in {**environment, "GITHUB_EVENT_PATH": str(path)}.items():
        monkeypatch.setenv(key, value)
    if case == "valid":
        assert policies.event_context() == ("push", history[-1], event)
    else:
        with pytest.raises(ReleaseRejectedError):
            policies.event_context()


def test_native_policy_jobs_are_read_only_pinned_and_run_on_main() -> None:
    root = Path(__file__).resolve().parent.parent
    document = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    for name, command in (("ipr-policy", "ipr"), ("conventional-commits", "conventional")):
        job = document["jobs"][name]
        assert job["if"] == "github.event_name == 'pull_request' || github.ref == 'refs/heads/main'"
        assert all(value == "read" for value in job["permissions"].values())
        checkout = job["steps"][0]
        assert checkout["uses"] == "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
        assert checkout["with"] == {
            "ref": "${{ github.sha }}",
            "fetch-depth": "0",
            "persist-credentials": "false",
        }
        assert job["steps"][1]["run"] == f"python3 -m scripts.check_main_policies {command}"
        assert "secrets." not in json.dumps(job)
