"""Release automation owns the SDK version and the SDK 9 stable channel."""

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


def test_release_please_is_on_the_stable_channel() -> None:
    """SDK 9 left the beta channel at the reviewed GA exit.

    The GA commit's ``Release-As: 9.0.0`` footer selects the stable release.
    With the prerelease keys removed, later fixes bump stable versions and
    GitHub releases are no longer marked as prereleases.
    """
    config = json.loads((ROOT / "release-please-config.json").read_text())
    package = config["packages"]["."]
    for key in ("versioning", "prerelease-type", "prerelease"):
        assert key not in package, key


STORAGE = ROOT / "packages/adcp-reporting-storage"


def test_storage_distribution_version_matches_its_manifest_entry() -> None:
    """adcp-reporting-storage is its own Release Please component on the beta channel."""
    pyproject = (STORAGE / "pyproject.toml").read_text()
    project_section = re.search(
        r'^\[project\]\s*$.*?^version\s*=\s*"([^"]+)"',
        pyproject,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert project_section is not None
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert project_section.group(1) == pep440_prerelease(
        manifest["packages/adcp-reporting-storage"]
    )
    config = json.loads((ROOT / "release-please-config.json").read_text())
    package = config["packages"]["packages/adcp-reporting-storage"]
    assert package["component"] == package["package-name"] == "adcp-reporting-storage"
    assert package["versioning"] == "prerelease"
    assert package["prerelease-type"] == "beta"
    assert package["prerelease"] is True
    # Its tags carry the component name; the root keeps bare vX.Y.Z tags.
    assert package["include-component-in-tag"] is True
    assert config["packages"]["."]["include-component-in-tag"] is False
    # Two components, two release PRs: the normalizer and the publish queue
    # are written for that.
    assert config["separate-pull-requests"] is True
    # Release Please hands the root path every commit unless told otherwise;
    # without this a storage-only fix or breaking change would release adcp.
    assert config["packages"]["."]["exclude-paths"] == ["packages/adcp-reporting-storage"]


def test_storage_distribution_pins_one_adcp_major() -> None:
    pyproject = (STORAGE / "pyproject.toml").read_text()
    assert re.search(r'"adcp>=9\.0\.0(?:b\d+)?,<10"', pyproject), "adcp range must be one major"
