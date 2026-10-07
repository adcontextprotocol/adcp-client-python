"""The signed AdCP 3.2.3 release is the live default and retains historical bundles.

Release identity values below come from the signed 3.2.3 release and its
``schemas/releases/3.2.3.json`` pin. Until they are filled in, every test that
depends on them fails loudly instead of passing vacuously.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from importlib.resources import files

import pytest
from jsonschema.validators import validator_for
from pydantic import TypeAdapter

from adcp._version import get_supported_adcp_versions, resolve_adcp_version
from adcp.exceptions import ConfigurationError
from adcp.reporting.feed import InMemoryReportingFeedStore
from adcp.reporting.receipts import ReportingReceiptHandler
from adcp.server.mcp_tools import MCPToolSet
from adcp.types import OptimizationGoal
from adcp.validation import schema_loader

VERSION = "3.2.3"
WIRE_VERSION = "3.2"
_TODO = "TODO: fill from the signed 3.2.3 release"
# v3.2.3 tag commit (schemas/releases/3.2.3.json source_commit).
SOURCE = "dfc6ad602780c1cff4fe23e16e30a570900c89d7"
# SHA-256 of https://adcontextprotocol.org/protocol/3.2.3.tgz.
BUNDLE = "cad841ddef4617fdb6eda130be7b471d9028123489b1384a7ca843ea97fd4ba5"
# SHA-256 of test-vectors/reporting-summary/complete-summary.json.
SUMMARY_SHA = "b72eabbc00b70c1993d53140a2427ac3208e224107c66659f5f25545fb2314e0"
# Counts from _compliance/3.2.3 after the pinned sync.
MANIFEST_FILE_COUNT: int | None = 4215
PROVENANCE_FILE_COUNT: int | None = 203
PROVENANCE_SCHEMA_COUNT: int | None = 1625
SUMMARY_CASE_COUNT: int | None = 22
ROOT = files("adcp").joinpath("_compliance", VERSION)


def _pinned(value):
    """Fail loudly while a release identity value is still a placeholder."""
    if value is None or (isinstance(value, str) and value.startswith("TODO")):
        pytest.fail(f"3.2.3 release identity placeholder not filled in: {value!r}")
    return value


def test_current_pin_and_historical_roots():
    assert files("adcp").joinpath("ADCP_VERSION").read_text().strip() == VERSION
    assert resolve_adcp_version(None) == resolve_adcp_version(VERSION) == WIRE_VERSION
    assert resolve_adcp_version(WIRE_VERSION) == WIRE_VERSION
    assert set(get_supported_adcp_versions()) == {"3.0", "3.1", WIRE_VERSION, "3.2-rc.7"}
    # Installed wheels retain every supported stable root and the exact rc.7
    # wire alias. Full archival prerelease caches are checkout-only assets.
    for supported in get_supported_adcp_versions():
        assert schema_loader._resolve_schema_root(resolve_adcp_version(supported)) is not None
    for previous in ("3.2.0-rc.3", "3.2.0-rc.6", "3.2.0-rc.7"):
        if previous == "3.2.0-rc.7":
            assert resolve_adcp_version(previous) == WIRE_VERSION
            assert schema_loader._resolve_schema_root(resolve_adcp_version(previous)) is not None
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


def test_default_reporting_mount_uses_current_status_schema():
    async def resolve_account(reference, context, consumer):
        return reference["account_id"]

    handler = ReportingReceiptHandler(InMemoryReportingFeedStore(), resolve_account=resolve_account)
    assert handler.get_adcp_version() == WIRE_VERSION
    tools = MCPToolSet(handler)
    output = next(
        tool["outputSchema"]
        for tool in tools.tool_definitions
        if tool["name"] == "get_reporting_status"
    )
    signed = json.loads(
        ROOT.joinpath("test-vectors/reporting-summary/complete-summary.json").read_bytes()
    )
    mounted = validator_for(output)(output)
    assert mounted.is_valid(signed["response"])
    old_periods = deepcopy(signed["response"])
    old_periods_case = next(
        case
        for case in signed["cases"]
        if case["name"] == "complete periods view retains its existing field restriction"
    )
    for operation in old_periods_case["patch"]:
        parts = operation["path"].split("/")[1:]
        parent = old_periods
        for part in parts[:-1]:
            parent = parent[part]
        if operation["op"] == "remove":
            del parent[parts[-1]]
        else:
            parent[parts[-1]] = operation["value"]
    assert not mounted.is_valid(old_periods)


def test_a2a_operation_resolution_vectors_are_packaged():
    vectors = ROOT.joinpath("test-vectors/request-signing/a2a")
    assert sum(path.name.endswith(".json") for path in vectors.joinpath("positive").iterdir()) == 7
    assert sum(path.name.endswith(".json") for path in vectors.joinpath("negative").iterdir()) == 26
    assert ROOT.joinpath("test-vectors/request-signing/README.md").is_file()
