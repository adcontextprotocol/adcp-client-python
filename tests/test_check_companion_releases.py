"""The adcp release gate requires an installable companion on PyPI."""

from __future__ import annotations

from typing import Any

import pytest
from packaging.version import Version

from scripts import check_companion_releases as gate

_PROJECT = "https://pypi.org/pypi/adcp-reporting-storage/json"


def _release(requirement: str | None) -> dict[str, Any]:
    requires = ["psycopg>=3; extra == 'pg'"] + ([requirement] if requirement else [])
    return {"info": {"requires_dist": requires}}


def _fetch(releases: dict[str, tuple[str | None, bool]]) -> gate.Fetch:
    data: dict[str, Any] = {
        _PROJECT: {
            "releases": {version: [{"yanked": yanked}] for version, (_, yanked) in releases.items()}
        }
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
