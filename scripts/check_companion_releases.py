#!/usr/bin/env python3
"""Refuse to publish a distribution its companion can't actually work with.

9.0.0 moved the PostgreSQL reporting stores into ``adcp-reporting-storage``
before that package was on PyPI, so the documented
``pip install "adcp-reporting-storage[pg]"`` failed and Postgres adopters were
stuck on the last beta. Then adcp 10.0.0b4 changed the ledger column lists the
storage package reads against its DDL, and the published storage release
(``adcp>=9.0.0,<11``) still *installed* beside it and failed every reporting
turn (adcp-client-python#1763). Range compatibility is not enough.

Before an ``adcp`` release publishes, every companion distribution must have a
published, non-yanked release whose ``adcp`` requirement accepts the version
being released (``compatible_release``). With ``--dist``, the gate goes
further and proves the pair works: it installs the built distribution and the
published companion into a private environment and runs
``scripts/companion_smoke.py`` there, which compares the storage contract both
sides declare and, when ``ADCP_PG_TEST_URL`` is set, creates the ledger schema
and round-trips records on PostgreSQL. ``--package adcp-reporting-storage``
gates the companion in the other direction: the built storage distribution is
smoked with both the oldest and the newest published ``adcp`` its own
requirement accepts.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from email.parser import HeaderParser
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

#: Distributions maintained in this repository that adopters install with adcp.
COMPANIONS = ("adcp-reporting-storage",)
#: The extra that installs the PostgreSQL driver the smoke needs.
STORAGE_EXTRA = "pg"
SMOKE = Path(__file__).with_name("companion_smoke.py")

Fetch = Callable[[str], Any]
Run = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _pypi(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, timeout=900, env=environment, check=False
    )


def _adcp_requirement(requires_dist: list[str] | None) -> Requirement | None:
    for line in requires_dist or ():
        requirement = Requirement(line)
        if canonicalize_name(requirement.name) == "adcp" and requirement.marker is None:
            return requirement
    return None


def _published_versions(project: Any) -> list[Version]:
    """Non-yanked releases of a PyPI project, newest first."""
    candidates = []
    for raw, files in (project or {}).get("releases", {}).items():
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if files and not all(file.get("yanked") for file in files):
            candidates.append(version)
    return sorted(candidates, reverse=True)


def compatible_release(companion: str, adcp_version: Version, fetch: Fetch = _pypi) -> str | None:
    """Return the newest published ``companion`` release that installs with ``adcp_version``."""
    project = fetch(f"https://pypi.org/pypi/{companion}/json")
    if not project:
        return None
    for version in _published_versions(project):
        release = fetch(f"https://pypi.org/pypi/{companion}/{version}/json")
        requirement = _adcp_requirement((release or {}).get("info", {}).get("requires_dist"))
        if requirement is not None and requirement.specifier.contains(
            adcp_version, prereleases=True
        ):
            return str(version)
    return None


def accepted_adcp_releases(requirement: Requirement, fetch: Fetch = _pypi) -> list[str]:
    """Published, non-yanked ``adcp`` releases the requirement accepts, oldest first."""
    project = fetch("https://pypi.org/pypi/adcp/json")
    accepted = [
        version
        for version in _published_versions(project)
        if requirement.specifier.contains(version, prereleases=True)
    ]
    return [str(version) for version in sorted(accepted)]


def wheel_adcp_requirement(distribution: Path) -> Requirement:
    """The ``adcp`` requirement a built companion wheel declares."""
    with zipfile.ZipFile(distribution) as wheel:
        metadata = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
        headers = HeaderParser().parsestr(wheel.read(metadata).decode("utf-8"))
    requirement = _adcp_requirement(headers.get_all("Requires-Dist"))
    if requirement is None:
        raise SystemExit(f"{distribution.name} declares no adcp requirement")
    return requirement


def smoke(requirements: list[str], *, label: str, run: Run = _run) -> bool:
    """Install ``requirements`` into a private environment and run the companion smoke."""
    with tempfile.TemporaryDirectory(prefix="adcp-companion-") as root:
        environment = Path(root) / "env"
        if shutil.which("uv"):
            create = ["uv", "venv", "--quiet", "--python", sys.executable, str(environment)]
            python = environment / "bin" / "python"
            install = ["uv", "pip", "install", "--quiet", "--python", str(python)]
        else:
            create = [sys.executable, "-m", "venv", str(environment)]
            python = environment / "bin" / "python"
            install = [str(python), "-m", "pip", "install", "--quiet"]
        for step, command in (("create", create), ("install", [*install, *requirements])):
            completed = run(command)
            if completed.returncode != 0:
                print(f"{label}: could not {step} the smoke environment", file=sys.stderr)
                print(completed.stderr[-4000:], file=sys.stderr)
                return False
        script = Path(root) / SMOKE.name
        shutil.copy2(SMOKE, script)
        completed = run([str(python), "-I", str(script)])
        sys.stdout.write(completed.stdout)
        if completed.returncode != 0:
            print(f"{label}: {completed.stderr.strip()}", file=sys.stderr)
            return False
        return True


def gate_adcp(
    adcp_version: Version, distribution: Path | None, fetch: Fetch = _pypi, run: Run = _run
) -> int:
    """An adcp release needs a published companion that installs with it and works with it."""
    missing = []
    failed = []
    for companion in COMPANIONS:
        found = compatible_release(companion, adcp_version, fetch)
        if found is None:
            missing.append(companion)
            continue
        print(f"{companion} {found} installs with adcp {adcp_version}")
        if distribution is not None and not smoke(
            [str(distribution), f"{companion}[{STORAGE_EXTRA}]=={found}"],
            label=f"adcp {adcp_version} + {companion} {found}",
            run=run,
        ):
            failed.append(f"{companion} {found}")
    if missing:
        print(
            f"No published release of {', '.join(missing)} accepts adcp {adcp_version}. "
            "Publish a compatible companion release first.",
            file=sys.stderr,
        )
        return 1
    if failed:
        print(
            f"adcp {adcp_version} does not work with the published {', '.join(failed)}. "
            "Publish a companion release that matches this adcp's storage contract first.",
            file=sys.stderr,
        )
        return 1
    return 0


def gate_companion(distribution: Path, fetch: Fetch = _pypi, run: Run = _run) -> int:
    """A companion release must work with the oldest and newest adcp its range accepts."""
    requirement = wheel_adcp_requirement(distribution)
    accepted = accepted_adcp_releases(requirement, fetch)
    if not accepted:
        print(
            f"No published adcp release satisfies {requirement}; publish adcp first.",
            file=sys.stderr,
        )
        return 1
    failed = []
    for adcp_version in sorted({accepted[0], accepted[-1]}, key=Version):
        print(f"{distribution.name} ({requirement}) accepts the published adcp {adcp_version}")
        if not smoke(
            [f"adcp=={adcp_version}", f"{distribution}[{STORAGE_EXTRA}]"],
            label=f"{distribution.name} + adcp {adcp_version}",
            run=run,
        ):
            failed.append(adcp_version)
    if failed:
        print(
            f"{distribution.name} does not work with the published adcp {', '.join(failed)} its "
            "requirement accepts. Raise the floor to the first adcp of its storage contract.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None, fetch: Fetch = _pypi, run: Run = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "adcp_version", nargs="?", help="the adcp version about to be published (package adcp)"
    )
    parser.add_argument(
        "--package",
        choices=("adcp", *COMPANIONS),
        default="adcp",
        help="the distribution about to be published",
    )
    parser.add_argument(
        "--dist",
        type=Path,
        help="the built wheel of that distribution; when given, the pair is installed and smoked",
    )
    args = parser.parse_args(argv)
    if args.package == "adcp":
        if not args.adcp_version:
            parser.error("the adcp version is required for --package adcp")
        return gate_adcp(Version(args.adcp_version), args.dist, fetch, run)
    if args.dist is None:
        parser.error(f"--dist is required for --package {args.package}")
    return gate_companion(args.dist, fetch, run)


if __name__ == "__main__":
    raise SystemExit(main())
