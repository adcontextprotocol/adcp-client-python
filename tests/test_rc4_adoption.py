"""Signed rc.4 inputs are available and usable from installed, offline artifacts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files

import pytest
from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from pydantic import TypeAdapter

from adcp._version import get_supported_adcp_versions, resolve_adcp_version
from adcp.types import GetAdcpCapabilitiesResponse, SyncAccountsAccount, SyncAccountsResponse
from adcp.validation import schema_loader
from tests.test_schema_datetime_formats import INVALID, VALID, request_with_timestamp

VERSION = "3.2.0-rc.4"
SOURCE = "94976657c8456e5ad6de55d9793a883542a4fc5f"
BUNDLE = "773bee016d279345d6fae91a0ce684a22ca9b81c2edd2cdb9a71dbfea65954d2"
SUMMARY_SHA = "b72eabbc00b70c1993d53140a2427ac3208e224107c66659f5f25545fb2314e0"
ROOT = files("adcp").joinpath("_compliance", VERSION)
SUMMARY = json.loads(
    ROOT.joinpath("test-vectors/reporting-summary/complete-summary.json").read_bytes()
)
SCHEMAS = (
    "media-buy/get-reporting-status-response.json",
    "bundled/media-buy/get-reporting-status-response.json",
    "mcp/2026-07-28/profiles/production/media-buy/get-reporting-status-response.json",
)


@lru_cache
def named(relative, version=VERSION):
    validator = schema_loader.get_named_validator(relative, version=version)
    assert validator is not None
    # The SDK registers its RFC3339 checker, including on the Python floor.
    checker = FormatChecker()
    checker.checks("date-time")(schema_loader._is_rfc3339_date_time)
    if relative.startswith("mcp/"):
        return validator_for(validator.schema)(
            validator.schema, resolver=validator.resolver, format_checker=checker
        )
    return validator.evolve(format_checker=checker)


def patched(value, operations):
    value = deepcopy(value)
    for operation in operations:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in operation["path"].split("/")[1:]]
        parent = value
        for part in parts[:-1]:
            parent = parent[part]
        if operation["op"] == "remove":
            del parent[parts[-1]]
        else:
            assert operation["op"] in {"add", "replace"}
            parent[parts[-1]] = deepcopy(operation["value"])
    return value


@pytest.mark.parametrize("relative", SCHEMAS)
@pytest.mark.parametrize("case", SUMMARY["cases"], ids=lambda case: case["name"])
def test_all_22_signed_summary_vectors_on_authoritative_schemas(relative, case):
    assert len(SUMMARY["cases"]) == 22
    validator = named(relative)
    document = deepcopy(validator.schema)
    assert (
        schema_loader._effective_task_schema(
            document, "get_reporting_status", "sync", bundle_key=VERSION
        )
        == document
    )
    payload = patched(SUMMARY["response"], case["patch"])
    assert validator.is_valid(payload) is case["valid"], list(validator.iter_errors(payload))
    assert validator.schema == document


def test_packaged_release_schema_and_fixture_provenance_is_complete():
    assert files("adcp").joinpath("ADCP_VERSION").read_text().strip() == VERSION
    assert resolve_adcp_version(None) == "3.2-rc.4"
    assert resolve_adcp_version("3.2.0-rc.3") == "3.2-rc.3"
    assert set(get_supported_adcp_versions()) == {"3.0", "3.1", "3.2-rc.3", "3.2-rc.4"}
    provenance = json.loads(ROOT.joinpath("provenance.json").read_bytes())
    assert provenance["release"]["source_commit"] == SOURCE
    assert provenance["release"]["artifacts"][VERSION + ".tgz"]["sha256"] == BUNDLE
    assert (
        provenance["release"]["certificate_identity"]
        == "https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/main"
    )
    manifest = json.loads(ROOT.joinpath("bundle-manifest.json").read_bytes())
    assert manifest["adcp_version"] == manifest["published_version"] == VERSION
    assert manifest["file_count"] == 4111
    for relative, expected in provenance["files"].items():
        raw = ROOT.joinpath(relative).read_bytes()
        assert len(raw) == expected["bytes"]
        assert hashlib.sha256(raw).hexdigest() == expected["sha256"], relative
    assert len(provenance["files"]) == 143
    summary = ROOT.joinpath("test-vectors/reporting-summary/complete-summary.json").read_bytes()
    assert hashlib.sha256(summary).hexdigest() == SUMMARY_SHA
    assert sum(n.startswith("test-vectors/request-signing/") for n in provenance["files"]) == 48
    assert sum(n.startswith("test-vectors/webhook-signing/") for n in provenance["files"]) == 31
    assert (
        sum(n.startswith("test-vectors/reporting-reconciliation/") for n in provenance["files"])
        == 20
    )
    schema_root = schema_loader._resolve_schema_root(VERSION)
    assert schema_root is not None
    assert len(provenance["schemas"]) == 1613
    for relative, expected in provenance["schemas"].items():
        raw = (schema_root.root / relative).read_bytes()
        assert len(raw) == expected["cache_bytes"], relative
        assert hashlib.sha256(raw).hexdigest() == expected["cache_sha256"], relative
    assert (
        provenance["schemas"][SCHEMAS[0]]["signed_sha256"]
        != provenance["schemas"][SCHEMAS[0]]["cache_sha256"]
    )


def test_historical_v32_models_keep_their_beta6_identity_offline():
    from adcp.types.v32 import ListCreativesRequest, PackageRequest

    assert ListCreativesRequest.schema_version == "3.2-beta.6"
    assert PackageRequest.schema_version == "3.2-beta.6"
    properties = ListCreativesRequest.model_json_schema()["properties"]
    assert {"assignment_projection", "assignment_limit"} <= properties.keys()
    request = ListCreativesRequest(
        assignment_projection="matching",
        assignment_limit=1,
        filters={"indicator_types": ["creative_fatigue"]},
    )
    assert ListCreativesRequest.model_validate_json(request.model_dump_json()) == request
    package = PackageRequest(product_id="product-1", pricing_option_id="fixed")
    assert package.budget is None
    assert package.model_dump() == {
        "product_id": "product-1",
        "pricing_option_id": "fixed",
        "paused": False,
    }


@pytest.mark.parametrize(
    "relative,digest",
    [
        (
            "creative/list-creatives-request.json",
            "a09fc7e7bd499622294a76b572543fbe92042cca39a93f4743403bc74add01dd",
        ),
        (
            "media-buy/package-request.json",
            "6fc291a77693094e07cf3b1f6e75ace06f76be4789e4c2d760bfa4797635509b",
        ),
        (
            "bundled/creative/list-creatives-request.json",
            "2e9642c3d62141f04ed5d13de7914908c122de83b606d81c68113f0add9a3dc2",
        ),
        (
            "bundled/media-buy/package-request.json",
            "43ff964d9b03f1a9f2bad876f97cd722e655d950da88cf3b8ac86dfaa2d9e949",
        ),
    ],
)
def test_historical_v32_schema_bytes_match_accepted_b24(relative, digest):
    # Historical input is the accepted e3a44d28 tree, not the signed rc.4 bundle.
    root = schema_loader._resolve_schema_root("3.2.0-beta.6")
    assert root is not None
    assert hashlib.sha256((root.root / relative).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    "relative",
    [
        "account/sync-accounts-response.json",
        "mcp/2026-07-28/profiles/production/account/sync-accounts-response.json",
    ],
)
@pytest.mark.parametrize(
    "account,valid",
    [
        ({"action": "updated", "account": {"account_id": "account-a"}, "status": "active"}, True),
        (
            {
                "action": "failed",
                "account": {"account_id": "account-a"},
                "errors": [{"code": "UNAUTHORIZED", "message": "Unavailable"}],
            },
            True,
        ),
        (
            {
                "action": "created",
                "brand": {"domain": "example.test"},
                "operator": "buyer.test",
                "status": "active",
            },
            True,
        ),
        ({"action": "updated", "status": "active"}, False),
        ({"action": "updated", "account": {"account_id": "account-a"}}, False),
        (
            {
                "action": "updated",
                "account": {"account_id": "account-a"},
                "brand": {"domain": "example.test"},
                "operator": "buyer.test",
                "status": "active",
            },
            False,
        ),
        ({"action": "failed", "account": {"account_id": "account-a"}}, False),
        ({"action": "failed", "account": {"account_id": "account-a"}, "errors": []}, False),
    ],
)
def test_account_result_identity_status_and_error_branches(relative, account, valid):
    response = {"status": "completed", "accounts": [account]}
    assert named(relative).is_valid(response) is valid
    if valid:
        model = TypeAdapter(SyncAccountsResponse).validate_python(response)
        for wire in (model.model_dump(mode="json"), json.loads(model.model_dump_json())):
            named(relative).validate(wire)
            assert TypeAdapter(SyncAccountsResponse).validate_python(wire) == model
        result = SyncAccountsAccount.model_validate(account)
        if "account" in account:
            assert result.brand is None and result.operator is None
            if account["action"] == "failed":
                assert result.status is None


@pytest.mark.parametrize(
    "relative",
    [
        "protocol/get-adcp-capabilities-response.json",
        "bundled/protocol/get-adcp-capabilities-response.json",
    ],
)
@pytest.mark.parametrize("anonymous", [False, True])
@pytest.mark.parametrize("account_required", [False, True])
def test_anonymous_discovery_rejects_conflicting_account_requirement(
    relative, anonymous, account_required
):
    from adcp.server.responses import capabilities_response

    response = capabilities_response(["media_buy", "signals"], idempotency={"supported": False})
    response["media_buy"]["anonymous_discovery"] = anonymous
    response["signals"] = {"anonymous_discovery": anonymous}
    response["account"] = {
        "supported_billing": ["operator"],
        "required_for_products": account_required,
    }
    assert named(relative).is_valid(response) is not (anonymous and account_required)
    model = GetAdcpCapabilitiesResponse.model_validate(response)
    assert model.media_buy.anonymous_discovery is anonymous
    assert model.signals.anonymous_discovery is anonymous


@pytest.mark.parametrize(
    "vector",
    json.loads(ROOT.joinpath("test-vectors/error-recovery/vectors.json").read_bytes())["vectors"],
    ids=lambda vector: vector["id"],
)
def test_official_error_recovery_vectors_schema_contract(vector):
    # These assert the advertised schema/decoding contract. Retry scheduling is
    # a separate runtime contract, not inferred from structural vector success.
    assert named("core/error.json").is_valid(vector["error"]) is vector["expected"]["schema_valid"]


@pytest.mark.parametrize("version", ["3.2-rc.3", "3.2-rc.4"])
def test_reporting_handler_constructor_selects_the_public_mount_pin(version):
    from adcp.reporting.feed import InMemoryReportingFeedStore
    from adcp.reporting.receipts import ReportingReceiptHandler
    from adcp.server.mcp_tools import MCPToolSet

    async def resolve_account(reference, context, consumer):
        return reference["account_id"]

    handler = ReportingReceiptHandler(
        InMemoryReportingFeedStore(), resolve_account=resolve_account, adcp_version=version
    )
    assert handler.get_adcp_version() == version
    tools = MCPToolSet(handler)
    output = next(
        t["outputSchema"] for t in tools.tool_definitions if t["name"] == "get_reporting_status"
    )
    validator = validator_for(output)(output, format_checker=named(SCHEMAS[0]).format_checker)
    old_periods = patched(SUMMARY["response"], SUMMARY["cases"][20]["patch"])
    assert validator.is_valid(old_periods) is (version == "3.2-rc.3")


@pytest.mark.parametrize("version", ["3.0", "3.1"])
@pytest.mark.parametrize("kind", ["receipt", "status"])
def test_reporting_mount_rejects_releases_without_reporting_schemas(version, kind):
    from adcp.exceptions import ConfigurationError
    from adcp.reporting.feed import InMemoryReportingFeedStore
    from adcp.reporting.ledger.status import ReportingStatusHandler
    from adcp.reporting.ledger.status_server import ReportingStatusNotificationHandler
    from adcp.reporting.receipts import ReportingReceiptHandler

    store = InMemoryReportingFeedStore()

    async def resolve(*args):
        raise AssertionError("construction must not authorize a request")

    # General client support for these stable versions remains intact.
    assert resolve_adcp_version(version) == version
    assert schema_loader.get_validator("get_reporting_status", "sync", version=version) is None
    with pytest.raises(ConfigurationError, match="reporting"):
        if kind == "receipt":
            ReportingReceiptHandler(store, resolve_account=resolve, adcp_version=version)
        else:
            ReportingStatusNotificationHandler(
                ReportingStatusHandler(store), resolve_caller=resolve, adcp_version=version
            )


@pytest.mark.parametrize("value", VALID + INVALID)
def test_current_public_timestamp_contract_on_the_installed_runtime(value):
    from adcp.validation.schema_validator import validate_request

    valid = isinstance(value, str) and value in VALID
    validator = named("core/reporting-delivery-config-state.json")
    field = validator.evolve(schema=validator.schema["properties"]["activated_at"])
    assert field.is_valid(value) is valid
    outcome = validate_request("create_media_buy", request_with_timestamp(value), version=VERSION)
    assert outcome.valid is valid
