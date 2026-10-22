"""Regression coverage for application intent crossing negotiated boundaries."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from adcp import ADCPClient
from adcp.compat.versions import CONVERSION_METADATA_KEY
from adcp.exceptions import ADCPError
from adcp.server.responses import capabilities_response
from adcp.types import (
    CreateMediaBuyRequest,
    GetAdcpCapabilitiesResponse,
    GetProductsRequest,
    ListProductsRequest,
    SyncAccountsRequest,
    UpdateMediaBuyRequest,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.types.domains.enums.task_status import TaskStatus as GeneratedTaskStatus


def _client(**kwargs):
    return ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp/", protocol=Protocol.MCP),
        account_policy=kwargs.pop("account_policy", "off"),
        **kwargs,
    )


def _capabilities(version):
    declaration = capabilities_response(
        ["media_buy"], adcp_version=version, supported_versions=["3.0", version]
    )
    declaration["adcp"]["idempotency"] = {"supported": False}
    if version == "3.1":
        declaration["media_buy"] = {"features": {"canonical_creatives": False}}
    return GetAdcpCapabilitiesResponse.model_validate(declaration)


def _completed(data):
    return TaskResult(status=TaskStatus.COMPLETED, success=True, data=data)


def _discovery(**kwargs):
    return GetProductsRequest.model_validate({"buying_mode": "brief", "brief": "video", **kwargs})


@pytest.mark.parametrize("account_policy", ["off", "auto", "strict"])
async def test_automatic_discovery_transport_failure_retains_failed_result(account_policy):
    client = _client(account_policy=account_policy)
    failure = TaskResult(
        status=TaskStatus.FAILED,
        success=False,
        error="HTTP 403: token cannot discover this seller",
        adcp_error={"code": "UNAUTHORIZED", "message": "Discovery denied"},
        metadata={"http_status": 403},
    )
    wire = AsyncMock(return_value=failure)
    client.adapter._call_mcp_tool_wire = wire

    result = await client.get_products(
        _discovery(account={"brand": {"domain": "brand.example"}, "operator": "buyer.example"})
    )

    assert not result.success
    assert result.error == failure.error
    assert result.adcp_error == failure.adcp_error
    assert result.metadata["http_status"] == 403
    assert [call.args[0] for call in wire.call_args_list] == ["get_adcp_capabilities"]
    with pytest.raises(ADCPError, match="Failed to fetch capabilities"):
        await client.fetch_capabilities()


async def test_invalid_v3_discovery_is_a_failed_task_and_explicit_fetch_still_raises():
    client = _client()
    wire = AsyncMock(
        return_value=_completed(
            {
                "adcp": {"major_versions": [3]},
                "supported_protocols": ["media_buy"],
                "media_buy": "invalid capability block",
            }
        )
    )
    client.adapter._call_mcp_tool_wire = wire

    result = await client.get_products(_discovery())

    assert not result.success
    assert "Failed to parse response" in result.error
    assert all(call.args[0] == "get_adcp_capabilities" for call in wire.call_args_list)
    with pytest.raises(ADCPError, match="v3 capabilities response"):
        await client.fetch_capabilities()


@pytest.mark.parametrize("protocol", [Protocol.MCP, Protocol.A2A])
@pytest.mark.parametrize("case", ["no_common_version", "new_task", "null_clear"])
async def test_version_preflight_failure_is_a_failed_result_without_dispatch(protocol, case):
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example", protocol=protocol),
        server_version="3.1",
        adcp_version="3.0" if case == "no_common_version" else "3.2",
        account_policy="off",
    )
    wire = AsyncMock()
    if protocol is Protocol.MCP:
        client.adapter._call_mcp_tool_wire = wire
    else:
        client.adapter._call_a2a_tool_wire = wire

    if case == "no_common_version":
        result = await client.get_products(_discovery())
    elif case == "new_task":
        result = await client.list_products(ListProductsRequest())
    else:
        result = await client.update_media_buy(
            UpdateMediaBuyRequest.model_validate(
                {
                    "account": {"account_id": "account-one"},
                    "media_buy_id": "buy-one",
                    "idempotency_key": "unsupported-null-clear-1551",
                    "frequency_cap": None,
                }
            )
        )

    assert not result.success
    assert result.status == TaskStatus.FAILED
    assert result.data is None
    assert result.adcp_error["code"] == "UNSUPPORTED_FEATURE"
    assert result.adcp_error["details"]["wire_response_received"] is False
    if case == "null_clear":
        assert result.adcp_error["details"]["pointer"] == "/frequency_cap"
        assert result.idempotency_key == "unsupported-null-clear-1551"
    wire.assert_not_awaited()


async def test_per_call_newer_ceiling_preserves_explicit_null_clear():
    client = _client(adcp_version="3.1")
    client.fetch_capabilities = AsyncMock(return_value=_capabilities("3.2"))
    wire = AsyncMock(return_value=TaskResult(status=TaskStatus.FAILED, success=False))
    client.adapter._call_mcp_tool_wire = wire

    await client.update_media_buy(
        UpdateMediaBuyRequest.model_validate(
            {
                "adcp_version": "3.2",
                "idempotency_key": "per-call-null-clear-1551",
                "account": {"account_id": "account-one"},
                "media_buy_id": "buy-one",
                "frequency_cap": None,
            }
        )
    )

    params = wire.call_args.args[1]
    assert params["adcp_version"] == "3.2"
    assert "frequency_cap" in params
    assert params["frequency_cap"] is None


async def test_historical_prerelease_success_keeps_its_existing_response_path():
    client = _client(server_version="3.2.0-beta.5")
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=_completed({"status": "completed", "products": [], "cache_scope": "public"})
    )

    result = await client.get_products(_discovery())

    assert result.success, result.error
    assert result.data.products == []
    params = client.adapter._call_mcp_tool_wire.call_args.args[1]
    assert result.metadata[CONVERSION_METADATA_KEY]["wire_version"] == params["adcp_version"]


async def test_cold_legacy_discovery_converts_inline_creative_without_package_selector():
    legacy_ref = {"agent_url": "https://formats.example/mcp", "id": "display_300x250_image"}
    client = _client(canonical_format_legacy_resolver=lambda _: [legacy_ref])

    async def respond(tool, params):
        if tool == "get_adcp_capabilities":
            return _completed(_capabilities("3.1").model_dump(mode="json", exclude_none=True))
        return TaskResult(status=TaskStatus.FAILED, success=False)

    wire = AsyncMock(side_effect=respond)
    client.adapter._call_mcp_tool_wire = wire
    assert client.capabilities is None

    await client.create_media_buy(
        CreateMediaBuyRequest.model_validate(
            {
                "idempotency_key": "cold-creative-conversion-1551",
                "account": {"account_id": "account-one"},
                "brand": {"domain": "brand.example"},
                "start_time": "2026-11-01T00:00:00Z",
                "end_time": "2026-11-30T00:00:00Z",
                "packages": [
                    {
                        "product_id": "product-one",
                        "pricing_option_id": "price-one",
                        "budget": 1000,
                        "creatives": [
                            {
                                "creative_id": "creative-one",
                                "name": "Inline image",
                                "format_kind": "image",
                                "assets": {"headline": {"asset_type": "text", "content": "Hello"}},
                            }
                        ],
                    }
                ],
            }
        )
    )

    params = wire.call_args.args[1]
    package = params["packages"][0]
    assert params["adcp_version"] == "3.1"
    assert "format_ids" not in package
    assert package["creatives"][0]["format_id"] == legacy_ref
    assert "format_kind" not in package["creatives"][0]


async def test_response_parse_failure_keeps_request_loss_metadata():
    client = _client(server_version="3.1")
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=_completed(
            {"products": "invalid", "status": "completed", "cache_scope": "public"}
        )
    )

    result = await client.get_products(
        _discovery(brand={"domain": "brand.example", "countries": ["US"]})
    )

    assert not result.success
    assert "parse" in result.error.lower()
    report = result.metadata[CONVERSION_METADATA_KEY]
    assert report["wire_version"] == "3.1"
    assert any(loss["pointer"] == "/brand/countries" for loss in report["losses"])


async def test_per_call_ceiling_cannot_exceed_configured_peer_version():
    client = _client(server_version="3.1")
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=_completed({"products": [], "status": "completed", "cache_scope": "public"})
    )

    result = await client.get_products(_discovery(adcp_version="3.2"))

    assert result.success, result.error
    assert client.adapter._call_mcp_tool_wire.call_args.args[1]["adcp_version"] == "3.1"
    assert result.metadata[CONVERSION_METADATA_KEY]["wire_version"] == "3.1"


async def test_seller_upgrade_retains_provisioned_account_address_at_client_boundary():
    key = {
        "brand": {"domain": "brand.example", "countries": ["US"]},
        "operator": "agency.example",
        "operator_unit": {"id": "seat-one"},
        "currency": "USD",
        "timezone": "UTC",
    }
    reduced = {"brand": {"domain": "brand.example"}, "operator": "agency.example"}
    client = _client()
    client.fetch_capabilities = AsyncMock(return_value=_capabilities("3.1"))
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=_completed(
            {
                "status": "completed",
                "accounts": [
                    {
                        **deepcopy(reduced),
                        "account_id": "seller-account",
                        "action": "created",
                        "status": "active",
                    }
                ],
            }
        )
    )
    provisioned = await client.sync_accounts(
        SyncAccountsRequest.model_validate(
            {
                "idempotency_key": "account-upgrade-1551",
                "accounts": [{**key, "billing": "operator"}],
            }
        )
    )
    assert provisioned.success, provisioned.error
    assert (await client.accounts.get(key)).wire_version == "3.1"
    client.fetch_capabilities = AsyncMock(return_value=_capabilities("3.2"))
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=_completed({"products": [], "status": "completed", "cache_scope": "account"})
    )

    result = await client.get_products(_discovery(account=key))

    assert result.success, result.error
    params = client.adapter._call_mcp_tool_wire.call_args.args[1]
    assert params["adcp_version"] == "3.2"
    assert params["account"] == {**reduced, "sandbox": False}
    assert result.metadata[CONVERSION_METADATA_KEY]["account_identity_migration_pending"] == [
        {"pointer": "/account", "provisioned_wire_version": "3.1", "wire_version": "3.2"}
    ]


def test_known_legacy_webhook_upgrades_product_envelope_without_public_cache_assumption():
    client = _client(server_version="3.0", webhook_secret="webhook-secret-for-test-1551")

    result = client._parse_webhook_result(
        task_id="task-one",
        task_type="get_products",
        operation_id="operation-one",
        status=GeneratedTaskStatus.completed,
        result={"products": []},
        timestamp="2026-10-22T00:00:00Z",
        message=None,
        context_id=None,
    )

    assert result.success, result.error
    assert result.data.status == "completed"
    assert result.data.cache_scope == "account"
    assert result.data.adcp_version == "3.2"
