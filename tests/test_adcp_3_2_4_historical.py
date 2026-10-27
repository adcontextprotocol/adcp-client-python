"""AdCP 3.2.4 stays the installed, verifiable 3.2 contract after 3.3.0-beta.0 adoption.

3.3.0-beta.0 is the live default (see ``test_adcp_3_3_0_beta_0_adoption``). 3.2.4 is
still synced as a preview bundle under the ``3.2`` cache key, so negotiation and
translation with 3.2 peers run on its signed bytes; the audited pin, the packaged
compliance provenance and its files are release-scoped and never overwritten.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from importlib.resources import files

import pytest
from pydantic import TypeAdapter

from adcp._version import get_supported_adcp_versions, resolve_adcp_version
from adcp.exceptions import ConfigurationError
from adcp.types import OptimizationGoal
from adcp.validation import schema_loader

VERSION = "3.2.4"
WIRE_VERSION = "3.2"
_TODO = "TODO: fill from the signed 3.2.4 release"
# v3.2.4 tag commit (schemas/releases/3.2.4.json source_commit).
SOURCE = "5e6c4b01113237234b440b6c95adb8c6e0a07ca9"
# SHA-256 of https://adcontextprotocol.org/protocol/3.2.4.tgz.
BUNDLE = "d5a0b167b323e5ee05ac2c8da9ca01c46a5cb5a7b988570f40f746bb8b15ae76"
# SHA-256 of test-vectors/reporting-summary/complete-summary.json.
SUMMARY_SHA = "b72eabbc00b70c1993d53140a2427ac3208e224107c66659f5f25545fb2314e0"
# Counts from _compliance/3.2.4 after the pinned sync.
MANIFEST_FILE_COUNT: int | None = 4237
PROVENANCE_FILE_COUNT: int | None = 225
PROVENANCE_SCHEMA_COUNT: int | None = 1625
SUMMARY_CASE_COUNT: int | None = 22
ROOT = files("adcp").joinpath("_compliance", VERSION)


def _pinned(value):
    """Fail loudly while a release identity value is still a placeholder."""
    if value is None or (isinstance(value, str) and value.startswith("TODO")):
        pytest.fail(f"3.2.4 release identity placeholder not filled in: {value!r}")
    return value


def test_historical_pin_and_3_2_roots():
    assert files("adcp").joinpath("ADCP_VERSION").read_text().strip() != VERSION
    assert resolve_adcp_version(VERSION) == resolve_adcp_version(WIRE_VERSION) == WIRE_VERSION
    assert WIRE_VERSION in get_supported_adcp_versions()
    assert schema_loader._resolve_schema_root(WIRE_VERSION) is not None
    for previous in ("3.2.0-rc.3", "3.2.0-rc.6", "3.2.0-rc.7"):
        assert schema_loader._resolve_schema_root(previous) is not None
        if previous == "3.2.0-rc.7":
            assert resolve_adcp_version(previous) == WIRE_VERSION
        else:
            with pytest.raises(ConfigurationError):
                resolve_adcp_version(previous)


def test_signed_release_and_package_bytes():
    provenance = json.loads(ROOT.joinpath("provenance.json").read_bytes())
    assert provenance["release"]["source_commit"] == _pinned(SOURCE)
    assert provenance["release"]["artifacts"][VERSION + ".tgz"]["sha256"] == _pinned(BUNDLE)
    assert provenance["release"]["certificate_identity"] == (
        "https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/3.2.x"
    )
    manifest = json.loads(ROOT.joinpath("bundle-manifest.json").read_bytes())
    assert manifest["adcp_version"] == manifest["published_version"] == VERSION
    assert manifest["file_count"] == _pinned(MANIFEST_FILE_COUNT)
    assert len(provenance["files"]) == _pinned(PROVENANCE_FILE_COUNT)
    assert len(provenance["schemas"]) == _pinned(PROVENANCE_SCHEMA_COUNT)
    for relative, expected in provenance["files"].items():
        body = ROOT.joinpath(relative).read_bytes()
        assert len(body) == expected["bytes"], relative
        assert hashlib.sha256(body).hexdigest() == expected["sha256"], relative
    # Stable bundles cache under their wire key ("3.2"), not the release name.
    schema_root = schema_loader._resolve_schema_root(WIRE_VERSION)
    assert schema_root is not None
    for relative, expected in provenance["schemas"].items():
        body = (schema_root.root / relative).read_bytes()
        assert len(body) == expected["cache_bytes"], relative
        assert hashlib.sha256(body).hexdigest() == expected["cache_sha256"], relative


@pytest.mark.parametrize(
    "relative",
    [
        "media-buy/get-reporting-status-response.json",
        "bundled/media-buy/get-reporting-status-response.json",
        "mcp/2026-07-28/profiles/production/media-buy/get-reporting-status-response.json",
    ],
)
def test_signed_reporting_summary_vectors_on_live_schema(relative):
    summary_bytes = ROOT.joinpath(
        "test-vectors/reporting-summary/complete-summary.json"
    ).read_bytes()
    assert hashlib.sha256(summary_bytes).hexdigest() == _pinned(SUMMARY_SHA)
    summary = json.loads(summary_bytes)
    assert len(summary["cases"]) == _pinned(SUMMARY_CASE_COUNT)
    validator = schema_loader.get_named_validator(relative, version=VERSION)
    assert validator is not None
    for case in summary["cases"]:
        payload = deepcopy(summary["response"])
        for operation in case["patch"]:
            parts = [
                part.replace("~1", "/").replace("~0", "~")
                for part in operation["path"].split("/")[1:]
            ]
            parent = payload
            for part in parts[:-1]:
                parent = parent[part]
            if operation["op"] == "remove":
                del parent[parts[-1]]
            else:
                assert operation["op"] in {"add", "replace"}
                parent[parts[-1]] = deepcopy(operation["value"])
        assert validator.is_valid(payload) is case["valid"], (relative, case["name"])


def test_viewable_rate_requires_a_standard_and_bounded_rate():
    validator = schema_loader.get_named_validator("core/optimization-goal.json", version=VERSION)
    assert validator is not None
    goal = {
        "kind": "metric",
        "metric": "viewable_rate",
        "standard": "mrc",
        "target": {"kind": "threshold_rate", "value": 0.7},
    }
    validator.validate(goal)
    assert (
        TypeAdapter(OptimizationGoal)
        .validate_python(goal)
        .model_dump(mode="json", exclude_none=True)
        == goal
    )
    for changed in (
        {**goal, "standard": None},
        {key: value for key, value in goal.items() if key != "standard"},
        {**goal, "target": {"kind": "threshold_rate", "value": 1.1}},
        {**goal, "metric": "clicks"},
    ):
        assert not validator.is_valid(changed)
