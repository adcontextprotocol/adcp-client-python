"""Tests for ADCPClient."""

import pytest

from adcp import ADCPClient, ADCPMultiAgentClient
from adcp.types import AgentConfig, Protocol
from tests.conftest import validate_union


def test_agent_config_creation():
    """Test creating agent configuration."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    assert config.id == "test_agent"
    assert config.agent_uri == "https://test.example.com"
    assert config.protocol == Protocol.A2A


@pytest.mark.parametrize(
    "input_uri,expected_uri",
    [
        ("https://example.com/mcp/", "https://example.com/mcp/"),
        ("https://example.com/mcp", "https://example.com/mcp"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_agent_uri_preserves_user_supplied_form(input_uri: str, expected_uri: str) -> None:
    cfg = AgentConfig(id="x", agent_uri=input_uri, protocol=Protocol.MCP)
    assert cfg.agent_uri == expected_uri


def test_agent_config_extra_headers_default_empty():
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.MCP,
    )
    assert config.extra_headers == {}


def test_agent_config_extra_headers_accepted():
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.MCP,
        extra_headers={"x-adcp-tenant": "acme", "x-correlation-id": "req-1"},
    )
    assert config.extra_headers == {
        "x-adcp-tenant": "acme",
        "x-correlation-id": "req-1",
    }


def test_agent_config_extra_headers_rejects_auth_header_collision():
    with pytest.raises(ValueError, match="reserved auth header"):
        AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
            auth_header="x-custom-auth",
            extra_headers={"X-Custom-Auth": "tok"},  # case-insensitive collision
        )


def test_agent_config_extra_headers_rejects_authorization_collision():
    with pytest.raises(ValueError, match="reserved auth header"):
        AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
            extra_headers={"Authorization": "Bearer foo"},
        )


def test_agent_config_extra_headers_rejects_empty_key():
    with pytest.raises(ValueError, match="empty header name"):
        AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
            extra_headers={"": "value"},
        )


def test_agent_config_extra_headers_rejects_crlf_in_value():
    with pytest.raises(ValueError, match="CR/LF/NUL"):
        AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
            extra_headers={"x-trace": "a\r\nAuthorization: Bearer evil"},
        )


def test_agent_config_extra_headers_rejects_crlf_in_key():
    with pytest.raises(ValueError, match="control character"):
        AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
            extra_headers={"x-trace\nInjected": "value"},
        )


def test_client_creation():
    """Test creating ADCP client."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    assert client.agent_config == config


def test_multi_agent_client_creation():
    """Test creating multi-agent client."""
    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    client = ADCPMultiAgentClient(agents)

    assert len(client.agents) == 2
    assert "agent1" in client.agent_ids
    assert "agent2" in client.agent_ids


