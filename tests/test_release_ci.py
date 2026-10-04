"""Release metadata shortcuts require unchanged build inputs and real artifacts."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_scope
from scripts.ci.check_release_artifacts import check_artifacts
from tests.test_ci_scope import commit


@pytest.fixture
def release_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path
    for args in (
        ("init", "-q"),
        ("config", "user.name", "CI Fixture"),
        ("config", "user.email", "ci@example.invalid"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src/package.py").write_text("VALUE = 1\n")
    script = root / "scripts/ci/ci_scope.py"
    script.parent.mkdir(parents=True)
    script.write_text(Path(ci_scope.__file__).read_text())
    (root / "pyproject.toml").write_text('[project]\nname = "adcp"\nversion = "8.0.0"\n')
    (root / ".release-please-manifest.json").write_text('{".": "8.0.0"}\n')
    (root / "CHANGELOG.md").write_text("# Changelog\n")
    base = commit((root, ""))["pull_request"]["head"]["sha"]
    (root / "pyproject.toml").write_text('[project]\nname = "adcp"\nversion = "9.0.0"\n')
    (root / ".release-please-manifest.json").write_text('{".": "9.0.0"}\n')
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 9.0.0\n")
    return root, base


def release_event(repository: tuple[Path, str]) -> dict:
    event = commit(repository)
    pr = event["pull_request"]
    repo = {"full_name": "adcontextprotocol/adcp-client-python"}
    pr["base"].update(ref="main", repo=repo)
    pr["head"].update(ref="release-please--branches--main--components--adcp", repo=repo)
    pr["user"] = {"login": "aao-ipr-bot[bot]", "type": "Bot"}
    return event


def test_only_release_metadata_gets_targeted_checks(release_repository: tuple[Path, str]) -> None:
    root, _ = release_repository
    event = release_event(release_repository)
    assert ci_scope.select_scope("pull_request", event, cwd=root) == ci_scope.Scope(
        run_tests=True, release_metadata=True
    )
    for name in ("push", "workflow_dispatch", "merge_group"):
        assert ci_scope.select_scope(name, event, cwd=root) == ci_scope.Scope.full()


@pytest.mark.parametrize(
    "case",
    [
        "dependency",
        "build-setting",
        "runtime",
        "manifest-mismatch",
        "manifest-extra",
        "invalid-json",
        "duplicate-version",
        "prerelease",
        "downgrade",
        "executable",
        "symlink",
        "missing-changelog",
        "human",
        "other-bot",
        "fork",
        "other-branch",
        "other-base",
        "missing-author",
        "missing-history",
    ],
)
def test_any_other_change_requires_full_ci(release_repository: tuple[Path, str], case: str) -> None:
    root, _ = release_repository
    pyproject = root / "pyproject.toml"
    manifest = root / ".release-please-manifest.json"
    if case == "dependency":
        pyproject.write_text(pyproject.read_text() + 'dependencies = ["httpx>=99"]\n')
    elif case == "build-setting":
        pyproject.write_text(pyproject.read_text() + '[build-system]\nrequires = ["other"]\n')
    elif case == "runtime":
        (root / "src/package.py").write_text("VALUE = 2\n")
    elif case == "manifest-mismatch":
        manifest.write_text('{".": "9.1.0"}\n')
    elif case == "manifest-extra":
        manifest.write_text('{".": "9.0.0", "other": "9.0.0"}\n')
    elif case == "invalid-json":
        manifest.write_text("not-json\n")
    elif case == "duplicate-version":
        pyproject.write_text(pyproject.read_text() + 'version = "9.0.0"\n')
    elif case in {"prerelease", "downgrade"}:
        version = "9.0.0rc1" if case == "prerelease" else "7.0.0"
        pyproject.write_text(pyproject.read_text().replace("9.0.0", version))
        manifest.write_text(json.dumps({".": version}))
    elif case == "executable":
        pyproject.chmod(0o755)
    elif case == "symlink":
        (root / "CHANGELOG.md").unlink()
        (root / "CHANGELOG.md").symlink_to("src/package.py")
    elif case == "missing-changelog":
        (root / "CHANGELOG.md").write_text("# Changelog\n")
    event = release_event(release_repository)
    pr = event["pull_request"]
    if case == "human":
        pr["user"]["type"] = "User"
    elif case == "other-bot":
        pr["user"]["login"] = "unrelated[bot]"
    elif case == "fork":
        pr["head"]["repo"] = {"full_name": "other/adcp-client-python"}
    elif case == "other-branch":
        pr["head"]["ref"] = "my-release"
    elif case == "other-base":
        pr["base"]["ref"] = "other"
    elif case == "missing-author":
        pr["user"] = None
    elif case == "missing-history":
        pr["base"]["sha"] = "f" * 40
    assert ci_scope.select_scope("pull_request", event, cwd=root) == ci_scope.Scope.full()


def test_metadata_scope_reads_blobs_in_sparse_checkout(
    release_repository: tuple[Path, str], tmp_path: Path
) -> None:
    root, base = release_repository
    event = release_event(release_repository)
    head = event["pull_request"]["head"]["sha"]
    subprocess.run(["git", "checkout", "-qb", "integration", base], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--no-ff",
            "-qm",
            "merge",
            head,
        ],
        cwd=root,
        check=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--depth=2", "--no-checkout", root.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "sparse-checkout", "set", "--no-cone", "scripts/ci/ci_scope.py"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "-q", "HEAD"], cwd=clone, check=True)
    assert not (clone / "pyproject.toml").exists()
    assert ci_scope.select_scope("pull_request", event, cwd=clone).release_metadata


def test_version_line_inside_toml_string_is_not_project_metadata() -> None:
    text = """[project]
