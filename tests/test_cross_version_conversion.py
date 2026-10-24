"""Application shapes stay current while client/server wire versions vary."""

import asyncio
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp import ADCPClient
from adcp.canonical_formats import CreativeDialect
from adcp.compat.versions import (
    CONVERSION_METADATA_KEY,
    ADCPVersionConversionError,
    convert_payload,
    negotiate_version,
)
from adcp.exceptions import ADCPFeatureUnsupportedError
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.types import GetAdcpCapabilitiesResponse, GetProductsRequest, SyncAccountsRequest
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.validation import ValidationHookConfig, validate_request, validate_response


def test_highest_common_version_and_call_ceiling() -> None:
    assert negotiate_version(["3.0", "3.1"], maximum="3.2") == "3.1"
    assert negotiate_version(["3.2", "3.0", "3.1"], maximum="3.1") == "3.1"
    assert negotiate_version(["3.2-rc.7"], maximum="3.2") == "3.2-rc.7"
    assert negotiate_version(["3.2"], maximum="3.2-rc.7") == "3.2"
    assert negotiate_version(["3.2", "3.2-rc.7"], maximum="3.2") == "3.2"
    with pytest.raises(ADCPFeatureUnsupportedError):
        negotiate_version(["2.5", "3.3"], maximum="3.2")


@pytest.mark.parametrize("target", ["3.1", "3.0"])
def test_account_keys_reduce_with_complete_loss_records(target: str) -> None:
    request = {
        "idempotency_key": "cross-version-key-1234",
        "accounts": [
            {
                "brand": {"domain": "example.com", "brand_id": "example", "countries": ["US"]},
                "operator": "agency.example",
                "operator_unit": {"id": "seat_1", "name": "One"},
                "currency": "USD",
                "timezone": "America/New_York",
                "billing": "operator",
            }
        ],
        "context": {"countries": ["US"], "operator_unit": "opaque"},
        "ext": {"vendor": {"countries": ["US"]}},
    }
    original = deepcopy(request)
    converted = convert_payload(
        "sync_accounts",
        request,
        direction="request",
        source_version="3.2",
        target_version=target,
    )
    assert request == original
    assert converted.payload["accounts"] == [
        {
            "brand": {"domain": "example.com", "brand_id": "example"},
            "operator": "agency.example",
            "billing": "operator",
        }
    ]
    assert converted.payload["context"] == original["context"]
    assert converted.payload["ext"] == original["ext"]
    assert {loss.pointer for loss in converted.losses} >= {
        "/accounts/0/brand/countries",
        "/accounts/0/operator_unit",
        "/accounts/0/currency",
        "/accounts/0/timezone",
    }
    assert all("value" not in record for record in converted.metadata())
    assert validate_request("sync_accounts", converted.payload, version=target).valid


def test_nested_account_reference_union_and_extensions() -> None:
    request = {
        "buying_mode": "brief",
        "brief": "video",
        "account": {
            "brand": {"domain": "example.com", "countries": ["US"]},
            "operator": "agency.example",
            "operator_unit": {"id": "seat_1"},
            "currency": "USD",
            "timezone": "UTC",
        },
        "vendor_unknown": {"currency": "do not edit"},
    }
    result = convert_payload(
        "get_products",
        request,
        direction="request",
        source_version="3.2",
        target_version="3.1",
    )
    assert result.payload["account"] == {
        "brand": {"domain": "example.com"},
        "operator": "agency.example",
    }
    assert result.payload["vendor_unknown"] == request["vendor_unknown"]
    assert len(result.losses) == 4


def test_new_task_fails_before_an_unrepresentable_old_call() -> None:
    with pytest.raises(ADCPFeatureUnsupportedError, match="list_products"):
        convert_payload(
            "list_products", {}, direction="request", source_version="3.2", target_version="3.0"
        )


def test_response_upgrade_and_downgrade_chain() -> None:
    old = {"products": [], "context": {"account": "opaque"}}
    upgraded = convert_payload(
        "get_products",
        old,
        direction="sync",
        source_version="3.0",
        target_version="3.2",
        request={},
    )
    assert upgraded.payload["status"] == "completed"
    assert upgraded.payload["cache_scope"] == "public"
    assert upgraded.payload["adcp_version"] == "3.2"
    reduced = convert_payload(
        "get_products",
        upgraded.payload,
        direction="sync",
        source_version="3.2",
        target_version="3.0",
    )
    assert reduced.payload == old
    assert {loss.pointer for loss in reduced.losses} >= {"/status", "/cache_scope"}


