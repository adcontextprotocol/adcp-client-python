"""Read-only release policy: absent, stale, or ambiguous evidence fails closed.

The live main freeze closes the check/use race that a SHA comparison alone
cannot close. Approval and the freeze are operator configuration; this program
never creates or changes either. Call it again immediately before each write.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

REPOSITORY = "adcontextprotocol/adcp-client-python"
CI_PATH = ".github/workflows/ci.yml"
ACCEPTANCE_PATH = ".github/workflows/release-acceptance.yml"
WORKFLOWS = {
    "proposal": ".github/workflows/release-proposal.yml",
    "publish": ".github/workflows/release-publish.yml",
}
MAX_AGE = timedelta(hours=24)
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
RELEASE_BRANCHES = {
    "release-please--branches--main",
    "release-please--branches--main--components--adcp",
}
# A protection edit must not silently remove the runtime CI floor. Additional
# required checks are discovered live and must also pass, without skip/neutral
# exceptions (including the currently PR-only policy checks).
CI_FLOOR = {
    "Test Python 3.10",
    "Test Python 3.11",
    "Test Python 3.12",
    "Test Python 3.13",
    "Validate schemas are up-to-date",
    "Postgres conformance tests (Postgres 16)",
    "Downstream import smoke (representative consumer symbols)",
    "v3 reference seller — pytest (respx-mocked upstream)",
    "AdCP storyboard runner — examples/seller_agent.py",
    "AdCP storyboard runner — examples/multi_platform_seller (PlatformRouter)",
    "AdCP storyboard runner — sales-proposal-mode (proposal_finalize)",
    "AdCP storyboard runner — v3 reference seller (translator)",
}


class ReleaseRejectedError(RuntimeError):
    """Evidence is absent, ambiguous, stale, or inconsistent."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ReleaseRejectedError(message)


def positive_id(value: str) -> int:
    require(bool(re.fullmatch(r"[1-9][0-9]*", value)), "expected a positive decimal ID")
    return int(value)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp must include a timezone")
    return parsed


def fresh(value: str, now: datetime) -> None:
    age = now - timestamp(value)
    require(timedelta(0) <= age <= MAX_AGE, "evidence is stale or from the future")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class GitHub:
    """Small REST client; pagination and download integrity fail closed."""

    def __init__(self, token: str) -> None:
        require(token, "GH_TOKEN is required")
        self.token = token

    def request(self, path: str, method: str = "GET", body: Any = None) -> Any:
        require(path.startswith("/"), "API path must be relative")
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method=method,
        )
        # Never forward the GitHub credential to a redirect destination.
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            content = response.read()
        return json.loads(content) if content else None

    def repo(self, suffix: str, method: str = "GET", body: Any = None) -> Any:
        return self.request(f"/repos/{REPOSITORY}/{suffix}", method, body)

    def optional(self, suffix: str) -> Any:
        try:
            return self.repo(suffix)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def pages(self, suffix: str, key: str | None = None) -> list[Any]:
        items: list[Any] = []
        separator = "&" if "?" in suffix else "?"
        for page in range(1, 101):
            response = self.repo(f"{suffix}{separator}per_page=100&page={page}")
            batch = response if key is None else response[key]
            require(isinstance(batch, list), "invalid paginated API response")
            items.extend(batch)
            if len(batch) < 100:
                if key is not None:
                    require(len(items) == response["total_count"], "incomplete API inventory")
                return items
        raise ReleaseRejectedError("API inventory exceeds the pagination limit")

    def artifact_bytes(self, artifact_id: int) -> bytes:
        try:
            self.repo(f"actions/artifacts/{artifact_id}/zip")
        except urllib.error.HTTPError as error:
            require(error.code == 302, "artifact download did not return a redirect")
            location = error.headers["Location"]
        else:
            raise ReleaseRejectedError("artifact download did not return a redirect")
        parsed = urllib.parse.urlparse(location)
        require(parsed.scheme == "https" and parsed.hostname, "invalid artifact download URL")
        # A separate opener/request intentionally has no Authorization header.
        with urllib.request.build_opener(NoRedirect).open(location, timeout=60) as response:
            return cast(bytes, response.read())


