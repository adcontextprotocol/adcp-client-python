"""Release tags and publication must describe the component being released."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.ci.check_publish_version import check_publish_version
from scripts.ci.check_release_pr import validate_release_pr

ROOT = Path(__file__).parent.parent
SHA = "a" * 40
MANIFEST = {".": "10.0.0-beta.21", "packages/adcp-reporting-storage": "0.2.0-beta.4"}


def release_pr(component: str, version: str, branch: str = "next") -> dict:
    suffix = "" if component == "adcp" else f" {component}"
    return {
        "merged_at": "2026-11-20T11:37:41Z",
        "merge_commit_sha": SHA,
        "base": {"ref": branch},
        "head": {"ref": f"release-please--branches--{branch}--components--{component}"},
        "body": (
            ":robot: I have created a release *beep* *boop*\n---\n\n"
            f"## [{version}](https://example.invalid/compare) (2026-11-20)\n\n"
            f"### Features\n\n* Release{suffix}\n\n---\n"
            "This PR was generated with Release Please."
        ),
    }


@pytest.mark.parametrize(
    "component,version", [("adcp", "10.0.0-beta.21"), ("adcp-reporting-storage", "0.2.0-beta.4")]
)
def test_release_pr_accepts_component_notes(component: str, version: str) -> None:
    validate_release_pr(release_pr(component, version), SHA, "next", MANIFEST)


def test_main_release_pr_accepts_stable_notes() -> None:
    validate_release_pr(release_pr("adcp", "9.6.3", "main"), SHA, "main", {".": "9.6.3"})


@pytest.mark.parametrize(
    "body",
    [
        (
            "## Release scope\n\nSDK `10.0.0-beta.21` from next.\n\n"
            "---\nGenerated with Release Please."
        ),
        "## [10.0.0-beta.21](https://example.invalid)\n### Features\n* Fix",
        ":robot: release\n---\n## [10.0.0-beta.20](https://example.invalid)\n---\nGenerated",
        ":robot: release\n---\n## [0.2.0-beta.4](https://example.invalid)\n---\nGenerated",
        (
            ":robot: release\n---\n## [10.0.0-beta.21](https://example.invalid)\n"
            "## [0.2.0-beta.4](https://example.invalid)\n---\nGenerated"
        ),
    ],
)
def test_release_pr_rejects_malformed_or_wrong_component_notes(body: str) -> None:
    pr = release_pr("adcp", "10.0.0-beta.21")
    pr["body"] = body
    with pytest.raises(ValueError):
        validate_release_pr(pr, SHA, "next", MANIFEST)


def test_release_pr_rejects_wrong_merge_commit() -> None:
    with pytest.raises(ValueError):
        validate_release_pr(release_pr("adcp", "10.0.0-beta.21"), "b" * 40, "next", MANIFEST)


def distribution(directory: Path, name: str, version: str) -> None:
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n".encode()
    with zipfile.ZipFile(directory / "candidate.whl", "w") as archive:
        archive.writestr("candidate.dist-info/METADATA", metadata)
    with tarfile.open(directory / "candidate.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("candidate/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))


@pytest.mark.parametrize(
    "package,tag,version",
    [
        ("adcp", "v10.0.0-beta.21", "10.0.0b21"),
        ("adcp-reporting-storage", "adcp-reporting-storage-v0.2.0-beta.4", "0.2.0b4"),
    ],
)
def test_publish_version_accepts_matching_distribution(
    tmp_path: Path, package: str, tag: str, version: str
) -> None:
    distribution(tmp_path, package, version)
    check_publish_version(tmp_path, package, tag)


def test_publish_version_rejects_bogus_storage_tag(tmp_path: Path) -> None:
    distribution(tmp_path, "adcp-reporting-storage", "0.2.0b4")
    with pytest.raises(ValueError):
        check_publish_version(
            tmp_path, "adcp-reporting-storage", "adcp-reporting-storage-v10.0.0-beta.21"
        )


@pytest.mark.parametrize(
    "package,tag", [("adcp", "v10.0.0-beta.20"), ("adcp-reporting-storage", "v0.2.0-beta.4")]
)
def test_publish_version_rejects_wrong_tag(tmp_path: Path, package: str, tag: str) -> None:
    version = "10.0.0b21" if package == "adcp" else "0.2.0b4"
    distribution(tmp_path, package, version)
    with pytest.raises(ValueError):
        check_publish_version(tmp_path, package, tag)


def test_release_workflows_guard_before_tagging_and_before_publication() -> None:
    for name in ("release-please-next.yml", "release-please.yml"):
        steps = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)[
            "jobs"
        ]["release"]["steps"]
        names = [step.get("name", "") for step in steps]
        guard = next(i for i, step in enumerate(steps) if "check_release_pr" in step.get("run", ""))
        assert guard < names.index(
            "Open or update the release PR, or create its release after merge"
        )

    steps = yaml.load(
        (ROOT / ".github/workflows/release-publish.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]["build"]["steps"]
    guard = next(
        i for i, step in enumerate(steps) if "check_publish_version" in step.get("run", "")
    )
    assert (
        next(i for i, step in enumerate(steps) if "python -m build" in step.get("run", "")) < guard
    )
    assert guard < next(
        i for i, step in enumerate(steps) if "upload-artifact" in step.get("uses", "")
    )