@pytest.mark.parametrize("tool", ["create_media_buy", "update_media_buy"])
def test_old_media_buy_success_without_lifecycle_status_gets_task_status(tool: str) -> None:
    old = {
        "media_buy_id": "mb_1",
        "packages": [],
        "revision": 1,
        "confirmed_at": "2026-10-22T00:00:00Z",
    }
    assert validate_response(tool, old, version="3.0").valid

    upgraded = convert_payload(
        tool, old, direction="sync", source_version="3.0", target_version="3.2"
    )

    assert upgraded.payload["status"] == "completed"
    assert "media_buy_status" not in upgraded.payload
    assert validate_response(tool, upgraded.payload, version="3.2").valid


def test_response_alias_is_normalized_without_rewriting_the_request_alias() -> None:
    payload = {"adcp_version": "3.2-rc.7"}
    response = convert_payload(
        "get_products", payload, direction="sync", source_version="3.2-rc.7", target_version="3.2"
    )
    request = convert_payload(
        "get_products",
        payload,
        direction="request",
        source_version="3.2",
        target_version="3.2-rc.7",
    )
    assert response.payload["adcp_version"] == "3.2"
    assert request.payload["adcp_version"] == "3.2-rc.7"


def test_unknown_confirmation_is_omitted_when_old_response_cannot_express_null() -> None:
    payload = {
        "status": "completed",
        "media_buy_id": "mb_1",
        "packages": [],
        "revision": 1,
        "confirmed_at": None,
    }
    assert validate_response("create_media_buy", payload, version="3.2").valid
    response = convert_payload(
        "create_media_buy", payload, direction="sync", source_version="3.2", target_version="3.0"
    )
    assert "confirmed_at" not in response.payload
    assert any(
        loss.pointer == "/confirmed_at" and loss.reason == "null_unavailable"
        for loss in response.losses
    )
    assert validate_response("create_media_buy", response.payload, version="3.0").valid


@pytest.mark.parametrize(
    "payload,expected_keys",
    [
        (
            {"status": "submitted", "task_id": "task_1", "message": "Awaiting approval"},
            {"status", "task_id", "message"},
        ),
        (
            {"status": "failed", "errors": [{"code": "INVALID_REQUEST", "message": "Invalid"}]},
            {"errors"},
        ),
        (
            {
                "status": "completed",
                "media_buy_id": "mb_1",
                "packages": [],
                "revision": 1,
                "confirmed_at": "2026-10-22T00:00:00Z",
            },
            {"media_buy_id", "packages", "revision", "confirmed_at"},
        ),
    ],
)
def test_media_buy_union_projection_keeps_the_structural_response_arm(payload, expected_keys):
    assert validate_response("create_media_buy", payload, version="3.2").valid
    response = convert_payload(
        "create_media_buy", payload, direction="sync", source_version="3.2", target_version="3.0"
    )
    assert expected_keys <= response.payload.keys()
    assert validate_response("create_media_buy", response.payload, version="3.0").valid


@pytest.mark.parametrize("tool", ["create_media_buy", "update_media_buy"])
def test_explicit_async_schema_projection_preserves_submitted_envelopes(tool: str) -> None:
    payload = {"status": "submitted", "task_id": "task_1", "message": "Awaiting approval"}
    assert validate_response(tool, payload, version="3.2").valid
    response = convert_payload(
        tool, payload, direction="submitted", source_version="3.2", target_version="3.0"
    )
    assert response.payload == payload
    assert validate_response(tool, response.payload, version="3.0").valid


def test_human_summary_message_survives_an_older_open_response_contract() -> None:
    response = convert_payload(
        "get_products",
        {"status": "completed", "products": [], "cache_scope": "public", "message": "No products"},
        direction="sync",
        source_version="3.2",
        target_version="3.0",
    )
    assert response.payload["message"] == "No products"
    assert "cache_scope" not in response.payload
    assert all(loss.pointer != "/message" for loss in response.losses)
    assert validate_response("get_products", response.payload, version="3.0").valid