@dataclass(frozen=True)
class Context:
    target: str
    operation: str
    run_id: int
    ci_run_id: int
    ci_attempt: int
    release_pr: int | None = None
    recover_from: int | None = None

    @classmethod
    def from_env(cls) -> Context:
        env = os.environ
        target = env["TARGET_SHA"]
        operation = env["OPERATION"]
        require(operation in WORKFLOWS, "unknown release operation")
        require(SHA.fullmatch(target), "target must be an explicit full commit SHA")
        require(env["GITHUB_REPOSITORY"] == REPOSITORY, "wrong repository")
        require(env["GITHUB_EVENT_NAME"] == "workflow_dispatch", "dispatch required")
        require(env["GITHUB_REF"] == "refs/heads/main", "only the main branch may dispatch")
        require(env["GITHUB_RUN_ATTEMPT"] == "1", "reruns are forbidden; make a fresh dispatch")
        require(env["GITHUB_SHA"] == target, "dispatch and target SHAs differ")
        require(env["GITHUB_WORKFLOW_SHA"] == target, "workflow and target SHAs differ")
        require(
            env["GITHUB_WORKFLOW_REF"] == f"{REPOSITORY}/{WORKFLOWS[operation]}@refs/heads/main",
            "unapproved workflow entry point",
        )
        release_pr = env.get("RELEASE_PR", "")
        recovery = env.get("RECOVER_FROM", "")
        if operation == "publish":
            require(env.get("PUBLICATION_ENABLED") == "true", "publication is disabled")
            require(release_pr, "the merged release PR is required")
        else:
            require(not release_pr and not recovery, "proposal cannot request publication/recovery")
        return cls(
            target,
            operation,
            positive_id(env["GITHUB_RUN_ID"]),
            positive_id(env["CI_RUN_ID"]),
            positive_id(env["CI_RUN_ATTEMPT"]),
            positive_id(release_pr) if release_pr else None,
            positive_id(recovery) if recovery else None,
        )


def validate_run(run: dict[str, Any], target: str, path: str, event: str, attempt: int) -> None:
    require(run["repository"]["full_name"] == REPOSITORY, "wrong workflow repository")
    require(run["head_repository"]["full_name"] == REPOSITORY, "fork workflow evidence")
    require(run["head_branch"] == "main" and run["head_sha"] == target, "wrong run target")
    run_path, separator, revision = run["path"].partition("@")
    require(run_path == path and run["event"] == event, "wrong workflow provenance")
    require(
        not separator or revision in {"main", "refs/heads/main", target},
        "workflow path references another revision",
    )
    require(run["run_attempt"] == attempt, "workflow attempt changed")


def validate_environment(api: GitHub, operation: str) -> None:
    name = "release-" + operation
    environment = api.repo(f"environments/{name}")
    require(environment["can_admins_bypass"] is False, "environment allows admin bypass")
    reviews = [r for r in environment["protection_rules"] if r["type"] == "required_reviewers"]
    require(len(reviews) == 1, "environment requires an explicit reviewer rule")
    require(reviews[0]["prevent_self_review"] is True, "environment permits self approval")
    require(reviews[0]["reviewers"], "environment has no reviewers")
    require(
        environment["deployment_branch_policy"]
        == {"protected_branches": False, "custom_branch_policies": True},
        "environment must restrict deployments to the main branch",
    )
    branches = api.pages(f"environments/{name}/deployment-branch-policies", "branch_policies")
    require(
        len(branches) == 1 and branches[0]["name"] == "main" and branches[0]["type"] == "branch",
        "environment must allow exactly branch main, with no tags",
    )


