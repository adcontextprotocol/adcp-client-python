#!/usr/bin/env python3
"""Refuse to publish an ``adcp`` release its companion distributions can't install with.

9.0.0 moved the PostgreSQL reporting stores into ``adcp-reporting-storage``
before that package was on PyPI, so the documented
``pip install "adcp-reporting-storage[pg]"`` failed and Postgres adopters were
stuck on the last beta. Before an ``adcp`` release publishes, every companion
distribution must have a published, non-yanked release whose ``adcp``
requirement accepts the version being released.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

#: Distributions maintained in this repository that adopters install with adcp.
COMPANIONS = ("adcp-reporting-storage",)

Fetch = Callable[[str], Any]


def _pypi(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _adcp_requirement(requires_dist: list[str] | None) -> Requirement | None:
    for line in requires_dist or ():
        requirement = Requirement(line)
        if canonicalize_name(requirement.name) == "adcp" and requirement.marker is None:
            return requirement
    return None


def compatible_release(companion: str, adcp_version: Version, fetch: Fetch = _pypi) -> str | None:
    """Return the newest published ``companion`` release that installs with ``adcp_version``."""
    project = fetch(f"https://pypi.org/pypi/{companion}/json")
    if not project:
        return None
    candidates = []
    for raw, files in project.get("releases", {}).items():
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if files and not all(file.get("yanked") for file in files):
            candidates.append(version)
    for version in sorted(candidates, reverse=True):
        release = fetch(f"https://pypi.org/pypi/{companion}/{version}/json")
        requirement = _adcp_requirement((release or {}).get("info", {}).get("requires_dist"))
        if requirement is not None and requirement.specifier.contains(
            adcp_version, prereleases=True
        ):
            return str(version)
    return None


def main(argv: list[str] | None = None, fetch: Fetch = _pypi) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("adcp_version", help="the adcp version about to be published")
    args = parser.parse_args(argv)
    adcp_version = Version(args.adcp_version)
    missing = []
    for companion in COMPANIONS:
        found = compatible_release(companion, adcp_version, fetch)
        if found is None:
            missing.append(companion)
        else:
            print(f"{companion} {found} installs with adcp {adcp_version}")
    if missing:
        print(
            f"No published release of {', '.join(missing)} accepts adcp {adcp_version}. "
            "Publish a compatible companion release first.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