@pytest.mark.parametrize("target", ["3.1", "3.2"])
@pytest.mark.parametrize("metrics", [["video_completions"], ["impressions", "video_completions"]])
def test_removed_reporting_metrics_fail_without_inventing_a_new_metric(target, metrics) -> None:
    payload = {
        "products": [
            {
                "product_id": "product_1",
                "name": "Video",
                "description": "Video product",
                "publisher_properties": [
                    {"publisher_domain": "publisher.example", "selection_type": "all"}
                ],
                "format_ids": [],
                "delivery_type": "non_guaranteed",
                "pricing_options": [
                    {"pricing_model": "cpm", "pricing_option_id": "price_1", "currency": "USD"}
                ],
                "reporting_capabilities": {
                    "available_reporting_frequencies": ["daily"],
                    "expected_delay_minutes": 0,
                    "timezone": "UTC",
                    "supports_webhooks": False,
                    "available_metrics": metrics,
                    "date_range_support": "date_range",
                },
            }
        ]
    }
    original = deepcopy(payload)
    assert validate_response("get_products", payload, version="3.0").valid
    with pytest.raises(ADCPVersionConversionError) as error:
        convert_payload(
            "get_products", payload, direction="sync", source_version="3.0", target_version=target
        )
    assert payload == original
    assert error.value.pointer == (
        f"/products/0/reporting_capabilities/available_metrics/{len(metrics) - 1}"
    )
    assert error.value.source_version == "3.0"
    assert error.value.target_version == "3.1"


@pytest.mark.parametrize("scope", ["categories", "multiple_agents", "account"])
def test_legacy_governance_does_not_silently_broaden_authority(scope: str) -> None:
    agent = {
        "url": "https://governance.example/mcp/",
        "authentication": {"schemes": ["Bearer"], "credentials": "secret" * 8},
    }
    agents = [agent]
    if scope == "categories":
        agent["categories"] = ["budget_authority"]
    elif scope == "multiple_agents":
        agents.append(deepcopy(agent))
    payload = {
        "idempotency_key": "cross-version-governance-1234",
        "accounts": [{"account": {"account_id": "account_1"}, "governance_agents": agents}],
    }
    assert validate_request("sync_governance", payload, version="3.0").valid
    if scope == "account":
        response = convert_payload(
            "sync_governance",
            payload,
            direction="request",
            source_version="3.0",
            target_version="3.2",
        )
        assert validate_request("sync_governance", response.payload, version="3.2").valid
    else:
        with pytest.raises(ADCPVersionConversionError) as error:
            convert_payload(
                "sync_governance",
                payload,
                direction="request",
                source_version="3.0",
                target_version="3.2",
            )
        prefix = "/accounts/0/governance_agents"
        assert error.value.pointer == (
            prefix + "/0/categories" if scope == "categories" else prefix
        )
        assert "secret" not in str(error.value)


@pytest.mark.parametrize("tool", ["create_media_buy", "get_media_buys"])
def test_missing_legacy_confirmation_uses_null_only_for_uncommitted_state(tool: str) -> None:
    entry = {"media_buy_id": "mb_1", "packages": [], "revision": 1}
    payload = (
        entry
        if tool == "create_media_buy"
        else {
            "media_buys": [
                {**entry, "status": "pending_start", "total_budget": 100, "currency": "USD"}
            ]
        }
    )
    assert validate_response(tool, payload, version="3.0").valid
    response = convert_payload(
        tool, payload, direction="sync", source_version="3.0", target_version="3.2"
    )
    converted = (
        response.payload if tool == "create_media_buy" else response.payload["media_buys"][0]
    )
    assert converted["confirmed_at"] is None
    assert validate_response(tool, response.payload, version="3.2").valid


@pytest.mark.parametrize("missing", ["revision", "confirmed_at"])
def test_nonempty_old_media_buy_reads_never_invent_financial_state(missing: str) -> None:
    entry = {
        "media_buy_id": "mb_1",
        "status": "active",
        "total_budget": 100,
        "currency": "USD",
        "packages": [],
        "revision": 7,
        "confirmed_at": "2026-10-22T00:00:00Z",
    }
    entry.pop(missing)
    payload = {"media_buys": [entry]}
    assert validate_response("get_media_buys", payload, version="3.0").valid
    with pytest.raises(ADCPVersionConversionError) as error:
        convert_payload(
            "get_media_buys", payload, direction="sync", source_version="3.0", target_version="3.2"
        )
    assert error.value.pointer == f"/media_buys/0/{missing}"


