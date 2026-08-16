"""Release automation is the single source of truth for package versions."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.normalize_pyproject_prerelease import pep440_prerelease

ROOT = Path(__file__).parent.parent


def test_worktree_version_matches_normalized_release_manifest() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    project_section = re.search(
        r'^\[project\]\s*$.*?^version\s*=\s*"([^"]+)"',
        pyproject,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert project_section is not None
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert project_section.group(1) == pep440_prerelease(manifest["."])


def test_release_please_targets_stable_versions() -> None:
    config = json.loads((ROOT / "release-please-config.json").read_text())
    package = config["packages"]["."]
    assert "versioning" not in package
    assert "prerelease-type" not in package
    assert "prerelease" not in package