def test_webhook_url_generation():
    """Test webhook URL generation."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(
        config,
        webhook_url_template="https://myapp.com/webhook/{task_type}/{agent_id}/{operation_id}",
    )

    url = client.get_webhook_url("get_products", "op_123")

    assert url == "https://myapp.com/webhook/get_products/test_agent/op_123"


@pytest.mark.asyncio
async def test_get_products():
    """Test get_products method with mock adapter."""
    from unittest.mock import patch

    from adcp.types._generated import GetProductsRequest, GetProductsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Mock both the adapter method and parsing
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": []},  # Simple data for adapter
        success=True,
    )

    mock_parsed_result = TaskResult[GetProductsResponse](
        status=TaskStatus.COMPLETED,
        data=GetProductsResponse(products=[]),  # Properly typed result
        success=True,
    )

    with (
        patch.object(client.adapter, "get_products", return_value=mock_raw_result) as mock_get,
        patch.object(
            client.adapter, "_parse_response", return_value=mock_parsed_result
        ) as mock_parse,
    ):
        request = validate_union(
            GetProductsRequest, {"buying_mode": "brief", "brief": "test campaign"}
        )
        result = await client.get_products(request)

        # Verify adapter method was called
        mock_get.assert_called_once_with({"brief": "test campaign", "buying_mode": "brief"})
        # Verify parsing was called with correct type
        mock_parse.assert_called_once_with(mock_raw_result, GetProductsResponse)
        # Verify final result
        assert result.success is True
        assert result.status == TaskStatus.COMPLETED
        assert isinstance(result.data, GetProductsResponse)


@pytest.mark.asyncio
async def test_get_products_wholesale_versions_sent_and_parsed():
    """Wholesale product enumeration sends and parses beta 3 version tokens."""
    from unittest.mock import patch

    from adcp.types._generated import GetProductsRequest, GetProductsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )
    client = ADCPClient(config)

    raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={
            "status": "completed",
            "products": [],
            "wholesale_feed_version": "wf_2",
            "pricing_version": "pr_2",
            "cache_scope": "public",
        },
        success=True,
    )
    parsed_result = TaskResult[GetProductsResponse](
        status=TaskStatus.COMPLETED,
        data=GetProductsResponse(
            status="completed",
            products=[],
            wholesale_feed_version="wf_2",
            pricing_version="pr_2",
            cache_scope="public",
        ),
        success=True,
    )

    with (
        patch.object(client.adapter, "get_products", return_value=raw_result) as mock_get,
        patch.object(client.adapter, "_parse_response", return_value=parsed_result),
    ):
        request = GetProductsRequest(
            buying_mode="wholesale",
            if_wholesale_feed_version="wf_1",
            if_pricing_version="pr_1",
            pagination={"max_results": 25},
        )
        result = await client.get_products(request)

    mock_get.assert_called_once_with(
        {
            "buying_mode": "wholesale",
            "pagination": {"max_results": 25},
            "if_wholesale_feed_version": "wf_1",
            "if_pricing_version": "pr_1",
        }
    )
    assert result.data.wholesale_feed_version == "wf_2"
    assert result.data.pricing_version == "pr_2"
    assert result.data.cache_scope.value == "public"
    assert result.data.unchanged is None
    assert "unchanged" not in result.data.model_dump(mode="json", exclude_none=True)


@pytest.mark.asyncio
async def test_new_brand_creative_methods_parse_with_response_models():
    """New beta 3 client methods should route through their generated response types."""
    from unittest.mock import patch

    from adcp.types import _generated as gen
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )
    client = ADCPClient(config)
    raw_result = TaskResult(status=TaskStatus.COMPLETED, data={}, success=True)

    cases = [
        (
            "validate_input",
            gen.ValidateInputRequest.model_construct(input={}),
            gen.ValidateInputResponse,
        ),
        (
            "verify_brand_claim",
            gen.VerifyBrandClaimRequest.model_construct(claim_type="domain", claim={}),
            gen.VerifyBrandClaimResponse,
        ),
        (
            "verify_brand_claims",
            gen.VerifyBrandClaimsRequest.model_construct(claims=[]),
            gen.VerifyBrandClaimsResponseBulk,
        ),
    ]

    for method_name, request, response_type in cases:
        parsed_result = TaskResult(
            status=TaskStatus.COMPLETED,
            data={},
            success=True,
        )
        with (
            patch.object(client.adapter, method_name, return_value=raw_result) as mock_call,
            patch.object(
                client.adapter, "_parse_response", return_value=parsed_result
            ) as mock_parse,
        ):
            result = await getattr(client, method_name)(request)

        mock_call.assert_called_once_with(request.model_dump(mode="json", exclude_none=True))
        mock_parse.assert_called_once_with(raw_result, response_type)
        assert result is parsed_result


@pytest.mark.asyncio
async def test_get_signals_wholesale_versions_sent_and_parsed():
    """Wholesale signal enumeration sends and parses beta 3 version tokens."""
    from unittest.mock import patch

    from adcp.types._generated import GetSignalsRequest, GetSignalsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )
    client = ADCPClient(config)

    raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={
            "status": "completed",
            "signals": [],
            "wholesale_feed_version": "swf_2",
            "pricing_version": "spr_2",
            "cache_scope": "account",
        },
        success=True,
    )
    parsed_result = TaskResult[GetSignalsResponse](
        status=TaskStatus.COMPLETED,
        data=GetSignalsResponse(
            status="completed",
            signals=[],
            wholesale_feed_version="swf_2",
            pricing_version="spr_2",
            cache_scope="account",
        ),
        success=True,
    )

    with (
        patch.object(client.adapter, "get_signals", return_value=raw_result) as mock_get,
        patch.object(client.adapter, "_parse_response", return_value=parsed_result),
    ):
        request = GetSignalsRequest(
            discovery_mode="wholesale",
            if_wholesale_feed_version="swf_1",
            if_pricing_version="spr_1",
            pagination={"max_results": 50},
        )
        result = await client.get_signals(request)

    mock_get.assert_called_once_with(
        {
            "discovery_mode": "wholesale",
            "pagination": {"max_results": 50},
            "if_wholesale_feed_version": "swf_1",
            "if_pricing_version": "spr_1",
        }
    )
    assert result.data.wholesale_feed_version == "swf_2"
    assert result.data.pricing_version == "spr_2"
    assert result.data.cache_scope.value == "account"
    assert result.data.unchanged is None
    assert "unchanged" not in result.data.model_dump(mode="json", exclude_none=True)


@pytest.mark.asyncio
async def test_all_client_methods():
    """Test that all AdCP tool methods exist and are callable."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Verify all required methods exist
    assert hasattr(client, "get_products")
    assert hasattr(client, "list_creative_formats")
    assert hasattr(client, "sync_creatives")
    assert hasattr(client, "list_creatives")
    assert hasattr(client, "get_media_buy_delivery")
    assert hasattr(client, "get_media_buys")
    assert hasattr(client, "get_signals")
    assert hasattr(client, "activate_signal")
    assert hasattr(client, "provide_performance_feedback")
    assert hasattr(client, "preview_creative")
    assert hasattr(client, "create_media_buy")
    assert hasattr(client, "update_media_buy")
    assert hasattr(client, "build_creative")
    assert hasattr(client, "list_accounts")
    assert hasattr(client, "sync_accounts")
    assert hasattr(client, "get_account_financials")
    assert hasattr(client, "report_usage")
    assert hasattr(client, "log_event")
    assert hasattr(client, "sync_event_sources")
    assert hasattr(client, "sync_audiences")
    assert hasattr(client, "sync_catalogs")
    assert hasattr(client, "get_creative_delivery")
    # V3 Protocol Discovery
    assert hasattr(client, "get_adcp_capabilities")
    # V3 Content Standards
    assert hasattr(client, "create_content_standards")
    assert hasattr(client, "get_content_standards")
    assert hasattr(client, "list_content_standards")
    assert hasattr(client, "update_content_standards")
    assert hasattr(client, "calibrate_content")
    assert hasattr(client, "validate_content_delivery")
    assert hasattr(client, "get_media_buy_artifacts")
    # V3 Governance
    assert hasattr(client, "get_creative_features")
    assert hasattr(client, "sync_plans")
    assert hasattr(client, "check_governance")
    assert hasattr(client, "report_plan_outcome")
    assert hasattr(client, "get_plan_audit_logs")
    # V3 Sponsored Intelligence
    assert hasattr(client, "si_get_offering")
    assert hasattr(client, "si_initiate_session")
    assert hasattr(client, "si_send_message")
    assert hasattr(client, "si_terminate_session")
    # V3 Governance (Property Lists)
    assert hasattr(client, "create_property_list")
    assert hasattr(client, "get_property_list")
    assert hasattr(client, "list_property_lists")
    assert hasattr(client, "update_property_list")
    assert hasattr(client, "delete_property_list")
    # V3 TMP
    assert hasattr(client, "context_match")
    assert hasattr(client, "identity_match")
    # V3 Brand Rights
    assert hasattr(client, "get_brand_identity")
    assert hasattr(client, "get_rights")
    assert hasattr(client, "acquire_rights")
    # V3 Compliance
    assert hasattr(client, "comply_test_controller")