@pytest.mark.parametrize("clear", ["geo_countries", "daily_budget_cap"])
def test_downgrade_rejects_an_explicit_clear_the_old_contract_cannot_express(clear: str) -> None:
    payload = {
        "idempotency_key": "cross-version-key-1234",
        "account": {"account_id": "account_1"},
        "media_buy_id": "mb_1",
    }
    if clear == "geo_countries":
        payload["packages"] = [
            {"package_id": "package_1", "targeting_overlay": {"geo_countries": None}}
        ]
    else:
        payload["daily_budget_cap"] = None
    assert validate_request("update_media_buy", payload, version="3.2").valid
    with pytest.raises(ADCPVersionConversionError, match="explicit null clear") as error:
        convert_payload(
            "update_media_buy",
            payload,
            direction="request",
            source_version="3.2",
            target_version="3.1",
        )
    assert error.value.pointer == (
        "/packages/0/targeting_overlay/geo_countries"
        if clear == "geo_countries"
        else "/daily_budget_cap"
    )


@pytest.mark.parametrize("tool", ["get_task_status", "list_tasks"])
def test_polling_results_project_the_task_contract_and_prefix_loss_paths(tool: str) -> None:
    row = {
        "task_id": "task_1",
        "task_type": "get_products",
        "protocol": "media-buy",
        "domain": "media-buy",
        "status": "completed",
        "created_at": "2026-10-22T00:00:00Z",
        "updated_at": "2026-10-22T00:00:00Z",
        "result": {
            "status": "completed",
            "products": [],
            "cache_scope": "public",
            "targeting_resolution": {"brief_targeting": {"geo_countries": ["US"]}},
        },
    }
    payload = (
        row
        if tool == "get_task_status"
        else {
            "status": "completed",
            "query_summary": {},
            "tasks": [row],
            "pagination": {"has_more": False},
        }
    )
    assert validate_response(tool, payload, version="3.2").valid
    downgraded = convert_payload(
        tool, payload, direction="sync", source_version="3.2", target_version="3.1"
    )
    converted_row = (
        downgraded.payload if tool == "get_task_status" else downgraded.payload["tasks"][0]
    )
    assert "targeting_resolution" not in converted_row["result"]
    prefix = "/result" if tool == "get_task_status" else "/tasks/0/result"
    assert any(loss.pointer == prefix + "/targeting_resolution" for loss in downgraded.losses)
    assert validate_response(tool, downgraded.payload, version="3.1").valid
    upgraded = convert_payload(
        tool, downgraded.payload, direction="sync", source_version="3.1", target_version="3.2"
    )
    converted_row = upgraded.payload if tool == "get_task_status" else upgraded.payload["tasks"][0]
    assert converted_row["result"]["adcp_version"] == "3.2"
    assert validate_response(tool, upgraded.payload, version="3.2").valid


@pytest.mark.parametrize("task_type", ["vendor_task", "get_task_status", "list_tasks"])
def test_unknown_and_recursive_poll_artifacts_remain_opaque(task_type: str) -> None:
    payload = {"task_type": task_type, "result": {"vendor": {"currency": "opaque"}}}
    response = convert_payload(
        "get_task_status", payload, direction="sync", source_version="3.2", target_version="3.1"
    )
    assert response.payload["result"] == payload["result"]


async def test_legacy_only_discovery_returns_a_structured_version_error() -> None:
    from adcp.exceptions import ADCPTaskError

    class Seller(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            pytest.fail("unsupported discovery must not reach the handler")

    caller = create_tool_caller(Seller(), "get_adcp_capabilities", supported_versions=("2.5",))
    with pytest.raises(ADCPTaskError) as error:
        await caller({})

    assert error.value.errors[0].code == "VERSION_UNSUPPORTED"


@pytest.mark.parametrize("request_payload", [None, {"account": {"account_id": "private"}}])
def test_old_account_responses_never_become_publicly_cacheable(request_payload) -> None:
    result = convert_payload(
        "get_products",
        {"products": []},
        direction="sync",
        source_version="3.0",
        target_version="3.2",
        request=request_payload,
    )
    assert result.payload["cache_scope"] == "account"


@pytest.mark.parametrize("protocol", [Protocol.MCP, Protocol.A2A])
async def test_client_converts_at_both_transport_boundaries(protocol: Protocol) -> None:
    client = ADCPClient(
        AgentConfig(
            id="seller",
            agent_uri="https://seller.example/mcp/",
            protocol=protocol,
        )
    )
    declaration = capabilities_response(
        ["media_buy"],
        adcp_version="3.1",
        supported_versions=["3.0", "3.1"],
    )
    declaration["adcp"]["idempotency"] = {"supported": False}
    caps = GetAdcpCapabilitiesResponse.model_validate(declaration)
    client.fetch_capabilities = AsyncMock(return_value=caps)
    wire_response = TaskResult(
        status=TaskStatus.COMPLETED,
        success=True,
        data={
            "products": [],
            "status": "completed",
            "cache_scope": "public",
            "adcp_version": "3.1",
        },
    )
    wire = AsyncMock(return_value=wire_response)
    setattr(
        client.adapter,
        "_call_mcp_tool_wire" if protocol == Protocol.MCP else "_call_a2a_tool_wire",
        wire,
    )
    result = await client.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "brief",
                "brief": "video",
                "brand": {"domain": "example.com", "countries": ["US"]},
            }
        )
    )
    params = wire.call_args.args[1]
    assert params["adcp_version"] == "3.1"
    assert params["brand"] == {"domain": "example.com"}
    assert result.success, result.error
    assert result.data.adcp_version == "3.2"
    report = result.metadata[CONVERSION_METADATA_KEY]
    assert report["wire_version"] == "3.1"
    assert report["losses"][0]["pointer"] == "/brand/countries"


