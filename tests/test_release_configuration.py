"""Release automation owns the SDK version and the SDK 9 beta channel."""

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


def test_release_please_keeps_sdk_9_on_the_beta_channel() -> None:
    """The breaking SDK 9 surface must remain beta until the reviewed GA exit.

    The adopting commit's ``Release-As: 9.0.0-beta.1`` footer selects the
    first beta. Subsequent fixes increment the beta, and GitHub releases
    are marked as prereleases rather than presented as stable releases.
    """
    config = json.loads((ROOT / "release-please-config.json").read_text())
    package = config["packages"]["."]
    assert package["versioning"] == "prerelease"
    assert package["prerelease-type"] == "beta"
    assert package["prerelease"] is True