@pytest.mark.parametrize(
    "method_name,request_class,request_data",
    [
        ("get_products", "GetProductsRequest", {"buying_mode": "wholesale"}),
        ("list_creative_formats", "ListCreativeFormatsRequest", {}),
        (
            "sync_creatives",
            "SyncCreativesRequest",
            {
                "account": {"account_id": "acct-1"},
                "creatives": [
                    {
                        "creative_id": "test",
                        "name": "Test",
                        "format_id": {
                            "id": "fmt-1",
                            "agent_url": "https://agent.example.com/",
                        },
                        "assets": {
                            "slot1": {
                                "content": "hello",
                                "asset_type": "text",
                                "name": "headline",
                            }
                        },
                    }
                ],
            },
        ),
        ("list_creatives", "ListCreativesRequest", {}),
        ("get_media_buy_delivery", "GetMediaBuyDeliveryRequest", {}),
        ("get_media_buys", "GetMediaBuysRequest", {"account": {"account_id": "acct-1"}}),
        (
            "get_signals",
            "GetSignalsRequest",
            {
                "signal_spec": "test",
                "deliver_to": {
                    "countries": ["US"],
                    "destinations": [{"type": "platform", "platform": "test"}],
                },
            },
        ),
        (
            "activate_signal",
            "ActivateSignalRequest",
            {
                "signal_agent_segment_id": "test",
                "destinations": [{"type": "platform", "platform": "test"}],
            },
        ),
        (
            "provide_performance_feedback",
            "ProvidePerformanceFeedbackRequest",
            {
                "media_buy_id": "test",
                "measurement_period": {
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-31T23:59:59Z",
                },
                "performance_index": 0.5,
            },
        ),
        ("list_accounts", "ListAccountsRequest", {}),
        (
            "sync_accounts",
            "SyncAccountsRequest",
            {
                "accounts": [
                    {
                        "billing": "operator",
                        "brand": {"domain": "example.com"},
                        "operator": "test-operator",
                    }
                ]
            },
        ),
        (
            "get_account_financials",
            "GetAccountFinancialsRequest",
            {"account": {"account_id": "acct-1"}},
        ),
        (
            "report_usage",
            "ReportUsageRequest",
            {
                "reporting_period": {
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-31T23:59:59Z",
                },
                "usage": [
                    {
                        "account": {"account_id": "acct-1"},
                        "vendor_cost": 123.45,
                        "currency": "USD",
                    }
                ],
            },
        ),
        (
            "log_event",
            "LogEventRequest",
            {
                "event_source_id": "src-1",
                "events": [
                    {
                        "event_id": "evt-1",
                        "event_type": "purchase",
                        "event_time": "2024-01-01T00:00:00Z",
                    }
                ],
            },
        ),
        (
            "sync_event_sources",
            "SyncEventSourcesRequest",
            {"account": {"account_id": "acct-1"}},
        ),
        (
            "sync_audiences",
            "SyncAudiencesRequest",
            {"account": {"account_id": "acct-1"}},
        ),
        (
            "sync_catalogs",
            "SyncCatalogsRequest",
            {"account": {"account_id": "acct-1"}},
        ),
        (
            "get_creative_delivery",
            "GetCreativeDeliveryRequest",
            {"media_buy_ids": ["mb-1"]},
        ),
        # V3 Governance
        (
            "sync_plans",
            "SyncPlansRequest",
            {"plans": []},
        ),
        (
            "check_governance",
            "CheckGovernanceRequest",
            {
                "plan_id": "plan_123",
                "caller": "https://buyer.example.com",
            },
        ),
        (
            "report_plan_outcome",
            "ReportPlanOutcomeRequest",
            {
                "plan_id": "plan_123",
                "outcome": "completed",
                "governance_context": "ctx-abc-123-governance",
            },
        ),
        (
            "get_plan_audit_logs",
            "GetPlanAuditLogsRequest",
            {"plan_ids": ["plan_123"]},
        ),
        (
            "get_creative_features",
            "GetCreativeFeaturesRequest",
            {
                "creative_manifest": {
                    "creative_id": "cr-1",
                    "name": "Test",
                    "format_id": {"id": "fmt-1", "agent_url": "https://a.example.com/"},
                    "assets": {},
                },
            },
        ),
        # V3 Property Lists
        (
            "create_property_list",
            "CreatePropertyListRequest",
            {"name": "test-list"},
        ),
        (
            "get_property_list",
            "GetPropertyListRequest",
            {"list_id": "pl-1"},
        ),
        (
            "list_property_lists",
            "ListPropertyListsRequest",
            {},
        ),
        (
            "update_property_list",
            "UpdatePropertyListRequest",
            {"list_id": "pl-1"},
        ),
        (
            "delete_property_list",
            "DeletePropertyListRequest",
            {"list_id": "pl-1"},
        ),
        # V3 TMP
        (
            "context_match",
            "ContextMatchRequest",
            {
                "property_rid": "01912345-6789-7abc-def0-123456789abc",
                "placement_id": "top-banner",
                "property_type": "website",
                "request_id": "req-001",
                "type": "context_match_request",
            },
        ),
        (
            "identity_match",
            "IdentityMatchRequest",
            {
                "request_id": "req-002",
                "type": "identity_match_request",
                "seller_agent_url": "https://seller.example.com",
                "identities": [{"user_token": "opaque-token-123", "uid_type": "uid2"}],
                "package_ids": ["pkg-1"],
            },
        ),
        # V3 Brand Rights
        (
            "get_brand_identity",
            "GetBrandIdentityRequest",
            {"brand_id": "brand-123"},
        ),
        (
            "get_rights",
            "GetRightsRequest",
            {"query": "celebrity spokesperson", "uses": ["likeness"]},
        ),
        (
            "acquire_rights",
            "AcquireRightsRequest",
            {
                "rights_id": "rights-123",
                "pricing_option_id": "opt-1",
                "buyer": {"domain": "buyer.example.com"},
                "campaign": {"description": "Test campaign", "uses": ["likeness"]},
                "revocation_webhook": {
                    "url": "https://buyer.example.com/webhook",
                    "authentication": {
                        "schemes": ["Bearer"],
                        "credentials": "a" * 32,
                    },
                },
            },
        ),
        # V3 Compliance
        (
            "comply_test_controller",
            "ComplyTestControllerRequest",
            {"scenario": "list_scenarios"},
        ),
        # Note: preview_creative, create_media_buy, update_media_buy, and build_creative
        # are tested separately with full request validation since their schemas are complex
    ],
)
@pytest.mark.asyncio
async def test_method_calls_correct_tool_name(method_name, request_class, request_data):
    """Test that each method calls the correct adapter method.

    This test ensures client methods call the matching adapter method
    (e.g., client.get_products calls adapter.get_products).
    """
    from unittest.mock import patch

    import adcp.types._generated as gen
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Create request instance with required fields
    # Some types are Union aliases (not callable) after RootModel unwrap (#155)
    import types

    request_cls = getattr(gen, request_class)
    if isinstance(request_cls, types.UnionType):
        request = validate_union(request_cls, request_data)
    else:
        if "idempotency_key" in getattr(request_cls, "model_fields", {}):
            request_data = {"idempotency_key": "test-idempotency-key", **request_data}
        request = request_cls(**request_data)

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={},
        success=True,
    )

    # Mock the specific adapter method (not call_tool)
    with patch.object(client.adapter, method_name, return_value=mock_result) as mock_method:
        method = getattr(client, method_name)
        await method(request)

        # Verify adapter method was called
        mock_method.assert_called_once()