def validate_freeze(api: GitHub, rules: list[dict[str, Any]], target: str, now: datetime) -> None:
    # GitHub can redact bypass_actors from read-only callers. A repository
    # administrator records this narrow assertion after inspecting the full
    # ruleset; the live ID/revision and target must still match on every gate.
    # Never grant administration/write credentials to the acceptance job just
    # to read that field, or interpret an omitted field as an empty list.
    attestation = json.loads(os.environ.get("MAIN_FREEZE_ATTESTATION", "null"))
    require(isinstance(attestation, dict), "operator main-freeze attestation is missing")
    require(
        set(attestation)
        == {
            "schema",
            "repository",
            "target_sha",
            "ruleset_id",
            "ruleset_updated_at",
            "observed_at",
            "bypass_actors",
        },
        "operator main-freeze attestation is incomplete",
    )
    require(
        attestation["schema"] == 1
        and attestation["repository"] == REPOSITORY
        and attestation["target_sha"] == target
        and attestation["bypass_actors"] == [],
        "operator main-freeze attestation does not authorize this target",
    )
    fresh(attestation["observed_at"], now)
    require(
        timestamp(attestation["ruleset_updated_at"]) <= timestamp(attestation["observed_at"]),
        "freeze attestation predates the ruleset revision",
    )
    freezes = [r for r in rules if r["type"] == "update"]
    require(freezes, "main must be frozen by an active restrict-updates ruleset")
    for rule in freezes:
        if (
            rule["ruleset_source_type"] != "Repository"
            or rule["ruleset_id"] != attestation["ruleset_id"]
        ):
            continue
        detail = api.repo(f"rulesets/{rule['ruleset_id']}")
        if (
            detail["id"] == attestation["ruleset_id"]
            and detail["updated_at"] == attestation["ruleset_updated_at"]
            and detail["enforcement"] == "active"
            and detail["target"] == "branch"
            and any(
                r["type"] == "update" and r["parameters"]["update_allows_fetch_and_merge"] is False
                for r in detail["rules"]
            )
        ):
            if "bypass_actors" in detail:
                require(detail["bypass_actors"] == [], "live main freeze permits bypass")
            return
    raise ReleaseRejectedError("live main freeze differs from the operator-attested revision")


def validate_checks(
    rules: list[dict[str, Any]], checks: list[dict[str, Any]], target: str, now: datetime
) -> None:
    required = [
        check
        for rule in rules
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    ]
    require(CI_FLOOR <= {check["context"] for check in required}, "protected CI floor is missing")
    for expected in required:
        require(expected["integration_id"] is not None, "required check has no trusted App binding")
        matches = [
            check
            for check in checks
            if check["name"] == expected["context"]
            and check["app"]["id"] == expected["integration_id"]
        ]
        require(len(matches) == 1, f"missing or ambiguous check: {expected['context']}")
        check = matches[0]
        require(check["head_sha"] == target, "check belongs to another commit")
        require(
            check["status"] == "completed" and check["conclusion"] == "success",
            f"required check is not successful: {expected['context']}",
        )
        fresh(check["completed_at"], now)


def validate_legacy_retirement(api: GitHub, now: datetime) -> None:
    """A disabled flag does not retire historical definitions or their tokens."""
    runs = api.pages("actions/workflows/204238826/runs", "workflow_runs")
    for run in runs:
        require(run["status"] == "completed", "a legacy release invocation has not drained")
        require(
            now - timestamp(run["created_at"]) > timedelta(days=30),
            "a legacy release invocation is still within GitHub's rerun window",
        )


