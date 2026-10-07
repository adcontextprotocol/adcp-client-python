"""Explicit compatibility bridge for clients shipped with adcp 8.0.0rc3."""

from typing import Any

import pytest

from adcp import ADCPClient
from adcp._version import get_supported_adcp_versions
from adcp.exceptions import ConfigurationError
from adcp.server import ADCPHandler, ToolContext, resolve_requested_adcp_version
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.types.core import AgentConfig, Protocol
from adcp.validation import (
    ValidationHookConfig,
    get_bundle_adcp_version,
    get_validator,
    validate_request,
)
from adcp.validation.envelope import UnsupportedVersionError, detect_wire_version


@pytest.mark.parametrize("version", ["3.2-rc.7", "3.2.0-rc.7"])
def test_rc7_wire_claim_resolves_to_stable(version: str) -> None:
    assert detect_wire_version({"adcp_version": version}) == "3.2"
    assert resolve_requested_adcp_version({"adcp_version": version}) == "3.2"
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="http://localhost/mcp/", protocol=Protocol.MCP),
        adcp_version=version,
    )
    assert client.get_adcp_version() == "3.2"


def test_alias_is_advertised_and_has_stable_validators() -> None:
    assert "3.2-rc.7" in get_supported_adcp_versions()
    assert "3.2-rc.7" in capabilities_response(["media_buy"])["adcp"]["supported_versions"]
    for direction in ("request", "sync"):
        aliased = get_validator("get_products", direction, version="3.2-rc.7")
        stable = get_validator("get_products", direction, version="3.2")
        assert aliased is not None and stable is not None
        assert aliased.schema == stable.schema
    assert get_bundle_adcp_version(version="3.2-rc.7") == "3.2.1"
    assert get_bundle_adcp_version(version="3.2.0-rc.7") == "3.2.0-rc.7"


@pytest.mark.parametrize("version", ["3.2-rc.6", "3.2-rc.8", "3.1-beta.5"])
def test_bridge_does_not_accept_other_prereleases(version: str) -> None:
    with pytest.raises(UnsupportedVersionError):
        detect_wire_version({"adcp_version": version})
    with pytest.raises(ConfigurationError):
        ADCPClient(
            AgentConfig(id="seller", agent_uri="http://localhost/mcp/", protocol=Protocol.MCP),
            adcp_version=version,
        )


def test_explicit_supported_set_can_exclude_alias() -> None:
    with pytest.raises(UnsupportedVersionError):
        detect_wire_version({"adcp_version": "3.2-rc.7"}, supported=("3.2",))


@pytest.mark.asyncio
async def test_rc7_request_dispatches_with_stable_validation_and_response() -> None:
    class Seller(ADCPHandler):
        async def get_products(self, params: dict[str, Any], context: ToolContext):
            assert context.resolved_adcp_version == "3.2"
            return {"products": [], "cache_scope": "public"}

    request = {"adcp_version": "3.2-rc.7", "buying_mode": "brief", "brief": "video"}
    assert validate_request("get_products", request, version="3.2-rc.7").valid
    caller = create_tool_caller(
        Seller(),
        "get_products",
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )
    response = await caller(request)
    assert response["products"] == []
    assert response["adcp_version"] == "3.2-rc.7"
    # The rc3 wheel chooses its response validator from this envelope and
    # bundles only the prerelease schema, so its strict validation must work.
    old_client_validator = get_validator("get_products", "sync", version="3.2.0-rc.7")
    assert old_client_validator is not None
    assert not list(old_client_validator.iter_errors(response))