@pytest.mark.asyncio
async def test_multi_agent_parallel_execution():
    """Test parallel execution across multiple agents."""
    from unittest.mock import patch

    from adcp.types._generated import GetProductsRequest
    from adcp.types.core import TaskResult, TaskStatus

    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    client = ADCPMultiAgentClient(agents)

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": []},
        success=True,
    )

    # Mock both agents' adapters - keep context active during execution
    with (
        patch.object(
            client.agents["agent1"].adapter, "get_products", return_value=mock_result
        ) as mock1,
        patch.object(
            client.agents["agent2"].adapter, "get_products", return_value=mock_result
        ) as mock2,
    ):
        request = validate_union(GetProductsRequest, {"buying_mode": "wholesale"})
        results = await client.get_products(request)

        # Verify both agents' get_products method was called
        mock1.assert_called_once_with({"buying_mode": "wholesale"})
        mock2.assert_called_once_with({"buying_mode": "wholesale"})

        # Verify results from both agents
        assert len(results) == 2
        assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_list_creative_formats_parses_mcp_response():
    """Test that list_creative_formats parses MCP content array into structured response."""
    import json
    from unittest.mock import patch

    from adcp.types._generated import ListCreativeFormatsRequest, ListCreativeFormatsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    # Mock MCP response with content array containing JSON
    formats_data = {
        "formats": [
            {
                "format_id": {"agent_url": "https://creative.example.com", "id": "banner_300x250"},
                "name": "Medium Rectangle",
                "type": "display",
            },
            {
                "format_id": {"agent_url": "https://creative.example.com", "id": "video_16x9"},
                "name": "Video 16:9",
                "type": "video",
            },
        ]
    }

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=[{"type": "text", "text": json.dumps(formats_data)}],  # MCP content array
        success=True,
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_result):
        request = ListCreativeFormatsRequest()
        result = await client.list_creative_formats(request)

        # Verify response is parsed into structured type
        assert result.success is True
        assert isinstance(result.data, ListCreativeFormatsResponse)
        assert len(result.data.formats) == 2
        assert result.data.formats[0].name == "Medium Rectangle"
        assert result.data.formats[1].name == "Video 16:9"


