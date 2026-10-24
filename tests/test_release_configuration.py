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
    expected = {pep440_prerelease(manifest["."])}
    # On the next draft-spec line, pyproject.toml carries next's own release
    # version once next has released; until then it still carries main's.
    next_manifest = ROOT / ".release-please-manifest.next.json"
    if next_manifest.exists():
        expected.add(pep440_prerelease(json.loads(next_manifest.read_text())["."]))
    assert project_section.group(1) in expected


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


def test_release_please_keeps_next_on_the_alpha_channel() -> None:
    """The next draft-spec line releases only ``10.0.0-alpha.N`` pre-releases.

    The adopting commit on next ends with ``Release-As: 10.0.0-alpha.0``;
    later commits increment the alpha number. next's workflow uses its own
    config and manifest so that merging main into next never changes them.
    """
    config_path = ROOT / "release-please-config.next.json"
    if not config_path.exists():
        return  # main does not carry next's release configuration.
    config = json.loads(config_path.read_text())
    package = config["packages"]["."]
    assert package["versioning"] == "prerelease"
    assert package["prerelease-type"] == "alpha"
    assert package["prerelease"] is True
    assert package["changelog-path"] != "CHANGELOG.md"
    workflow = (ROOT / ".github/workflows/release-please-next.yml").read_text()
    assert "target-branch: next" in workflow
    assert "config-file: release-please-config.next.json" in workflow
    assert "manifest-file: .release-please-manifest.next.json" in workflow
    assert "--target-branch next" in workflow
    assert "release-publish.yml --ref next -f package=adcp" in workflow
    # next releases adcp only. The adcp-reporting-storage component is
    # released from main; storage-only commits stay out of next's changelog.
    assert set(config["packages"]) == {"."}
    assert package["exclude-paths"] == ["packages/adcp-reporting-storage"]
    assert "package=adcp-reporting-storage" not in workflow


def test_storage_distribution_pins_one_adcp_major() -> None:
    """The storage distribution's adcp range is one major on main.

    On the next draft-spec line the range also spans adcp 10 pre-releases, or
    next's first ``10.0.0aN`` release could not install the storage package
    from the workspace. next never releases the storage distribution; its
    first adcp-10 release sets the one-major range again (see RELEASING.md).
    """
    pyproject = (STORAGE / "pyproject.toml").read_text()
    if (ROOT / "release-please-config.next.json").exists():
        assert re.search(r'"adcp>=9\.0\.0(?:b\d+)?,<11"', pyproject), "next spans adcp 9 and 10"
    else:
        assert re.search(r'"adcp>=9\.0\.0(?:b\d+)?,<10"', pyproject), "adcp range must be one major"
