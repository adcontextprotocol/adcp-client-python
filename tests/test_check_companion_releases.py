"""The release gate requires a companion on PyPI that installs *and works* with the release."""

from __future__ import annotations

import subprocess
import sys
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from scripts import check_companion_releases as gate

_PROJECT = "https://pypi.org/pypi/adcp-reporting-storage/json"
_ADCP = "https://pypi.org/pypi/adcp/json"


def _release(requirement: str | None) -> dict[str, Any]:
    requires = ["psycopg>=3; extra == 'pg'"] + ([requirement] if requirement else [])
    return {"info": {"requires_dist": requires}}


def _fetch(
    releases: dict[str, tuple[str | None, bool]], adcp: dict[str, bool] | None = None
) -> gate.Fetch:
    data: dict[str, Any] = {
        _PROJECT: {
            "releases": {version: [{"yanked": yanked}] for version, (_, yanked) in releases.items()}
        },
        _ADCP: {
            "releases": {version: [{"yanked": yanked}] for version, yanked in (adcp or {}).items()}
        },
    }
    for version, (requirement, _) in releases.items():
        data[f"https://pypi.org/pypi/adcp-reporting-storage/{version}/json"] = _release(requirement)
    return lambda url: data.get(url)


def test_missing_companion_blocks_the_release() -> None:
    assert gate.main(["9.0.0"], fetch=lambda url: None) == 1


def test_compatible_companion_beta_allows_the_release() -> None:
    fetch = _fetch({"0.1.0b4": ("adcp>=9.0.0,<10", False)})
    assert gate.compatible_release("adcp-reporting-storage", Version("9.1.0"), fetch) == "0.1.0b4"
    assert gate.main(["9.1.0"], fetch=fetch) == 0


def test_a_new_adcp_major_needs_a_companion_that_accepts_it() -> None:
    fetch = _fetch({"0.1.0b4": ("adcp>=9.0.0,<10", False)})
    assert gate.main(["10.0.0"], fetch=fetch) == 1


def test_prerelease_adcp_versions_match_a_prerelease_floor() -> None:
    fetch = _fetch({"0.1.0b1": ("adcp>=9.0.0b4,<10", False)})
    assert gate.compatible_release("adcp-reporting-storage", Version("9.0.0b5"), fetch)


@pytest.mark.parametrize(
    "releases",
    [
        {"0.1.0b4": ("adcp>=9.0.0,<10", True)},
        {"0.1.0b4": (None, False)},
        {"0.1.0b4": ("adcp>=9.2.0,<10", False)},
    ],
    ids=["yanked", "no-adcp-requirement", "floor-above"],
)
def test_unusable_companion_releases_do_not_count(
    releases: dict[str, tuple[str | None, bool]],
) -> None:
    assert gate.main(["9.1.0"], fetch=_fetch(releases)) == 1


def test_newest_compatible_release_is_reported() -> None:
    fetch = _fetch(
        {
            "0.1.0b1": ("adcp>=9.0.0b4,<10", False),
            "0.2.0": ("adcp>=10,<11", False),
            "0.1.0b4": ("adcp>=9.0.0,<10", False),
        }
    )
    assert gate.compatible_release("adcp-reporting-storage", Version("9.1.0"), fetch) == "0.1.0b4"


# -- the built distribution must work with the published companion (#1763) ----


