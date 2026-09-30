"""Release automation is the single source of truth for the stable SDK 8 line."""

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


def test_release_please_uses_stable_semver_versioning() -> None:
    """SDK 8 is stable: no prerelease channel may be configured.

    The ``Release-As: 8.0.0`` footer on the adopting commit selects the first
    stable version. Without these keys, later ``fix:``/``feat:`` commits bump
    normal SemVer (8.0.1, 8.1.0) instead of starting another rc series.
    """
    config = json.loads((ROOT / "release-please-config.json").read_text())
    package = config["packages"]["."]
    for key in ("versioning", "prerelease-type", "prerelease"):
        assert key not in package, key
