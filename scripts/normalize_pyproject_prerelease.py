#!/usr/bin/env python3
"""Normalize release-please prerelease versions in pyproject.toml."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_SEMVER_PRERELEASE_RE = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)-(?P<label>alpha|beta|rc)\.(?P<number>0|[1-9]\d*)$"
)
_PYPROJECT_SECTION_RE = re.compile(r"^\s*\[(?P<section>[^\]]+)\]\s*$")
_PYPROJECT_VERSION_LINE_RE = re.compile(
    r'^(?P<prefix>\s*version\s*=\s*")'
    r"(?P<version>\d+\.\d+\.\d+-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))"
    r'(?P<suffix>"\s*)$'
)
_PEP440_LABELS = {
    "alpha": "a",
    "beta": "b",
    "rc": "rc",
}
_REPOSITORY = "adcontextprotocol/adcp-client-python"
_RELEASE_BRANCHES = {
    "release-please--branches--main",
    "release-please--branches--main--components--adcp",
}


def pep440_prerelease(version: str) -> str:
    """Convert a SemVer prerelease version to PEP 440 when needed."""
    match = _SEMVER_PRERELEASE_RE.fullmatch(version)
    if not match:
        return version

    label = _PEP440_LABELS[match.group("label")]
    return f"{match.group('release')}{label}{match.group('number')}"


def normalize_pyproject_text(text: str) -> str:
    """Normalize a pyproject.toml project version line if it is a prerelease."""

    lines = text.splitlines(keepends=True)
    current_section: str | None = None
    for index, line in enumerate(lines):
        content = line.removesuffix("\n")
        newline = "\n" if line.endswith("\n") else ""
        section_match = _PYPROJECT_SECTION_RE.match(content)
        if section_match:
            current_section = section_match.group("section").strip()
            continue

        if current_section != "project":
            continue

        version_match = _PYPROJECT_VERSION_LINE_RE.match(content)
        if not version_match:
            continue

        lines[index] = (
            f"{version_match.group('prefix')}"
            f"{pep440_prerelease(version_match.group('version'))}"
            f"{version_match.group('suffix')}"
            f"{newline}"
        )

    return "".join(lines)


def normalize_pyproject(path: Path) -> bool:
    """Normalize pyproject.toml in place.

    Returns True when the file changed.
    """
    text = path.read_text(encoding="utf-8")
    normalized = normalize_pyproject_text(text)
    if normalized == text:
        return False

    path.write_text(normalized, encoding="utf-8")
    return True


def _github(path: str, token: str, *, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{_REPOSITORY}/{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET" if body is None else "PUT",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def normalize_release_prs(prs: list[dict[str, Any]], token: str) -> None:
    """Normalize the one Release Please PR with the App credential that opened it."""
    if len(prs) > 1:
        raise ValueError("expected at most one release PR")
    for proposed in prs:
        number = int(proposed["number"])
        pr = _github(f"pulls/{number}", token)
        branch = pr["head"]["ref"]
        if (
            pr["base"]["ref"] != "main"
            or pr["base"]["repo"]["full_name"] != _REPOSITORY
            or pr["head"]["repo"]["full_name"] != _REPOSITORY
            or branch not in _RELEASE_BRANCHES
        ):
            raise ValueError("unexpected release PR identity")
        original = _github(
            f"contents/pyproject.toml?ref={urllib.parse.quote(branch, safe='')}", token
        )
        contents = base64.b64decode(original["content"], validate=False).decode()
        normalized = normalize_pyproject_text(contents)
        if normalized != contents:
            _github(
                "contents/pyproject.toml",
                token,
                body={
                    "message": "chore: normalize prerelease version to PEP 440",
                    "content": base64.b64encode(normalized.encode()).decode(),
                    "sha": original["sha"],
                    "branch": branch,
                },
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize release-please SemVer prereleases to PEP 440 in pyproject.toml."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="pyproject.toml",
        type=Path,
        help="Path to pyproject.toml.",
    )
    parser.add_argument("--release-prs", action="store_true", help="Normalize Release Please PRs")
    args = parser.parse_args()

    if args.release_prs:
        normalize_release_prs(
            json.loads(os.environ["PROPOSAL_PRS"] or "[]"), os.environ["GH_TOKEN"]
        )
        return 0

    changed = normalize_pyproject(args.path)
    if changed:
        print(f"Normalized prerelease version in {args.path}")
    else:
        print(f"No prerelease normalization needed for {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