@pytest.mark.asyncio
async def test_list_creative_formats_parses_a2a_response():
    """Test that list_creative_formats parses A2A dict response into structured response."""
    from unittest.mock import patch

    from adcp.types._generated import ListCreativeFormatsRequest, ListCreativeFormatsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Mock A2A response with direct dict data
    formats_data = {
        "formats": [
            {
                "format_id": {"agent_url": "https://creative.example.com", "id": "native_feed"},
                "name": "Native Feed Ad",
                "type": "native",
            }
        ]
    }

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=formats_data,  # Direct dict from A2A
        success=True,
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_result):
        request = ListCreativeFormatsRequest()
        result = await client.list_creative_formats(request)

        # Verify response is parsed into structured type
        assert result.success is True
        assert isinstance(result.data, ListCreativeFormatsResponse)
        assert len(result.data.formats) == 1
        assert result.data.formats[0].name == "Native Feed Ad"


@pytest.mark.asyncio
async def test_list_creative_formats_handles_invalid_response():
    """Test that list_creative_formats handles invalid response gracefully."""
    from unittest.mock import patch

    from adcp.types._generated import ListCreativeFormatsRequest
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    # Mock invalid response (text instead of structured data)
    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=[{"type": "text", "text": "Found 42 creative formats"}],  # Invalid: not JSON
        success=True,
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_result):
        request = ListCreativeFormatsRequest()
        result = await client.list_creative_formats(request)

        # Verify error is returned
        assert result.success is False
        assert result.status == TaskStatus.FAILED
        assert "Failed to parse response" in result.error