class _Runner:
    """A fake subprocess: records the commands, scripts the smoke's verdict."""

    def __init__(self, smoke_returncode: int = 0, install_returncode: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.smoke_returncode = smoke_returncode
        self.install_returncode = install_returncode

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[-1].endswith("companion_smoke.py"):
            return subprocess.CompletedProcess(
                command,
                self.smoke_returncode,
                stdout="smoke stdout\n",
                stderr="" if self.smoke_returncode == 0 else "storage contract 2 vs 1",
            )
        if "install" in command:
            return subprocess.CompletedProcess(command, self.install_returncode, "", "pip failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    def installed(self) -> list[str]:
        """The requirements of the install step, without the installer's own options."""
        for command in self.commands:
            if "install" not in command:
                continue
            arguments = iter(command[command.index("install") + 1 :])
            requirements = []
            for item in arguments:
                if item == "--python":
                    next(arguments)  # the interpreter path
                elif not item.startswith("-"):
                    requirements.append(item)
            return requirements
        return []

    def smoked(self) -> int:
        return sum(1 for command in self.commands if command[-1].endswith("companion_smoke.py"))


def test_the_range_compatible_companion_is_smoked_with_the_built_adcp(tmp_path: Path) -> None:
    dist = tmp_path / "adcp-10.0.0b6-py3-none-any.whl"
    dist.touch()
    fetch = _fetch({"0.2.0b1": ("adcp>=10.0.0b4,<11", False)})
    runner = _Runner()
    assert gate.main(["10.0.0b6", "--dist", str(dist)], fetch=fetch, run=runner) == 0
    assert runner.installed() == [str(dist), "adcp-reporting-storage[pg]==0.2.0b1"]
    assert runner.smoked() == 1
    assert all("-I" in command for command in runner.commands if command[-1].endswith(".py"))


def test_a_companion_that_installs_but_does_not_work_blocks_the_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #1763 pair: range-compatible, contract-incompatible."""
    dist = tmp_path / "adcp-10.0.0b4-py3-none-any.whl"
    dist.touch()
    fetch = _fetch({"0.2.0b0": ("adcp>=9.0.0,<11", False)})
    assert gate.main(["10.0.0b4", "--dist", str(dist)], fetch=fetch, run=_Runner(1)) == 1
    err = capsys.readouterr().err
    assert "storage contract 2 vs 1" in err
    assert "does not work with the published adcp-reporting-storage 0.2.0b0" in err


def test_a_smoke_environment_that_cannot_install_is_a_failing_gate(tmp_path: Path) -> None:
    dist = tmp_path / "adcp-10.0.0b6-py3-none-any.whl"
    dist.touch()
    fetch = _fetch({"0.2.0b1": ("adcp>=10.0.0b4,<11", False)})
    runner = _Runner(install_returncode=1)
    assert gate.main(["10.0.0b6", "--dist", str(dist)], fetch=fetch, run=runner) == 1
    assert runner.smoked() == 0


def test_without_a_built_distribution_only_the_range_is_checked() -> None:
    runner = _Runner(1)
    fetch = _fetch({"0.2.0b0": ("adcp>=9.0.0,<11", False)})
    assert gate.main(["10.0.0b5"], fetch=fetch, run=runner) == 0
    assert runner.commands == []


def _storage_wheel(path: Path, requirement: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(
            "adcp_reporting_storage-0.2.0b1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: adcp-reporting-storage\nVersion: 0.2.0b1\n"
            f"Requires-Dist: {requirement}\nRequires-Dist: psycopg>=3; extra == 'pg'\n",
        )
    return path


def test_the_built_companion_is_smoked_with_the_oldest_and_newest_accepted_adcp(
    tmp_path: Path,
) -> None:
    dist = _storage_wheel(
        tmp_path / "adcp_reporting_storage-0.2.0b1-py3-none-any.whl", "adcp<11,>=10.0.0b4"
    )
    assert gate.wheel_adcp_requirement(dist) == Requirement("adcp<11,>=10.0.0b4")
    fetch = _fetch(
        {}, adcp={"10.0.0b3": False, "10.0.0b4": False, "10.0.0b5": False, "9.4.0": False}
    )
    assert gate.accepted_adcp_releases(Requirement("adcp>=10.0.0b4,<11"), fetch) == [
        "10.0.0b4",
        "10.0.0b5",
    ]
    runner = _Runner()
    assert (
        gate.main(
            ["--package", "adcp-reporting-storage", "--dist", str(dist)], fetch=fetch, run=runner
        )
        == 0
    )
    assert runner.smoked() == 2
    installs = [c for c in runner.commands if "install" in c]
    assert [c[-2] for c in installs] == ["adcp==10.0.0b4", "adcp==10.0.0b5"]
    assert all(c[-1] == f"{dist}[pg]" for c in installs)


def test_a_companion_whose_floor_admits_an_adcp_it_fails_with_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _storage_wheel(
        tmp_path / "adcp_reporting_storage-0.2.0b1-py3-none-any.whl", "adcp>=9.0.0,<11"
    )
    fetch = _fetch({}, adcp={"9.3.1": False, "10.0.0b4": False})
    assert (
        gate.main(
            ["--package", "adcp-reporting-storage", "--dist", str(dist)],
            fetch=fetch,
            run=_Runner(1),
        )
        == 1
    )
    assert "Raise the floor" in capsys.readouterr().err


def test_a_companion_with_no_published_adcp_in_range_is_refused(tmp_path: Path) -> None:
    dist = _storage_wheel(
        tmp_path / "adcp_reporting_storage-0.3.0-py3-none-any.whl", "adcp>=11,<12"
    )
    fetch = _fetch({}, adcp={"10.0.0b5": False})
    assert (
        gate.main(
            ["--package", "adcp-reporting-storage", "--dist", str(dist)], fetch=fetch, run=_Runner()
        )
        == 1
    )


def test_the_companion_package_requires_a_built_distribution() -> None:
    with pytest.raises(SystemExit):
        gate.main(["--package", "adcp-reporting-storage"])
    with pytest.raises(SystemExit):
        gate.main(["--package", "adcp"])


@pytest.fixture
def smoke() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("companion_smoke", gate.SMOKE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declaring(name: str, value: int) -> Any:
    return types.SimpleNamespace(**{name: value})


def test_the_smoke_script_reads_declared_contracts(
    smoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "adcp.reporting.ledger.storage_contract",
        _declaring("REPORTING_STORAGE_CONTRACT", 2),
    )
    monkeypatch.setitem(
        sys.modules, "adcp_reporting_storage.contract", _declaring("STORAGE_CONTRACT", 2)
    )
    assert smoke.adcp_contract() == smoke.storage_contract() == 2
    smoke.check_contract()
    monkeypatch.setitem(
        sys.modules, "adcp_reporting_storage.contract", _declaring("STORAGE_CONTRACT", 3)
    )
    with pytest.raises(SystemExit, match="contract 2 but the storage implements contract 3"):
        smoke.check_contract()


def test_the_smoke_script_classifies_releases_without_a_declaration(
    smoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adcp 10.0.0b4/b5 by their column lists; storage 0.1.0b4/0.2.0b0 as contract 1."""
    from adcp.reporting.ledger import _record_rows

    monkeypatch.setitem(sys.modules, "adcp.reporting.ledger.storage_contract", None)
    monkeypatch.setitem(sys.modules, "adcp_reporting_storage.contract", None)
    assert smoke.storage_contract() == 1
    assert smoke.adcp_contract() == 2  # this checkout's columns carry offering_id
    with pytest.raises(
        SystemExit, match="storage contract 2 but the storage implements contract 1"
    ):
        smoke.check_contract()  # the #1763 pair
    monkeypatch.setattr(
        _record_rows,
        "_OBLIGATION_COLUMNS",
        _record_rows._OBLIGATION_COLUMNS.replace(", offering_id", ""),
    )
    assert smoke.adcp_contract() == 1  # 9.x and 10.0.0b0-b3
    smoke.check_contract()