@pytest.mark.parametrize("version", ["3.0", "3.1"])
async def test_server_handlers_receive_latest_and_respond_to_old_wire(version: str) -> None:
    contexts = []

    class Seller(ADCPHandler):
        adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": True}}}

        async def get_products(self, params: dict[str, Any], context: ToolContext):
            assert params.get("adcp_version", "3.2") == "3.2"
            assert context.resolved_adcp_version == version
            contexts.append(context)
            return {"products": [], "status": "completed", "cache_scope": "public"}

    caller = create_tool_caller(
        Seller(),
        "get_products",
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )
    wire = {"buying_mode": "brief", "brief": "video"}
    if version == "3.0":
        wire["adcp_major_version"] = 3
    else:
        wire["adcp_version"] = version
    response = await caller(wire)
    assert validate_response("get_products", response, version=version).valid
    if version == "3.0":
        assert "status" not in response
        assert "cache_scope" not in response
        assert contexts[0].metadata[CONVERSION_METADATA_KEY]["losses"]
    else:
        assert response["adcp_version"] == "3.1"


async def test_old_wire_validated_before_handler_upgrade() -> None:
    class Seller(ADCPHandler):
        async def get_products(self, params, context=None):
            pytest.fail("invalid old request must not reach the handler")

    caller = create_tool_caller(
        Seller(),
        "get_products",
        validation=ValidationHookConfig(requests="strict"),
    )
    from adcp.exceptions import ADCPTaskError

    with pytest.raises(ADCPTaskError, match="buying_mode"):
        await caller({"adcp_version": "3.1", "buying_mode": 123})


async def test_server_rejects_new_task_on_old_contract_with_structured_error() -> None:
    from adcp.exceptions import ADCPTaskError

    caller = create_tool_caller(ADCPHandler(), "list_products")
    with pytest.raises(ADCPTaskError) as exc:
        await caller({"adcp_version": "3.0"})
    assert exc.value.errors[0].code == "VERSION_UNSUPPORTED"


@pytest.mark.parametrize("selected", [None, ("3.0", "3.1")])
async def test_unversioned_server_discovery_retains_supported_versions(selected) -> None:
    class Seller(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            declaration = capabilities_response(["media_buy"])
            declaration["adcp"]["idempotency"] = {"supported": False}
            return declaration

    context = ToolContext()
    caller = create_tool_caller(
        Seller(),
        "get_adcp_capabilities",
        supported_versions=selected,
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )
    response = await caller({}, context)
    assert response["adcp"]["supported_versions"] == (
        list(selected) if selected else ["3.0", "3.1", "3.2", "3.2-rc.7"]
    )
    assert context.resolved_adcp_version == ("3.1" if selected else "3.2")


async def test_mcp_discovery_wire_validation_and_concurrent_call_versions() -> None:
    """Exercise real adapter validation, discovery caching, and per-call state."""
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp/", protocol=Protocol.MCP),
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )
    declaration = capabilities_response(
        ["media_buy"],
        adcp_version="3.1",
        supported_versions=["3.0", "3.1"],
    )
    declaration["adcp"]["idempotency"] = {"supported": False}
    sent = []

    async def call_tool(name, params):
        result = MagicMock()
        result.isError = False
        result.content = []
        if name == "get_adcp_capabilities":
            assert "adcp_version" not in params
            result.structuredContent = declaration
        else:
            sent.append(deepcopy(params))
            version = client.adapter._request_version(params)
            assert validate_request(name, params, version=version).valid
            await asyncio.sleep(0)
            # Deliberately different wire envelopes for concurrent calls.
            result.structuredContent = {"products": []}
            if version == "3.1":
                result.structuredContent.update(status="completed", cache_scope="public")
        return result

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=call_tool)
    client.adapter._get_session = AsyncMock(return_value=session)
    # Complete discovery once, then use the real TTL cache for both calls.
    await client.fetch_capabilities()
    results = await asyncio.gather(
        *[
            client.get_products(
                GetProductsRequest.model_validate(
                    {
                        "buying_mode": "brief",
                        "brief": "video",
                        "adcp_version": version,
                        "brand": {"domain": "example.com", "countries": ["US"]},
                    }
                )
            )
            for version in ("3.2", "3.0")
        ]
    )
    assert all(result.success for result in results), [result.error for result in results]
    assert {client.adapter._request_version(params) for params in sent} == {"3.0", "3.1"}
    assert all("countries" not in params["brand"] for params in sent)
    assert [result.metadata[CONVERSION_METADATA_KEY]["wire_version"] for result in results] == [
        "3.1",
        "3.0",
    ]
    assert all(result.data.adcp_version == "3.2" for result in results)
    assert session.call_tool.call_count == 3