name = "adcp"
version = '8.0.0'
[tool.notes]
text = '''
[project]
version = "9.0.0"
'''
"""
    assert ci_scope.project_version(text) is None


@pytest.mark.parametrize("version", ["3.10", "3.11", "3.12", "3.13"])
@pytest.mark.parametrize("profile", ["full", "ordinary", "release"])
def test_actual_interpreter_step_selection(version: str, profile: str) -> None:
    workflow = yaml.load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    selected = []
    for step in workflow["jobs"]["test"]["steps"]:
        if step.get("name") not in {
            "Run full native suite",
            "Run canonical suite with coverage",
            "Run interpreter compatibility checks",
            "Validate release metadata and built distributions",
        }:
            continue
        condition = step["if"].replace("matrix.python-version", repr(version))
        condition = condition.replace(
            "needs.changes.outputs.full_matrix", repr(str(profile == "full").lower())
        )
        condition = condition.replace(
            "needs.changes.outputs.release_metadata", repr(str(profile == "release").lower())
        )
        if eval(condition.replace("&&", " and ").replace("||", " or "), {"__builtins__": {}}):
            selected.append(step["name"])
    if profile == "release":
        assert selected == ["Run interpreter compatibility checks"] + (
            ["Validate release metadata and built distributions"] if version == "3.12" else []
        )
    else:
        assert selected == [
            (
                "Run canonical suite with coverage"
                if version == "3.12"
                else (
                    "Run full native suite"
                    if profile == "full"
                    else "Run interpreter compatibility checks"
                )
            )
        ]


def artifacts(directory: Path, version: str, *, name: str = "adcp") -> None:
    data = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n".encode()
    with zipfile.ZipFile(directory / "candidate.whl", "w") as archive:
        archive.writestr("adcp-9.0.0.dist-info/METADATA", data)
    with tarfile.open(directory / "candidate.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("adcp-9.0.0/PKG-INFO")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


@pytest.mark.parametrize(
    "case", ["good", "wrong-version", "wrong-package", "missing", "extra-wheel"]
)
def test_artifact_metadata_must_match_candidate(tmp_path: Path, case: str) -> None:
    artifacts(
        tmp_path,
        "8.0.0" if case == "wrong-version" else "9.0.0",
        name="other" if case == "wrong-package" else "adcp",
    )
    if case == "missing":
        (tmp_path / "candidate.tar.gz").unlink()
    elif case == "extra-wheel":
        (tmp_path / "second.whl").write_bytes((tmp_path / "candidate.whl").read_bytes())
    if case == "good":
        check_artifacts(tmp_path, "9.0.0")
    else:
        with pytest.raises(ValueError):
            check_artifacts(tmp_path, "9.0.0")
