"""Real read-only policy checks for PRs and the integrated main source.

GitHub Actions supplies the check-run identity. This script never creates a
status/check, records an agreement, or interprets another context as agreement.
The central IPR policy binds the PR author's numeric GitHub identity, not commit
email addresses: adcontextprotocol/adcp@82a671607c92945f0fec513c4375af583fdea914,
scripts/ipr/{check-and-record,signatures}.mjs and signatures/README.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from scripts.release_gate import REPOSITORY, SHA, GitHub, require, timestamp

ROOT = Path(__file__).resolve().parent.parent
LEDGER_REPOSITORY = "adcontextprotocol/adcp"
# Last published source before the guarded release path. This immutable floor
# never follows a tag or moves forward automatically: every subsequent main
# integration is checked again, including the complete staged foundation stack.
PUBLISHED_BASE = "3e76aa54623529a3dda01cd690b8a5c287c75641"  # v8.0.0-beta.15
CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([^()\n]+\))?(?P<breaking>!)?: (?P<description>\S.*)$"
)


def git(*arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()


def main_commits(target: str) -> list[str]:
    require(SHA.fullmatch(target), "main policy needs an exact commit SHA")
    # Require the floor on the first-parent chain, rather than accepting a
    # side-branch ancestor that would silently omit integrations.
    history = git("rev-list", "--first-parent", target).splitlines()
    require(PUBLISHED_BASE in history, "published policy floor is missing from main history")
    commits = list(reversed(history[: history.index(PUBLISHED_BASE)]))
    require(commits, "no integrations after the published policy floor")
    return commits


def validate_message(message: str) -> None:
    subject, _, body = message.partition("\n")
    match = CONVENTIONAL.fullmatch(subject)
    require(match is not None, "commit subject is not a conventional commit")
    assert match is not None
    # GitHub integration subjects may include the PR number. Parentheses/quotes in
    # the actual description remain forbidden by the repository's parser rule.
    description = re.sub(r" \(#[1-9][0-9]*\)$", "", match["description"])
    require(description and not any(c in description for c in '()"'), "unsafe release description")
    if match["breaking"]:
        require(
            re.search(r"(?m)^BREAKING(?: CHANGE|-CHANGE): \S.+$", body),
            "breaking subject requires its BREAKING CHANGE footer in the actual commit",
        )


def event_context() -> tuple[str, str, dict[str, Any]]:
    require(os.environ["GITHUB_REPOSITORY"] == REPOSITORY, "wrong policy repository")
    target = os.environ["GITHUB_SHA"]
    require(SHA.fullmatch(target), "policy needs an exact checkout SHA")
    require(git("rev-parse", "HEAD") == target, "policy checkout does not match the event")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    kind = os.environ["GITHUB_EVENT_NAME"]
    if kind == "push":
        require(os.environ["GITHUB_REF"] == "refs/heads/main", "policy push must target main")
        require(os.environ["GITHUB_WORKFLOW_SHA"] == target, "main workflow is from another SHA")
        require(event["after"] == target and event["ref"] == "refs/heads/main", "wrong push target")
        require(not event["deleted"] and not event["forced"], "deleted or forced main push")
    else:
        require(kind == "pull_request", "unsupported policy event")
        pr = event["pull_request"]
        require(pr["base"]["repo"]["full_name"] == REPOSITORY, "wrong PR repository")
        require(pr["base"]["ref"] == "main", "PR must target main")
        require(
            os.environ["GITHUB_REF"] == f"refs/pull/{pr['number']}/merge",
            "policy must check the PR merge ref",
        )
    return kind, target, event


def conventional(kind: str, target: str, event: dict[str, Any]) -> dict[str, Any]:
    if kind == "push":
        commits = main_commits(target)
    else:
        pr = event["pull_request"]
        validate_message(pr["title"] + "\n\n" + (pr["body"] or ""))
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        require(SHA.fullmatch(head) and SHA.fullmatch(base), "invalid PR source identities")
        fork = git("merge-base", base, head)
        # Only real merge commits are excluded from individual PR commits.
        # A single-parent commit named 'Merge ...' is not a bypass.
        commits = git("rev-list", "--reverse", "--no-merges", f"{fork}..{head}").splitlines()
        require(commits, "PR has no commits to validate")
    for sha in commits:
        try:
            validate_message(git("show", "-s", "--format=%B", sha))
        except RuntimeError as error:
            raise RuntimeError(f"invalid integrated commit {sha}: {error}") from error
    return {"target_sha": target, "commits": commits, "policy": "conventional-commits"}


def signatures(api: GitHub) -> tuple[str, dict[int, dict[str, Any]]]:
    remote = f"/repos/{LEDGER_REPOSITORY}"
    ref = api.request(remote + "/git/ref/heads/main")
    sha = ref["object"]["sha"]
    require(ref["object"]["type"] == "commit" and SHA.fullmatch(sha), "invalid ledger revision")
    blob = api.request(remote + f"/contents/signatures/ipr-signatures.json?ref={sha}")
    require(blob["encoding"] == "base64", "unreadable central agreement ledger")
    ledger = json.loads(base64.b64decode("".join(blob["content"].split()), validate=True))
    entries = ledger["signedContributors"]
    require(isinstance(entries, list) and entries, "missing canonical signature records")
    signers: dict[int, dict[str, Any]] = {}
    for entry in entries:
        identifier = entry["id"]
        require(type(identifier) is int and identifier > 0, "invalid signer GitHub identity")
        require(identifier not in signers, "ambiguous signer GitHub identity")
        require(entry["name"] and entry["method"], "incomplete signature record")
        timestamp(entry["created_at"])
        signers[identifier] = entry
    return sha, signers


def author_agreement(pr: dict[str, Any], signers: dict[int, dict[str, Any]]) -> dict[str, Any]:
    user = pr["user"]
    require(isinstance(user, dict), "PR has no authenticated author")
    identifier = user["id"]
    require(type(identifier) is int and identifier > 0, "PR has no authenticated author ID")
    # Follow the canonical bot exemption using the API's account type. A
    # missing/deleted author or a suggestive login string is not a bot identity.
    if user["type"] == "Bot":
        return {"author_id": identifier, "agreement": "canonical-bot-exemption"}
    require(user["type"] == "User", "unknown PR author type")
    require(identifier in signers, f"PR #{pr['number']} author ID {identifier} has not signed")
    entry = signers[identifier]
    if pr.get("merged_at"):
        require(
            timestamp(entry["created_at"]) <= timestamp(pr["merged_at"]),
            "agreement was recorded after the contribution merged",
        )
    return {"author_id": identifier, "agreement": entry["created_at"]}


def validate_pr(pr: dict[str, Any]) -> None:
    require(pr["base"]["ref"] == "main", "contribution targets another branch")
    require(pr["base"]["repo"]["full_name"] == REPOSITORY, "foreign contribution repository")


def ipr(api: GitHub, kind: str, target: str, event: dict[str, Any]) -> dict[str, Any]:
    ledger_sha, signers = signatures(api)
    contributions = []
    if kind == "push":
        for sha in main_commits(target):
            candidates = [
                pr
                for pr in api.pages(f"commits/{sha}/pulls")
                if pr["merge_commit_sha"] == sha
                and pr["merged_at"]
                and pr["base"]["ref"] == "main"
                and pr["base"]["repo"]["full_name"] == REPOSITORY
            ]
            require(len(candidates) == 1, f"no unique merged contribution for main commit {sha}")
            pr = api.repo(f"pulls/{candidates[0]['number']}")
            validate_pr(pr)
            require(pr["merged"] is True and pr["merge_commit_sha"] == sha, "wrong merged PR")
            require(pr["merged_at"], "missing contribution merge time")
            contributions.append({"sha": sha, "pr": pr["number"], **author_agreement(pr, signers)})
    else:
        expected = event["pull_request"]
        pr = api.repo(f"pulls/{expected['number']}")
        validate_pr(pr)
        require(pr["head"]["sha"] == expected["head"]["sha"], "PR head changed during policy check")
        require(pr["state"] == "open" and not pr["merged"], "PR is no longer open")
        contributions.append(
            {"sha": pr["head"]["sha"], "pr": pr["number"], **author_agreement(pr, signers)}
        )
    return {
        "policy": "IPR Policy / Signature",
        "target_sha": target,
        "published_base": PUBLISHED_BASE,
        "ledger_repository": LEDGER_REPOSITORY,
        "ledger_sha": ledger_sha,
        "contributions": contributions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", choices=("ipr", "conventional"))
    args = parser.parse_args()
    kind, target, event = event_context()
    if args.policy == "ipr":
        result = ipr(GitHub(os.environ["GH_TOKEN"]), kind, target, event)
    else:
        result = conventional(kind, target, event)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