async def test_server_response_losses_are_visible_to_application_context() -> None:
    class Seller(ADCPHandler):
        async def sync_accounts(self, params, context=None):
            return {
                "status": "completed",
                "accounts": [
                    {
                        "brand": {"domain": "example.com", "countries": ["US"]},
                        "operator": "agency.example",
                        "operator_unit": {"id": "one"},
                        "currency": "USD",
                        "timezone": "UTC",
                        "action": "created",
                        "status": "active",
                        "billing": "operator",
                        "account_scope": "operator_brand",
                    }
                ],
            }

    context = ToolContext()
    response = await create_tool_caller(Seller(), "sync_accounts")(
        {
            "adcp_version": "3.1",
            "accounts": [],
        },
        context,
    )
    assert response["accounts"][0]["brand"] == {"domain": "example.com"}
    report = context.metadata[CONVERSION_METADATA_KEY]
    assert {loss["pointer"] for loss in report["losses"]} >= {
        "/accounts/0/brand/countries",
        "/accounts/0/operator_unit",
        "/accounts/0/currency",
        "/accounts/0/timezone",
    }


async def test_reduced_echo_registers_the_complete_original_account_key() -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp/", protocol=Protocol.MCP),
        server_version="3.1",
        account_policy="auto",
    )
    key = {
        "brand": {"domain": "example.com", "countries": ["US"]},
        "operator": "agency.example",
        "operator_unit": {"id": "one"},
        "currency": "USD",
        "timezone": "UTC",
    }
    client.adapter._call_mcp_tool_wire = AsyncMock(
        return_value=TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data={
                "status": "completed",
                "accounts": [
                    {
                        "brand": {"domain": "example.com"},
                        "operator": "agency.example",
                        "account_id": "older-seller-account",
                        "status": "active",
                        "action": "created",
                    }
                ],
            },
        )
    )
    result = await client.sync_accounts(
        SyncAccountsRequest.model_validate(
            {
                "idempotency_key": "cross-version-key-1234",
                "accounts": [{**key, "billing": "operator"}],
            }
        )
    )
    assert result.success, result.error
    record = await client.accounts.get(key)
    assert record is not None
    assert record.account_id == "older-seller-account"


async def test_in_flight_creative_dialect_survives_capability_refresh() -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp/", protocol=Protocol.MCP),
        server_version="3.1",
    )
    request = {"buying_mode": "brief", "brief": "video"}
    prepared = await client._convert_outgoing_request("get_products", request)
    assert prepared.creative_dialect == "legacy"
    declaration = capabilities_response(["media_buy"])
    declaration["adcp"]["idempotency"] = {"supported": False}
    declaration["media_buy"] = {"features": {"canonical_creatives": True}}
    client._capabilities = GetAdcpCapabilitiesResponse.model_validate(declaration)
    result = client._convert_incoming_response(
        "get_products",
        TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data={"products": [], "status": "completed", "cache_scope": "public"},
        ),
        prepared,
    )
    assert client._creative_dialect(request) is CreativeDialect.CANONICAL
    assert client._creative_dialect(request, result=result) is CreativeDialect.LEGACY
