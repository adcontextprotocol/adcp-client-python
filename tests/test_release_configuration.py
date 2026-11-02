"""Release automation owns the SDK version and the SDK 9 stable channel."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

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


def test_release_publish_gates_both_packages_on_a_working_companion() -> None:
    """The companion gate (#1605, #1763) runs for every publish of either package.

    It runs after the build, so the wheel just built is installed beside the
    published companion and smoked, with a PostgreSQL service available: a
    pair whose version ranges admit each other but whose storage contracts
    differ never reaches PyPI.
    """
    publisher = yaml.load(
        (ROOT / ".github/workflows/release-publish.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = publisher["jobs"]["build"]["steps"]
    companion_steps = [
        index
        for index, step in enumerate(steps)
        if "check_companion_releases.py" in step.get("run", "")
    ]
    assert len(companion_steps) == 1
    companion = steps[companion_steps[0]]
    assert "if" not in companion, "both packages are gated"
    assert "--dist dist/adcp-*.whl" in companion["run"]
    assert '--package "$PACKAGE" --dist dist/adcp_reporting_storage-*.whl' in companion["run"]
    assert companion["env"]["ADCP_PG_TEST_URL"].startswith("postgresql://")
    build_steps = [
        index for index, step in enumerate(steps) if "python -m build" in step.get("run", "")
    ]
    assert build_steps and build_steps[0] < companion_steps[0], "gate the built wheel"
    assert publisher["jobs"]["build"]["services"]["postgres"]["image"].startswith("postgres:")


def test_storage_distribution_pins_one_adcp_major() -> None:
    pyproject = (STORAGE / "pyproject.toml").read_text()
    assert re.search(r'"adcp>=9\.0\.0(?:b\d+)?,<10"', pyproject), "adcp range must be one major"
