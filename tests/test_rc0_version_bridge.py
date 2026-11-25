"""Explicit compatibility bridge for buyers still pinned to AdCP 3.3-rc.0.

A seller on this wheel (packaged 3.3.0-rc.3) serves rc.0 buyers during a
lockstep move: rc.0 claims validate against the rc.3 contract and responses
echo ``"3.3-rc.0"`` so the buyer selects its own bundled response validator.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from adcp import ADCPClient
from adcp._version import get_supported_adcp_versions, resolve_adcp_version
from adcp.exceptions import ADCPTaskError, ConfigurationError
from adcp.reporting.service_subscriptions import advertise_subscriptions
from adcp.reporting.subscriptions import ReportingSubscriptionSupport, serves_subscriptions
from adcp.server import ADCPHandler, ToolContext, create_a2a_server, resolve_requested_adcp_version
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.mcp_tools import MCPToolSet, create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.types.core import AgentConfig, Protocol
from adcp.validation import ValidationHookConfig, get_validator, validate_request
from adcp.validation.envelope import UnsupportedVersionError, detect_wire_version

ALIAS = "3.3-rc.0"
TARGET = "3.3-rc.3"
REQUEST = {"buying_mode": "brief", "brief": "sports"}


class Seller(ADCPHandler[ToolContext]):
    advertised_tools = {"get_products"}

    def __init__(self) -> None:
        self.versions: list[str | None] = []

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        assert context is not None
        self.versions.append(context.resolved_adcp_version)
        return {"products": [], "cache_scope": "public"}


def _client(version: str) -> ADCPClient:
    return ADCPClient(
        AgentConfig(id="seller", agent_uri="http://localhost/mcp/", protocol=Protocol.MCP),
        adcp_version=version,
    )


# --- 1. resolution -----------------------------------------------------------


@pytest.mark.parametrize("version", [ALIAS, "3.3.0-rc.0"])
def test_rc0_claim_resolves_to_rc3(version: str) -> None:
    assert detect_wire_version({"adcp_version": version}) == TARGET
    assert resolve_requested_adcp_version({"adcp_version": version}) == TARGET
    assert resolve_adcp_version(version) == TARGET
    assert _client(version).get_adcp_version() == TARGET


# --- 2. advertising ----------------------------------------------------------


def test_alias_is_advertised_and_has_rc3_validators() -> None:
    assert ALIAS in get_supported_adcp_versions()
    assert ALIAS in capabilities_response(["media_buy"])["adcp"]["supported_versions"]
    for tool in ("create_media_buy", "get_media_buy_delivery", "get_products"):
        for direction in ("request", "sync"):
            aliased = get_validator(tool, direction, version=ALIAS)
            target = get_validator(tool, direction, version=TARGET)
            assert aliased is not None and target is not None, (tool, direction)
            assert aliased.schema == target.schema, (tool, direction)


# --- 3. MCP ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, [TARGET, ALIAS]])
@pytest.mark.parametrize(
    "validation", [None, ValidationHookConfig(requests="strict", responses="strict")]
)
async def test_mcp_serves_rc0_claim_and_echoes_it(selection: Any, validation: Any) -> None:
    seller = Seller()
    caller = create_tool_caller(
        seller, "get_products", validation=validation, supported_versions=selection
    )
    response = await caller({**REQUEST, "adcp_version": ALIAS})
    assert response["products"] == []
    assert response["adcp_version"] == ALIAS
    assert seller.versions == [TARGET]


@pytest.mark.asyncio
async def test_mcp_toolset_serves_rc0_claim() -> None:
    seller = Seller()
    tools = MCPToolSet(seller, supported_versions=[TARGET, ALIAS], advertise_all=True)
    response = await tools.call_tool("get_products", {**REQUEST, "adcp_version": ALIAS})
    assert response["adcp_version"] == ALIAS
    assert seller.versions == [TARGET]


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", [ALIAS, "3.3.0-rc.0"])
async def test_mcp_selection_excluding_alias_refuses_it(claim: str) -> None:
    seller = Seller()
    caller = create_tool_caller(seller, "get_products", supported_versions=[TARGET])
    with pytest.raises(ADCPTaskError) as caught:
        await caller({**REQUEST, "adcp_version": claim})
    assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
    assert caught.value.errors[0].details["supported_versions"] == [TARGET]
    assert seller.versions == []


def test_explicit_supported_set_can_exclude_alias() -> None:
    with pytest.raises(UnsupportedVersionError):
        detect_wire_version({"adcp_version": ALIAS}, supported=(TARGET,))


# --- 4. A2A ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, [TARGET, ALIAS]])
async def test_a2a_executor_serves_rc0_claim_and_echoes_it(selection: Any) -> None:
    seller = Seller()
    executor = ADCPAgentExecutor(seller, validation=None, supported_versions=selection)
    response = await executor._tool_callers["get_products"](
        {**REQUEST, "adcp_version": ALIAS}, ToolContext()
    )
    assert response["products"] == []
    assert response["adcp_version"] == ALIAS
    assert seller.versions == [TARGET]


@pytest.mark.asyncio
async def test_a2a_executor_selection_excluding_alias_refuses_it() -> None:
    seller = Seller()
    executor = ADCPAgentExecutor(seller, validation=None, supported_versions=[TARGET])
    with pytest.raises(ADCPTaskError) as caught:
        await executor._tool_callers["get_products"](
            {**REQUEST, "adcp_version": ALIAS}, ToolContext()
        )
    assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
    assert seller.versions == []


def _a2a_send(client: TestClient, params: dict[str, Any]) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [
                    {"kind": "data", "data": {"skill": "get_products", "parameters": params}}
                ],
            }
        },
    }
    response = client.post("/", json=body, headers={"accept": "application/json"})
    assert response.status_code == 200
    return response.text


def test_a2a_wire_serves_rc0_claim_and_the_response_carries_it() -> None:
    seller = Seller()
    app = create_a2a_server(seller, supported_versions=[TARGET, ALIAS], validation=None)
    with TestClient(app, base_url="http://localhost:3001") as client:
        text = _a2a_send(client, {**REQUEST, "adcp_version": ALIAS})
    assert "VERSION_UNSUPPORTED" not in text
    assert '"products"' in text
    assert f'"adcp_version":"{ALIAS}"' in text.replace(" ", "")
    assert seller.versions == [TARGET]


def test_a2a_wire_refuses_rc0_claim_when_alias_is_not_selected() -> None:
    seller = Seller()
    app = create_a2a_server(seller, supported_versions=[TARGET], validation=None)
    with TestClient(app, base_url="http://localhost:3001") as client:
        text = _a2a_send(client, {**REQUEST, "adcp_version": ALIAS})
    assert "VERSION_UNSUPPORTED" in text
    assert seller.versions == []


# --- 5. other prereleases stay refused ---------------------------------------


@pytest.mark.parametrize("version", ["3.3-rc.1", "3.3-rc.2", "3.3-beta.6"])
def test_bridge_does_not_accept_other_prereleases(version: str) -> None:
    with pytest.raises(UnsupportedVersionError):
        detect_wire_version({"adcp_version": version})
    with pytest.raises(ConfigurationError):
        _client(version)


# --- 6. known gap: rc.3 withdrew performance-standard metric_id (adcp#8854) --


_STANDARD = {
    "metric": "viewability",
    "threshold": 0.7,
    "standard": "mrc",
    "vendor": {"domain": "ias.example"},
}


def _buy_products(standard: dict[str, Any]) -> dict[str, Any]:
    return {
        "adcp_version": ALIAS,
        "idempotency_key": "k" * 16,
        "account": {"account_id": "acct_1"},
        "brand": {"domain": "brand.example"},
        "feed_version": "feed_1",
        "start_time": "2026-12-01T00:00:00Z",
        "end_time": "2026-12-31T00:00:00Z",
        "purchases": [
            {"product_id": "p1", "pricing_option_id": "po1", "performance_standards": [standard]}
        ],
    }


def test_buy_products_without_metric_id_validates_under_the_alias() -> None:
    assert validate_request("buy_products", _buy_products(_STANDARD), version=ALIAS).valid


def test_rc0_buy_products_metric_id_is_rejected_under_the_alias() -> None:
    """Known gap: the rc.3 compact standard is ``additionalProperties: false``."""
    outcome = validate_request(
        "buy_products", _buy_products({**_STANDARD, "metric_id": "viewable_rate"}), version=ALIAS
    )
    assert not outcome.valid
    assert any(
        issue.pointer == "/purchases/0/performance_standards/0"
        and issue.keyword == "additionalProperties"
        for issue in outcome.issues
    )


def test_create_media_buy_package_performance_standard_validates() -> None:
    request = {
        "adcp_version": ALIAS,
        "idempotency_key": "k" * 16,
        "account": {"account_id": "acct_1"},
        "brand": {"domain": "brand.example"},
        "start_time": "2026-12-01T00:00:00Z",
        "end_time": "2026-12-31T00:00:00Z",
        "packages": [
            {
                "product_id": "p1",
                "pricing_option_id": "po1",
                "budget": 100,
                "performance_standards": [_STANDARD],
            }
        ],
    }
    assert validate_request("create_media_buy", request, version=ALIAS).valid


# --- 7. reporting ------------------------------------------------------------


def test_reporting_subscription_advertisement_is_not_withheld_from_rc0() -> None:
    options = SimpleNamespace(engine=SimpleNamespace(support=ReportingSubscriptionSupport()))
    response = {"adcp": {"principal": {"supported_sections": []}}, "experimental_features": []}
    served = advertise_subscriptions(response, options, adcp_version=ALIAS)  # type: ignore[arg-type]
    assert served == advertise_subscriptions(response, options, adcp_version=TARGET)  # type: ignore[arg-type]
    assert "reporting_subscriptions" in served["adcp"]["principal"]["supported_sections"]
    assert not serves_subscriptions("3.2")
