"""Require parseable, component-specific notes on a merged Release Please PR."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

REPOSITORY = "adcontextprotocol/adcp-client-python"
COMPONENTS = {
    "adcp": ".",
    "adcp-reporting-storage": "packages/adcp-reporting-storage",
}
NOTES_HEADER = re.compile(r"^## \[([^\]]+)\]\(.+\)(?: \(.+\))?$")
DELIMITER = re.compile(r"(?m)^---\s*$")


def validate_release_pr(
    pr: dict[str, Any], sha: str, branch: str, manifest: dict[str, str]
) -> None:
    """Validate the notes section Release Please parses for this component."""
    if pr.get("merged_at") is None or pr.get("merge_commit_sha") != sha:
        raise ValueError("release PR is not the merged commit")
    if pr["base"]["ref"] != branch:
        raise ValueError("release PR targets another branch")
    prefix = f"release-please--branches--{branch}--components--"
    head = pr["head"]["ref"]
    if not head.startswith(prefix):
        raise ValueError("unexpected release PR branch")
    component = head.removeprefix(prefix)
    if component not in COMPONENTS:
        raise ValueError(f"unknown release component: {component}")
    expected_version = manifest[COMPONENTS[component]]
    sections = DELIMITER.split(pr.get("body") or "")
    if len(sections) != 3:
        raise ValueError(f"{component} release PR must contain two notes delimiters")
    notes = sections[1].strip().splitlines()
    match = NOTES_HEADER.fullmatch(notes[0]) if notes else None
    if (
        match is None
        or match[1] != expected_version
        or len(re.findall(r"(?m)^## \[", sections[1])) != 1
    ):
        raise ValueError(f"{component} release notes must start with version {expected_version}")


def merged_release_prs(sha: str, token: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/commits/{sha}/pulls?per_page=100",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        prs = json.load(response)
    if not isinstance(prs, list) or len(prs) == 100:
        raise ValueError("incomplete merged PR list")
    return prs


def check_commit(sha: str, branch: str, token: str, root: Path) -> None:
    manifest_path = root / (
        ".release-please-manifest.next.json"
        if branch == "next"
        else ".release-please-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    candidates = [
        pr
        for pr in merged_release_prs(sha, token)
        if pr.get("merge_commit_sha") == sha
        and pr.get("base", {}).get("ref") == branch
        and pr.get("head", {}).get("ref", "").startswith(f"release-please--branches--{branch}")
    ]
    for pr in candidates:
        validate_release_pr(pr, sha, branch, manifest)
    print(f"Validated {len(candidates)} merged release PR(s) at {sha}")


def main() -> None:
    branch = os.environ["GITHUB_REF_NAME"]
    if branch not in {"main", "next"}:
        raise ValueError("unexpected release branch")
    check_commit(os.environ["GITHUB_SHA"], branch, os.environ["GH_TOKEN"], Path.cwd())


if __name__ == "__main__":
    main()
