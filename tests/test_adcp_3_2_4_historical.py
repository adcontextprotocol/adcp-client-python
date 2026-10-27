"""AdCP 3.2.4 stays an immutable, verifiable historical release after 3.2.7 adoption.

3.2.7 is the live default (see ``test_adcp_3_2_7_adoption``). The shared ``3.2`` schema
cache now holds the signed 3.2.7 bytes, so only the release-scoped, never-overwritten
assets are checked here: the audited pin, the packaged compliance provenance and its files.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

VERSION = "3.2.4"
SOURCE = "5e6c4b01113237234b440b6c95adb8c6e0a07ca9"
BUNDLE = "d5a0b167b323e5ee05ac2c8da9ca01c46a5cb5a7b988570f40f746bb8b15ae76"
IDENTITY = (
    "https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/3.2.x"
)
MANIFEST_FILE_COUNT = 4237
PROVENANCE_FILE_COUNT = 225
ROOT = files("adcp").joinpath("_compliance", VERSION)
PIN = Path(__file__).resolve().parents[1] / "schemas" / "releases" / f"{VERSION}.json"


def test_audited_pin_is_unchanged() -> None:
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    assert pin["version"] == VERSION
    assert pin["source_commit"] == SOURCE
    assert pin["certificate_identity"] == IDENTITY
    assert pin["bundle_url"] == f"https://adcontextprotocol.org/protocol/{VERSION}.tgz"
    assert pin["artifacts"][f"{VERSION}.tgz"]["sha256"] == BUNDLE


def test_packaged_compliance_provenance_and_bytes_are_unchanged() -> None:
    provenance = json.loads(ROOT.joinpath("provenance.json").read_bytes())
    assert provenance["release"]["source_commit"] == SOURCE
    assert provenance["release"]["artifacts"][f"{VERSION}.tgz"]["sha256"] == BUNDLE
    assert provenance["release"]["certificate_identity"] == IDENTITY
    manifest = json.loads(ROOT.joinpath("bundle-manifest.json").read_bytes())
    assert manifest["adcp_version"] == manifest["published_version"] == VERSION
    assert manifest["file_count"] == MANIFEST_FILE_COUNT
    assert len(provenance["files"]) == PROVENANCE_FILE_COUNT
    for relative, expected in provenance["files"].items():
        body = ROOT.joinpath(relative).read_bytes()
        assert len(body) == expected["bytes"], relative
        assert hashlib.sha256(body).hexdigest() == expected["sha256"], relative