def gate(api: GitHub, context: Context, now: datetime | None = None) -> dict[str, Any]:
    """Re-read every mutable prerequisite; no cached successful gate is used."""
    now = now or datetime.now(timezone.utc)
    require(
        api.repo("actions/workflows/204238826")["state"] == "disabled_manually",
        "the retired Release Please workflow must remain disabled",
    )
    if context.operation == "publish":
        validate_legacy_retirement(api, now)
    require(api.repo("git/ref/heads/main")["object"]["sha"] == context.target, "stale main target")
    rules = api.pages("rules/branches/main")
    validate_freeze(api, rules, context.target, now)
    validate_environment(api, context.operation)
    run = api.repo(f"actions/runs/{context.run_id}")
    validate_run(run, context.target, WORKFLOWS[context.operation], "workflow_dispatch", 1)
    fresh(run["created_at"], now)
    require(run["status"] == "in_progress", "invocation is no longer active")
    # Local reusable workflows resolve at the caller's commit. Also verify the
    # API's provenance, independently of the YAML needs dependency.
    require(
        any(
            workflow["path"].split("@")[0] == f"{REPOSITORY}/{ACCEPTANCE_PATH}"
            and workflow["sha"] == context.target
            for workflow in run["referenced_workflows"]
        ),
        "approved acceptance workflow is not bound to this commit",
    )
    ci = api.repo(f"actions/runs/{context.ci_run_id}")
    validate_run(ci, context.target, CI_PATH, "push", context.ci_attempt)
    require(ci["status"] == "completed" and ci["conclusion"] == "success", "CI is incomplete")
    fresh(ci["updated_at"], now)
    jobs = api.pages(f"actions/runs/{context.ci_run_id}/attempts/{context.ci_attempt}/jobs", "jobs")
    require(CI_FLOOR | {"Workflow security"} <= {job["name"] for job in jobs}, "CI jobs missing")
    for job in jobs:
        require(job["head_sha"] == context.target, "CI job belongs to another commit")
        require(
            job["status"] == "completed" and job["conclusion"] == "success",
            f"CI job is not successful: {job['name']}",
        )
        fresh(job["completed_at"], now)
    checks = api.pages(f"commits/{context.target}/check-runs?filter=latest", "check_runs")
    validate_checks(rules, checks, context.target, now)
    job_checks = {job["check_run_url"] for job in jobs}
    for check in checks:
        if check["name"] in CI_FLOOR:
            require(
                f"https://api.github.com/repos/{REPOSITORY}/check-runs/{check['id']}" in job_checks,
                "protected CI check does not belong to the selected CI attempt",
            )
    if context.release_pr is not None:
        pr = api.repo(f"pulls/{context.release_pr}")
        require(
            pr["merged"] is True and pr["merge_commit_sha"] == context.target, "wrong release PR"
        )
        require(pr["base"]["ref"] == "main", "release PR must target main")
        require(pr["base"]["repo"]["full_name"] == REPOSITORY, "wrong release PR repository")
        require(pr["head"]["repo"]["full_name"] == REPOSITORY, "fork release PR")
        require(pr["head"]["ref"] in RELEASE_BRANCHES, "not the normal Release Please branch")
        require(
            {label["name"] for label in pr["labels"]}
            & {"autorelease:pending", "autorelease:tagged"},
            "release PR has no Release Please marker",
        )
    # Detect movement even during a long paginated read.
    require(
        api.repo("git/ref/heads/main")["object"]["sha"] == context.target, "main moved during gate"
    )
    return {"ci_run_id": context.ci_run_id, "ci_run_attempt": context.ci_attempt}


def normalize_proposal(
    reader: GitHub, writer: GitHub, context: Context, prs: list[dict[str, Any]]
) -> None:
    """Normalize the proposed file via the API; never execute release-branch code."""
    from scripts.normalize_pyproject_prerelease import normalize_pyproject_text

    require(context.operation == "proposal", "normalization is proposal-only")
    require(len(prs) <= 1, "unexpected multiple release PRs")
    for proposed in prs:
        pr = reader.repo(f"pulls/{int(proposed['number'])}")
        require(pr["base"]["ref"] == "main", "wrong proposal base")
        require(pr["head"]["repo"]["full_name"] == REPOSITORY, "fork proposal")
        branch = pr["head"]["ref"]
        require(branch in RELEASE_BRANCHES, "unexpected proposal branch")
        encoded_branch = urllib.parse.quote(branch, safe="")
        original = reader.repo(f"contents/pyproject.toml?ref={encoded_branch}")
        text = base64.b64decode(original["content"]).decode()
        normalized = normalize_pyproject_text(text)
        if normalized != text:
            gate(reader, context)
            writer.repo(
                "contents/pyproject.toml",
                "PUT",
                {
                    "message": "chore: normalize prerelease version to PEP 440",
                    "content": base64.b64encode(normalized.encode()).decode(),
                    "sha": original["sha"],
                    "branch": branch,
                },
            )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalize-proposal", action="store_true")
    args = parser.parse_args()
    context = Context.from_env()
    api = GitHub(os.environ["GH_TOKEN"])
    gate(api, context)
    if args.normalize_proposal:
        normalize_proposal(
            api,
            GitHub(os.environ["PROPOSAL_TOKEN"]),
            context,
            json.loads(os.environ["PROPOSAL_PRS"] or "[]"),
        )
    print(f"Accepted current main {context.target} for {context.operation} run {context.run_id}")


if __name__ == "__main__":
    main()
