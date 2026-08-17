"""CI gate: ``src/adcp/ADCP_VERSION`` and ``schemas/cache/index.json``
must pin the same AdCP spec version.

Without this test, a seller can bump ``ADCP_VERSION`` from ``"latest"``
to ``"3.0.0"`` (or vice versa) and forget to re-run
``make regenerate-schemas``. The package then ships with generated
Pydantic types from the *wrong* spec version — validation passes
locally because nothing cross-checks, but downstream consumers hit
subtle schema mismatches in production.

Failure mode this test prevents:

1. Developer edits ``ADCP_VERSION`` (e.g. pinning for a stable
   release).
2. CI green — nothing ran the regen.
3. Release ships.
4. Clients upgrade to the new SDK version, hit types missing fields
   or flagged as unexpected because the schemas were generated
   against a different spec revision.

The check is cheap (two file reads, one string comparison) and
catches the exact release-day footgun:
``git log --oneline src/adcp/ADCP_VERSION`` shows a bump;
``git log --oneline schemas/cache/`` doesn't. Test fails → regen
was skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adcp.validation.version import resolve_bundle_key

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADCP_VERSION_FILE = _REPO_ROOT / "src" / "adcp" / "ADCP_VERSION"
_BUNDLE_KEY = resolve_bundle_key(_ADCP_VERSION_FILE.read_text().strip())


def test_pinned_schema_bundle_is_in_distribution_manifests() -> None:
    """The current prerelease schema must ship in both wheels and sdists."""
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    manifest = (_REPO_ROOT / "MANIFEST.in").read_text()

    assert f'"_schemas/{_BUNDLE_KEY}/**/*.json"' in pyproject
    assert f"recursive-include src/adcp/_schemas/{_BUNDLE_KEY} *.json" in manifest


_CACHE_INDEX = _REPO_ROOT / "schemas" / "cache" / _BUNDLE_KEY / "index.json"


def test_adcp_version_file_exists() -> None:
    """Paranoia: the pinning file must exist. A missing file is the
    same drift class — the SDK doesn't know what spec it targets."""
    assert _ADCP_VERSION_FILE.exists(), (
        f"Missing {_ADCP_VERSION_FILE.relative_to(_REPO_ROOT)}. "
        f"This file pins the AdCP spec version the SDK was generated "
        f"against; its absence means nothing downstream can verify spec "
        f"alignment."
    )


def test_schemas_cache_index_exists() -> None:
    """Paranoia: the schema cache index must exist. Same drift class —
    without the index, we can't verify what spec the types were
    generated from."""
    assert _CACHE_INDEX.exists(), (
        f"Missing {_CACHE_INDEX.relative_to(_REPO_ROOT)}. "
        f"This file is written by ``make regenerate-schemas`` and records "
        f"the spec version the generated types were built against."
    )


def test_adcp_version_matches_schemas_cache() -> None:
    """The pinned spec version must match what the generated schemas
    were built against. If this test fails, someone changed
    ``ADCP_VERSION`` without running ``make regenerate-schemas``
    (or vice versa).

    Fix: run ``make regenerate-schemas`` at the repo root, then commit
    the updated ``schemas/cache/`` + ``src/adcp/types/generated_poc/``.
    """
    pinned = _ADCP_VERSION_FILE.read_text().strip()
    cache_data = json.loads(_CACHE_INDEX.read_text())
    cache_version = cache_data.get("adcp_version", "<missing>")

    assert pinned == cache_version, (
        f"Spec version drift:\n"
        f"  {_ADCP_VERSION_FILE.relative_to(_REPO_ROOT)}: {pinned!r}\n"
        f"  {_CACHE_INDEX.relative_to(_REPO_ROOT)}.adcp_version: "
        f"{cache_version!r}\n"
        f"\n"
        f"These must agree. If you bumped ADCP_VERSION intentionally, "
        f"run `make regenerate-schemas` to refresh the generated types "
        f"and commit the result."
    )


@pytest.mark.parametrize(
    "bad_value",
    ["", " ", "\n", "\t"],
)
def test_adcp_version_file_not_blank(bad_value: str) -> None:
    """Pinning to a blank string would make the check pass trivially
    (empty == empty). Guard against that class of regression at the
    file-read level."""
    pinned = _ADCP_VERSION_FILE.read_text().strip()
    assert pinned != bad_value.strip(), (
        f"{_ADCP_VERSION_FILE.relative_to(_REPO_ROOT)} is blank. "
        f"Set it to a concrete version like 'latest' or '3.0.0'."
    )
