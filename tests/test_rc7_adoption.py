"""The signed rc.7 release is the live default; older bundles remain offline."""

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

VERSION = "3.2.0-rc.7"
SOURCE = "4ca13ae5cb65dff40aa514619f677293616522cd"
BUNDLE = "942b24c66500839b3db21e74f69f2fe34d6ac18f898acc1f67aa126e35547994"
SUMMARY_SHA = "b72eabbc00b70c1993d53140a2427ac3208e224107c66659f5f25545fb2314e0"
ROOT = files("adcp").joinpath("_compliance", VERSION)


def test_current_pin_and_historical_roots():
    assert files("adcp").joinpath("ADCP_VERSION").read_text().strip() == VERSION
    assert resolve_adcp_version(None) == resolve_adcp_version(VERSION) == "3.2-rc.7"
    assert set(get_supported_adcp_versions()) == {"3.0", "3.1", "3.2-rc.7"}
    for previous in ("3.2.0-rc.3", "3.2.0-rc.6"):
        assert schema_loader._resolve_schema_root(previous) is not None
        with pytest.raises(ConfigurationError):
            resolve_adcp_version(previous)


def test_signed_release_and_package_bytes():
    provenance = json.loads(ROOT.joinpath("provenance.json").read_bytes())
    assert provenance["release"]["source_commit"] == SOURCE
    assert provenance["release"]["artifacts"][VERSION + ".tgz"]["sha256"] == BUNDLE
    assert provenance["release"]["certificate_identity"] == (
        "https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/main"
    )
    manifest = json.loads(ROOT.joinpath("bundle-manifest.json").read_bytes())
    assert manifest["adcp_version"] == manifest["published_version"] == VERSION
    assert manifest["file_count"] == 4131
    assert len(provenance["files"]) == 143
    assert len(provenance["schemas"]) == 1620
    for relative, expected in provenance["files"].items():
        body = ROOT.joinpath(relative).read_bytes()
        assert len(body) == expected["bytes"], relative
        assert hashlib.sha256(body).hexdigest() == expected["sha256"], relative
    schema_root = schema_loader._resolve_schema_root(VERSION)
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
    assert hashlib.sha256(summary_bytes).hexdigest() == SUMMARY_SHA
    summary = json.loads(summary_bytes)
    assert len(summary["cases"]) == 22
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


def test_default_reporting_mount_uses_rc7_status_schema():
    async def resolve_account(reference, context, consumer):
        return reference["account_id"]

    handler = ReportingReceiptHandler(InMemoryReportingFeedStore(), resolve_account=resolve_account)
    assert handler.get_adcp_version() == "3.2-rc.7"
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
    for operation in signed["cases"][20]["patch"]:
        parts = operation["path"].split("/")[1:]
        parent = old_periods
        for part in parts[:-1]:
            parent = parent[part]
        if operation["op"] == "remove":
            del parent[parts[-1]]
        else:
            parent[parts[-1]] = operation["value"]
    assert not mounted.is_valid(old_periods)
