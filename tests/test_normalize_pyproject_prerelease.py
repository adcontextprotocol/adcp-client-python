import base64
from pathlib import Path

import pytest

from scripts import normalize_pyproject_prerelease as normalizer
from scripts.normalize_pyproject_prerelease import (
    normalize_pyproject,
    normalize_pyproject_text,
    pep440_prerelease,
)


def test_pep440_prerelease_converts_supported_semver_labels() -> None:
    assert pep440_prerelease("7.0.0-alpha.1") == "7.0.0a1"
    assert pep440_prerelease("7.0.0-beta.2") == "7.0.0b2"
    assert pep440_prerelease("7.0.0-rc.3") == "7.0.0rc3"


def test_pep440_prerelease_leaves_other_versions_unchanged() -> None:
    assert pep440_prerelease("7.0.0") == "7.0.0"
    assert pep440_prerelease("7.0.0b1") == "7.0.0b1"
    assert pep440_prerelease("7.0.0-dev.1") == "7.0.0-dev.1"


def test_normalize_pyproject_text_changes_only_project_version_line() -> None:
    text = """\
[project]
name = "adcp"
version = "7.0.0-beta.1"

[tool.example]
version = "1.0.0-beta.1"
"""

    assert (
        normalize_pyproject_text(text)
        == """\
[project]
name = "adcp"
version = "7.0.0b1"

[tool.example]
version = "1.0.0-beta.1"
"""
    )


def test_normalize_pyproject_reports_whether_file_changed(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "6.1.0-beta.1"\n', encoding="utf-8")

    assert normalize_pyproject(pyproject) is True
    assert pyproject.read_text(encoding="utf-8") == '[project]\nversion = "6.1.0b1"\n'
    assert normalize_pyproject(pyproject) is False


def test_release_pr_normalization_updates_only_the_expected_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    def github(path: str, token: str, *, body: dict | None = None) -> dict:
        assert token == "app-token"
        calls.append((path, body))
        if path == "pulls/42":
            return {
                "base": {"ref": "main", "repo": {"full_name": normalizer._REPOSITORY}},
                "head": {
                    "ref": "release-please--branches--main",
                    "repo": {"full_name": normalizer._REPOSITORY},
                },
            }
        if path.startswith("contents/pyproject.toml?ref="):
            return {
                "content": base64.b64encode(b'[project]\nversion = "8.0.0-beta.16"\n').decode(),
                "sha": "blob",
            }
        return {}

    monkeypatch.setattr(normalizer, "_github", github)
    normalizer.normalize_release_prs([{"number": 42}], "app-token")
    assert calls[-1] == (
        "contents/pyproject.toml",
        {
            "message": "chore: normalize prerelease version to PEP 440",
            "content": base64.b64encode(b'[project]\nversion = "8.0.0b16"\n').decode(),
            "sha": "blob",
            "branch": "release-please--branches--main",
        },
    )


def test_release_pr_normalization_refuses_foreign_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    def github(path: str, token: str, *, body: dict | None = None) -> dict:
        return {
            "base": {"ref": "main", "repo": {"full_name": normalizer._REPOSITORY}},
            "head": {"ref": "release-please--branches--main", "repo": {"full_name": "other/repo"}},
        }

    monkeypatch.setattr(normalizer, "_github", github)
    with pytest.raises(ValueError, match="unexpected release PR identity"):
        normalizer.normalize_release_prs([{"number": 42}], "app-token")