@pytest.mark.asyncio
async def test_client_context_manager():
    """Test that ADCPClient works as an async context manager."""
    from unittest.mock import AsyncMock, patch

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.MCP,
    )

    # Mock the close method to verify it gets called
    with patch.object(ADCPClient, "close", new_callable=AsyncMock) as mock_close:
        async with ADCPClient(config) as client:
            assert client.agent_config == config

        # Verify close was called on context exit
        mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_multi_agent_context_manager():
    """Test that ADCPMultiAgentClient works as an async context manager."""
    from unittest.mock import AsyncMock, patch

    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    # Mock the close method to verify it gets called
    with patch.object(ADCPMultiAgentClient, "close", new_callable=AsyncMock) as mock_close:
        async with ADCPMultiAgentClient(agents) as client:
            assert len(client.agents) == 2

        # Verify close was called on context exit
        mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_client_context_manager_with_exception():
    """Test that ADCPClient properly closes even when an exception occurs."""
    from unittest.mock import AsyncMock, patch

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.MCP,
    )

    # Mock the close method to verify it gets called
    with patch.object(ADCPClient, "close", new_callable=AsyncMock) as mock_close:
        try:
            async with ADCPClient(config) as client:
                assert client.agent_config == config
                raise ValueError("Test exception")
        except ValueError:
            pass  # Expected

        # Verify close was called even after exception
        mock_close.assert_called_once()


def test_get_media_buys_request_account_is_optional():
    """GetMediaBuysRequest.account is optional per AdCP 3.0.0-rc.1 schema."""
    from adcp.types._generated import GetMediaBuysRequest

    req = GetMediaBuysRequest.model_validate({})
    assert req.account is None
    assert "account" not in req.model_dump(exclude_none=True)


@pytest.mark.asyncio
async def test_get_media_buys_parses_response():
    """Test that get_media_buys parses A2A response into typed GetMediaBuysResponse."""
    from unittest.mock import patch

    from adcp.types._generated import GetMediaBuysRequest, GetMediaBuysResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    media_buys_data = {
        "media_buys": [
            {
                "media_buy_id": "mb-1",
                "status": "active",
                "currency": "USD",
                "total_budget": 5000.0,
                "packages": [
                    {
                        "package_id": "pkg-1",
                        "creative_approvals": [
                            {"creative_id": "cr-1", "approval_status": "approved"}
                        ],
                    }
                ],
            }
        ]
    }

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=media_buys_data,
        success=True,
    )

    with patch.object(client.adapter, "get_media_buys", return_value=mock_result) as mock_adapter:
        result = await client.get_media_buys(
            GetMediaBuysRequest.model_validate({"account": {"account_id": "acct-1"}})
        )
        mock_adapter.assert_called_once_with(
            {"account": {"account_id": "acct-1"}, "include_history": 0, "include_snapshot": False}
        )
        assert result.success is True
        assert isinstance(result.data, GetMediaBuysResponse)
        assert len(result.data.media_buys) == 1
        assert result.data.media_buys[0].media_buy_id == "mb-1"
        assert result.data.media_buys[0].currency == "USD"
        assert result.data.media_buys[0].total_budget == 5000.0
        packages = result.data.media_buys[0].packages
        assert len(packages) == 1
        assert packages[0].package_id == "pkg-1"


