"""Release automation is the single source of truth for the v7 RC version."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_worktree_version_matches_last_release_manifest() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    project_section = re.search(
        r'^\[project\]\s*$.*?^version\s*=\s*"([^"]+)"',
        pyproject,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert project_section is not None
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert project_section.group(1) == manifest["."]


def test_release_please_targets_v7_rc_from_breaking_commit() -> None:
    config = json.loads((ROOT / "release-please-config.json").read_text())
    package = config["packages"]["."]
    assert package["versioning"] == "prerelease"
    assert package["prerelease-type"] == "rc"
    assert package["prerelease"] is True