@pytest.mark.asyncio
async def test_get_media_buys_parses_snapshot_response():
    """Test that get_media_buys parses snapshot data including DeliveryStatus."""
    from unittest.mock import patch

    from adcp.types._generated import (
        DeliveryStatus,
        GetMediaBuysRequest,
        GetMediaBuysResponse,
        SnapshotUnavailableReason,
    )
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    media_buys_data = {
        "media_buys": [
            {
                "media_buy_id": "mb-2",
                "status": "active",
                "currency": "USD",
                "total_budget": 10000.0,
                "packages": [
                    {
                        "package_id": "pkg-delivering",
                        "snapshot": {
                            "impressions": 4500.0,
                            "spend": 225.50,
                            "as_of": "2026-02-22T12:00:00Z",
                            "staleness_seconds": 900,
                            "delivery_status": "delivering",
                            "pacing_index": 0.95,
                        },
                    },
                    {
                        "package_id": "pkg-not-delivering",
                        "snapshot": {
                            "impressions": 0.0,
                            "spend": 0.0,
                            "as_of": "2026-02-22T12:00:00Z",
                            "staleness_seconds": 900,
                            "delivery_status": "not_delivering",
                        },
                    },
                    {
                        "package_id": "pkg-no-snapshot",
                        "snapshot_unavailable_reason": "SNAPSHOT_UNSUPPORTED",
                    },
                ],
            }
        ]
    }

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=media_buys_data,
        success=True,
    )

    with patch.object(client.adapter, "get_media_buys", return_value=mock_result) as mock_adapter:
        result = await client.get_media_buys(
            GetMediaBuysRequest.model_validate(
                {"account": {"account_id": "acct-1"}, "include_snapshot": True}
            )
        )
        mock_adapter.assert_called_once_with(
            {"account": {"account_id": "acct-1"}, "include_history": 0, "include_snapshot": True}
        )
        assert result.success is True
        assert isinstance(result.data, GetMediaBuysResponse)

        packages = result.data.media_buys[0].packages
        assert len(packages) == 3

        delivering = packages[0]
        assert delivering.snapshot is not None
        assert delivering.snapshot.delivery_status.value == DeliveryStatus.delivering.value
        assert delivering.snapshot.impressions == 4500.0
        assert delivering.snapshot.spend == 225.50
        assert delivering.snapshot.staleness_seconds == 900
        assert delivering.snapshot.pacing_index == 0.95

        not_delivering = packages[1]
        assert not_delivering.snapshot is not None
        assert not_delivering.snapshot.delivery_status.value == DeliveryStatus.not_delivering.value
        assert not_delivering.snapshot.impressions == 0.0

        no_snapshot = packages[2]
        assert no_snapshot.snapshot is None
        assert (
            no_snapshot.snapshot_unavailable_reason.value
            == SnapshotUnavailableReason.SNAPSHOT_UNSUPPORTED.value
        )


@pytest.mark.asyncio
async def test_multi_agent_close_handles_adapter_failures():
    """Test that multi-agent close handles individual adapter failures gracefully."""
    from unittest.mock import AsyncMock, patch

    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    client = ADCPMultiAgentClient(agents)

    # Mock one adapter to fail during close
    mock_close_success = AsyncMock()
    mock_close_failure = AsyncMock(side_effect=RuntimeError("Cleanup error"))

    with (
        patch.object(client.agents["agent1"].adapter, "close", mock_close_success),
        patch.object(client.agents["agent2"].adapter, "close", mock_close_failure),
    ):
        # Should not raise despite one adapter failing
        await client.close()

        # Verify both adapters had close called
        mock_close_success.assert_called_once()
        mock_close_failure.assert_called_once()
