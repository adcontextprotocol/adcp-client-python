"""Tests for ADCP server framework."""

import pytest

from adcp.server import (
    ADCPHandler,
    ContentStandardsHandler,
    GovernanceHandler,
    MCPToolSet,
    NotImplementedResponse,
    ProposalBuilder,
    ProposalNotSupported,
    SponsoredIntelligenceHandler,
    ToolContext,
    create_mcp_tools,
    not_supported,
)
from adcp.server.proposal import proposals_not_supported
from adcp.types import (
    CalibrateContentResponse,
    CreateContentStandardsResponse,
    CreatePropertyListResponse,
    DeletePropertyListResponse,
    GetContentStandardsResponse,
    GetMediaBuyArtifactsResponse,
    GetPropertyListResponse,
    ListContentStandardsResponse,
    ListPropertyListsResponse,
    SiGetOfferingResponse,
    SiInitiateSessionResponse,
    SiSendMessageResponse,
    SiTerminateSessionResponse,
    UpdateContentStandardsResponse,
    UpdatePropertyListResponse,
    ValidateContentDeliveryResponse,
)
from tests.conftest import validate_union


class TestNotSupported:
    """Tests for not_supported helper."""

    def test_not_supported_default_message(self):
        """Test not_supported with default message."""
        response = not_supported()
        assert response.supported is False
        assert response.error is not None
        assert response.error.code == "NOT_SUPPORTED"

    def test_not_supported_custom_message(self):
        """Test not_supported with custom message."""
        response = not_supported("Custom reason here")
        assert response.supported is False
        assert response.reason == "Custom reason here"
        assert response.error.message == "Custom reason here"


class TestToolContext:
    """Tests for ToolContext."""

    def test_tool_context_defaults(self):
        """Test ToolContext has sensible defaults."""
        ctx = ToolContext()
        assert ctx.request_id is None
        assert ctx.caller_identity is None
        assert ctx.metadata == {}

    def test_tool_context_with_values(self):
        """Test ToolContext with values."""
        ctx = ToolContext(
            request_id="req-123",
            caller_identity="agent@example.com",
            metadata={"key": "value"},
        )
        assert ctx.request_id == "req-123"
        assert ctx.caller_identity == "agent@example.com"
        assert ctx.metadata["key"] == "value"


class TestADCPHandler:
    """Tests for base ADCPHandler."""

    @pytest.mark.asyncio
    async def test_default_get_products_returns_not_supported(self):
        """Test default get_products returns not supported."""
        handler = ADCPHandler()
        result = await handler.get_products({})
        assert isinstance(result, NotImplementedResponse)
        assert result.supported is False
        assert "get_products" in result.reason

    @pytest.mark.asyncio
    async def test_default_create_media_buy_returns_not_supported(self):
        """Test default create_media_buy returns not supported."""
        handler = ADCPHandler()
        result = await handler.create_media_buy({})
        assert isinstance(result, NotImplementedResponse)
        assert result.supported is False

    @pytest.mark.asyncio
    async def test_default_content_standards_returns_not_supported(self):
        """Test default content standards methods return not supported."""
        handler = ADCPHandler()

        result = await handler.create_content_standards({})
        assert isinstance(result, NotImplementedResponse)

        result = await handler.calibrate_content({})
        assert isinstance(result, NotImplementedResponse)

    @pytest.mark.asyncio
    async def test_default_si_methods_return_not_supported(self):
        """Test default sponsored intelligence methods return not supported."""
        handler = ADCPHandler()

        result = await handler.si_get_offering({})
        assert isinstance(result, NotImplementedResponse)

        result = await handler.si_initiate_session({})
        assert isinstance(result, NotImplementedResponse)


class TestContentStandardsHandler:
    """Tests for ContentStandardsHandler."""

    def create_concrete_handler(self):
        """Create a concrete handler for testing."""

        class ConcreteCSHandler(ContentStandardsHandler):
            async def handle_create_content_standards(self, request, context=None):
                return validate_union(CreateContentStandardsResponse, {"standards_id": "test"})

            async def handle_get_content_standards(self, request, context=None):
                return validate_union(GetContentStandardsResponse, {"standards_id": "test"})

            async def handle_list_content_standards(self, request, context=None):
                return validate_union(ListContentStandardsResponse, {"standards": []})

            async def handle_update_content_standards(self, request, context=None):
                return validate_union(
                    UpdateContentStandardsResponse,
                    {"standards_id": "test", "success": True},
                )

            async def handle_calibrate_content(self, request, context=None):
                return validate_union(
                    CalibrateContentResponse, {"verdict": "pass"}
                )

            async def handle_validate_content_delivery(self, request, context=None):
                return validate_union(
                    ValidateContentDeliveryResponse,
                    {
                        "results": [],
                        "summary": {
                            "total_records": 0,
                            "passed_records": 0,
                            "failed_records": 0,
                        },
                    },
                )

            async def handle_get_media_buy_artifacts(self, request, context=None):
                return validate_union(
                    GetMediaBuyArtifactsResponse,
                    {"artifacts": [], "media_buy_id": "test"},
                )

        return ConcreteCSHandler()

    @pytest.mark.asyncio
    async def test_get_products_returns_not_supported(self):
        """Test get_products is stubbed as not supported."""
        handler = self.create_concrete_handler()
        result = await handler.get_products({})
        assert isinstance(result, NotImplementedResponse)
        assert result.supported is False
        assert "Content Standards" in result.reason

    @pytest.mark.asyncio
    async def test_create_media_buy_returns_not_supported(self):
        """Test create_media_buy is stubbed as not supported."""
        handler = self.create_concrete_handler()
        result = await handler.create_media_buy({})
        assert isinstance(result, NotImplementedResponse)
        assert "media buying" in result.reason.lower() or "Content Standards" in result.reason

    @pytest.mark.asyncio
    async def test_si_methods_return_not_supported(self):
        """Test SI methods are stubbed as not supported."""
        handler = self.create_concrete_handler()

        result = await handler.si_get_offering({})
        assert isinstance(result, NotImplementedResponse)
        assert "Sponsored Intelligence" in result.reason

    @pytest.mark.asyncio
    async def test_signal_methods_return_not_supported(self):
        """Test signal methods are stubbed as not supported."""
        handler = self.create_concrete_handler()

        result = await handler.get_signals({})
        assert isinstance(result, NotImplementedResponse)

        result = await handler.activate_signal({})
        assert isinstance(result, NotImplementedResponse)

    @pytest.mark.asyncio
    async def test_governance_methods_return_not_supported(self):
        """Test governance methods are stubbed as not supported."""
        handler = self.create_concrete_handler()

        result = await handler.create_property_list({})
        assert isinstance(result, NotImplementedResponse)
        assert "Governance" in result.reason

        result = await handler.list_property_lists({})
        assert isinstance(result, NotImplementedResponse)


class TestSponsoredIntelligenceHandler:
    """Tests for SponsoredIntelligenceHandler."""

    def create_concrete_handler(self):
        """Create a concrete handler for testing."""

        class ConcreteSIHandler(SponsoredIntelligenceHandler):
            async def handle_si_get_offering(self, request, context=None):
                return SiGetOfferingResponse()

            async def handle_si_initiate_session(self, request, context=None):
                return SiInitiateSessionResponse()

            async def handle_si_send_message(self, request, context=None):
                return SiSendMessageResponse()

            async def handle_si_terminate_session(self, request, context=None):
                return SiTerminateSessionResponse()

        return ConcreteSIHandler()

    @pytest.mark.asyncio
    async def test_get_products_returns_not_supported(self):
        """Test get_products is stubbed as not supported."""
        handler = self.create_concrete_handler()
        result = await handler.get_products({})
        assert isinstance(result, NotImplementedResponse)
        assert result.supported is False
        assert "Sponsored Intelligence" in result.reason

    @pytest.mark.asyncio
    async def test_create_media_buy_returns_not_supported(self):
        """Test create_media_buy is stubbed as not supported."""
        handler = self.create_concrete_handler()
        result = await handler.create_media_buy({})
        assert isinstance(result, NotImplementedResponse)
        assert (
            "si_initiate_session" in result.reason.lower()
            or "Sponsored Intelligence" in result.reason
        )

    @pytest.mark.asyncio
    async def test_content_standards_returns_not_supported(self):
        """Test content standards methods are stubbed as not supported."""
        handler = self.create_concrete_handler()

        result = await handler.create_content_standards({})
        assert isinstance(result, NotImplementedResponse)
        assert "Content Standards" in result.reason

        result = await handler.calibrate_content({})
        assert isinstance(result, NotImplementedResponse)

    @pytest.mark.asyncio
    async def test_governance_methods_return_not_supported(self):
        """Test governance methods are stubbed as not supported."""
        handler = self.create_concrete_handler()

        result = await handler.create_property_list({})
        assert isinstance(result, NotImplementedResponse)
        assert "Governance" in result.reason

        result = await handler.list_property_lists({})
        assert isinstance(result, NotImplementedResponse)


class TestGovernanceHandler:
    """Tests for GovernanceHandler."""

    def create_concrete_handler(self):
        """Create a concrete handler for testing."""

        class ConcreteGovHandler(GovernanceHandler):
            async def handle_create_property_list(self, request, context=None):
                return CreatePropertyListResponse()

            async def handle_get_property_list(self, request, context=None):
                return GetPropertyListResponse()

            async def handle_list_property_lists(self, request, context=None):
                return ListPropertyListsResponse(lists=[])

            async def handle_update_property_list(self, request, context=None):
                return UpdatePropertyListResponse()

            async def handle_delete_property_list(self, request, context=None):
                return DeletePropertyListResponse()

        return ConcreteGovHandler()

    @pytest.mark.asyncio
    async def test_get_products_returns_not_supported(self):
        """Test get_products is stubbed as not supported."""
        handler = self.create_concrete_handler()
        result = await handler.get_products({})
        assert isinstance(result, NotImplementedResponse)
        assert result.supported is False
        assert "Governance" in result.reason

    @pytest.mark.asyncio
    async def test_create_media_buy_returns_not_supported(self):
        """Test create_media_buy is stubbed as not supported."""
        handler = self.create_concrete_handler()
        result = await handler.create_media_buy({})
        assert isinstance(result, NotImplementedResponse)
        assert "Governance" in result.reason

    @pytest.mark.asyncio
    async def test_content_standards_returns_not_supported(self):
        """Test content standards methods are stubbed as not supported."""
        handler = self.create_concrete_handler()

        result = await handler.create_content_standards({})
        assert isinstance(result, NotImplementedResponse)
        assert "Content Standards" in result.reason

        result = await handler.calibrate_content({})
        assert isinstance(result, NotImplementedResponse)

    @pytest.mark.asyncio
    async def test_si_methods_return_not_supported(self):
        """Test SI methods are stubbed as not supported."""
        handler = self.create_concrete_handler()

        result = await handler.si_get_offering({})
        assert isinstance(result, NotImplementedResponse)
        assert "Sponsored Intelligence" in result.reason


class TestProposalBuilder:
    """Tests for ProposalBuilder."""

    def test_build_simple_proposal(self):
        """Test building a simple proposal."""
        proposal = ProposalBuilder("Test Campaign").add_allocation("product-1", 100).build()

        assert proposal["name"] == "Test Campaign"
        assert "proposal_id" in proposal
        assert len(proposal["allocations"]) == 1
        assert proposal["allocations"][0]["product_id"] == "product-1"
        assert proposal["allocations"][0]["allocation_percentage"] == 100

    def test_build_multi_allocation_proposal(self):
        """Test building a proposal with multiple allocations."""
        proposal = (
            ProposalBuilder("Multi Product Campaign")
            .with_description("A balanced approach")
            .add_allocation("product-1", 60)
            .with_rationale("High-impact display")
            .add_allocation("product-2", 40)
            .with_rationale("Contextual targeting")
            .build()
        )

        assert proposal["name"] == "Multi Product Campaign"
        assert proposal["description"] == "A balanced approach"
        assert len(proposal["allocations"]) == 2
        assert proposal["allocations"][0]["allocation_percentage"] == 60
        assert proposal["allocations"][0]["rationale"] == "High-impact display"
        assert proposal["allocations"][1]["allocation_percentage"] == 40

    def test_build_proposal_with_budget_guidance(self):
        """Test building proposal with budget guidance."""
        proposal = (
            ProposalBuilder("Budget Campaign")
            .add_allocation("product-1", 100)
            .with_budget_guidance(min=5000, recommended=10000, max=20000)
            .build()
        )

        assert "total_budget_guidance" in proposal
        guidance = proposal["total_budget_guidance"]
        assert guidance["min"] == 5000
        assert guidance["recommended"] == 10000
        assert guidance["max"] == 20000
        assert guidance["currency"] == "USD"

    def test_build_proposal_with_custom_id(self):
        """Test building proposal with custom ID."""
        proposal = (
            ProposalBuilder("Custom ID Campaign", proposal_id="my-custom-id")
            .add_allocation("product-1", 100)
            .build()
        )

        assert proposal["proposal_id"] == "my-custom-id"

    def test_allocation_validation_fails_if_not_100(self):
        """Test that allocations must sum to 100."""
        with pytest.raises(ValueError, match="sum to 100"):
            ProposalBuilder("Bad Campaign").add_allocation("product-1", 50).build()

    def test_allocation_validation_empty_allocations(self):
        """Test that at least one allocation is required."""
        with pytest.raises(ValueError, match="at least one allocation"):
            ProposalBuilder("Empty Campaign").build()

    def test_validate_without_building(self):
        """Test validate method returns errors without raising."""
        builder = ProposalBuilder("Incomplete Campaign").add_allocation("product-1", 50)
        errors = builder.validate()
        assert len(errors) == 1
        assert "sum to 100" in errors[0]

    def test_validate_valid_proposal(self):
        """Test validate returns empty for valid proposal."""
        builder = ProposalBuilder("Valid Campaign").add_allocation("product-1", 100)
        errors = builder.validate()
        assert errors == []


class TestProposalNotSupported:
    """Tests for ProposalNotSupported."""

    def test_proposals_not_supported_helper(self):
        """Test proposals_not_supported helper."""
        response = proposals_not_supported("Custom reason")
        assert isinstance(response, ProposalNotSupported)
        assert response.proposals_supported is False
        assert response.reason == "Custom reason"
        assert response.error.code == "PROPOSALS_NOT_SUPPORTED"


class TestMCPToolSet:
    """Tests for MCPToolSet and create_mcp_tools."""

    def test_create_mcp_tools(self):
        """Test creating MCP tools from a handler."""
        handler = ADCPHandler()
        tools = create_mcp_tools(handler)

        assert isinstance(tools, MCPToolSet)
        assert len(tools.tool_definitions) > 0

    def test_tool_definitions_have_required_fields(self):
        """Test tool definitions have name, description, inputSchema."""
        handler = ADCPHandler()
        tools = create_mcp_tools(handler)

        for tool_def in tools.tool_definitions:
            assert "name" in tool_def
            assert "description" in tool_def
            assert "inputSchema" in tool_def

    def test_get_tool_names(self):
        """Test getting list of tool names."""
        handler = ADCPHandler()
        tools = create_mcp_tools(handler)
        names = tools.get_tool_names()

        # Should include core ADCP operations
        assert "get_products" in names
        assert "create_media_buy" in names
        assert "get_adcp_capabilities" in names

        # Should include V3 Content Standards
        assert "create_content_standards" in names
        assert "calibrate_content" in names

        # Should include V3 Sponsored Intelligence
        assert "si_get_offering" in names
        assert "si_send_message" in names

        # Should include Campaign Governance
        assert "sync_plans" in names
        assert "check_governance" in names
        assert "report_plan_outcome" in names
        assert "get_plan_audit_logs" in names

    @pytest.mark.asyncio
    async def test_call_tool_invokes_handler(self):
        """Test calling a tool invokes the handler method."""
        handler = ADCPHandler()
        tools = create_mcp_tools(handler)

        result = await tools.call_tool("get_products", {})

        # Should return the not_supported response as a dict
        assert result["supported"] is False

    @pytest.mark.asyncio
    async def test_call_unknown_tool_raises(self):
        """Test calling unknown tool raises KeyError."""
        handler = ADCPHandler()
        tools = create_mcp_tools(handler)

        with pytest.raises(KeyError, match="Unknown tool"):
            await tools.call_tool("nonexistent_tool", {})


class TestCampaignGovernanceHandler:
    """Tests for CampaignGovernanceHandler."""

    @pytest.mark.asyncio
    async def test_non_governance_operations_return_not_supported(self):
        """Non-governance operations should return not_supported."""
        from adcp.server import CampaignGovernanceHandler
        from adcp.types.campaign_governance import (
            CheckGovernanceResponse,
            GetPlanAuditLogsResponse,
            ReportPlanOutcomeResponse,
            SyncPlansResponse,
        )

        class MinimalGovernanceHandler(CampaignGovernanceHandler):
            async def handle_sync_plans(self, request, context=None):
                return SyncPlansResponse(plans=[])

            async def handle_check_governance(self, request, context=None):
                return CheckGovernanceResponse(
                    check_id="chk-1",
                    status="approved",
                    binding=request.binding,
                    plan_id=request.plan_id,
                    buyer_campaign_ref=request.buyer_campaign_ref,
                    explanation="OK",
                    mode="enforce",
                )

            async def handle_report_plan_outcome(self, request, context=None):
                return ReportPlanOutcomeResponse(
                    outcome_id="out-1", status="recorded"
                )

            async def handle_get_plan_audit_logs(self, request, context=None):
                return GetPlanAuditLogsResponse(plans=[])

        handler = MinimalGovernanceHandler()

        # Non-governance operations should return not_supported
        result = await handler.get_products({})
        assert result.supported is False

        result = await handler.create_media_buy({})
        assert result.supported is False

        result = await handler.get_signals({})
        assert result.supported is False

    @pytest.mark.asyncio
    async def test_governance_operations_validate_and_delegate(self):
        """Governance operations should validate input and delegate to handle_* methods."""
        from adcp.server import CampaignGovernanceHandler
        from adcp.types.campaign_governance import (
            CheckGovernanceResponse,
            GetPlanAuditLogsResponse,
            ReportPlanOutcomeResponse,
            SyncPlansResponse,
        )

        class TrackingHandler(CampaignGovernanceHandler):
            def __init__(self):
                self.calls = []

            async def handle_sync_plans(self, request, context=None):
                self.calls.append("sync_plans")
                return SyncPlansResponse(plans=[])

            async def handle_check_governance(self, request, context=None):
                self.calls.append("check_governance")
                return CheckGovernanceResponse(
                    check_id="chk-1",
                    status="approved",
                    binding=request.binding,
                    plan_id=request.plan_id,
                    buyer_campaign_ref=request.buyer_campaign_ref,
                    explanation="OK",
                    mode="enforce",
                )

            async def handle_report_plan_outcome(self, request, context=None):
                self.calls.append("report_plan_outcome")
                return ReportPlanOutcomeResponse(
                    outcome_id="out-1", status="recorded"
                )

            async def handle_get_plan_audit_logs(self, request, context=None):
                self.calls.append("get_plan_audit_logs")
                return GetPlanAuditLogsResponse(plans=[])

        handler = TrackingHandler()

        await handler.sync_plans(
            {
                "plans": [
                    {
                        "plan_id": "p1",
                        "brand": {"domain": "test.com"},
                        "objectives": "awareness",
                        "budget": {
                            "total": 10000,
                            "currency": "USD",
                            "authority_level": "agent_full",
                        },
                        "flight": {"start": "2026-04-01", "end": "2026-04-30"},
                    }
                ]
            }
        )
        assert "sync_plans" in handler.calls

        await handler.check_governance(
            {
                "plan_id": "p1",
                "buyer_campaign_ref": "c1",
                "binding": "proposed",
                "caller": "orchestrator",
            }
        )
        assert "check_governance" in handler.calls

        await handler.report_plan_outcome(
            {
                "plan_id": "p1",
                "buyer_campaign_ref": "c1",
                "outcome": "completed",
            }
        )
        assert "report_plan_outcome" in handler.calls

        await handler.get_plan_audit_logs({"plan_ids": ["p1"]})
        assert "get_plan_audit_logs" in handler.calls

    @pytest.mark.asyncio
    async def test_governance_validation_error_returns_not_implemented(self):
        """Invalid input should return NotImplementedResponse with validation error."""
        from adcp.server import CampaignGovernanceHandler
        from adcp.types.campaign_governance import (
            CheckGovernanceResponse,
            GetPlanAuditLogsResponse,
            ReportPlanOutcomeResponse,
            SyncPlansResponse,
        )

        class StubHandler(CampaignGovernanceHandler):
            async def handle_sync_plans(self, request, context=None):
                return SyncPlansResponse(plans=[])

            async def handle_check_governance(self, request, context=None):
                return CheckGovernanceResponse(
                    check_id="x",
                    status="approved",
                    binding="proposed",
                    plan_id="p",
                    buyer_campaign_ref="c",
                    explanation="OK",
                    mode="enforce",
                )

            async def handle_report_plan_outcome(self, request, context=None):
                return ReportPlanOutcomeResponse(
                    outcome_id="x", status="recorded"
                )

            async def handle_get_plan_audit_logs(self, request, context=None):
                return GetPlanAuditLogsResponse(plans=[])

        handler = StubHandler()

        # Missing required 'plans' field
        result = await handler.sync_plans({})
        assert result.supported is False
        assert result.error is not None
        assert result.error.code == "VALIDATION_ERROR"

        # Missing required fields for check_governance
        result = await handler.check_governance({"plan_id": "p1"})
        assert result.supported is False

        # Missing required fields for report_plan_outcome
        result = await handler.report_plan_outcome({})
        assert result.supported is False
        assert result.error is not None
        assert result.error.code == "VALIDATION_ERROR"

        # Invalid type for get_plan_audit_logs
        result = await handler.get_plan_audit_logs({"plan_ids": "not_a_list"})
        assert result.supported is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_validated_request_passed_to_handler(self):
        """Verify the Pydantic-validated request object is passed to handle_*."""
        from adcp.server import CampaignGovernanceHandler
        from adcp.types.campaign_governance import (
            CheckGovernanceRequest,
            CheckGovernanceResponse,
            GetPlanAuditLogsResponse,
            ReportPlanOutcomeResponse,
            SyncPlansRequest,
            SyncPlansResponse,
        )

        class CapturingHandler(CampaignGovernanceHandler):
            def __init__(self):
                self.captured_request = None

            async def handle_sync_plans(self, request, context=None):
                self.captured_request = request
                return SyncPlansResponse(plans=[])

            async def handle_check_governance(self, request, context=None):
                self.captured_request = request
                return CheckGovernanceResponse(
                    check_id="x",
                    status="approved",
                    binding=request.binding,
                    plan_id=request.plan_id,
                    buyer_campaign_ref=request.buyer_campaign_ref,
                    explanation="OK",
                    mode="enforce",
                )

            async def handle_report_plan_outcome(self, request, context=None):
                self.captured_request = request
                return ReportPlanOutcomeResponse(
                    outcome_id="x", status="recorded"
                )

            async def handle_get_plan_audit_logs(self, request, context=None):
                self.captured_request = request
                return GetPlanAuditLogsResponse(plans=[])

        handler = CapturingHandler()

        await handler.sync_plans(
            {
                "plans": [
                    {
                        "plan_id": "p1",
                        "brand": {"domain": "test.com"},
                        "objectives": "awareness",
                        "budget": {
                            "total": 10000,
                            "currency": "USD",
                            "authority_level": "agent_full",
                        },
                        "flight": {"start": "2026-04-01", "end": "2026-04-30"},
                    }
                ]
            }
        )
        assert isinstance(handler.captured_request, SyncPlansRequest)
        assert handler.captured_request.plans[0].plan_id == "p1"
        assert handler.captured_request.plans[0].budget.total == 10000

        await handler.check_governance(
            {
                "plan_id": "p1",
                "buyer_campaign_ref": "c1",
                "binding": "proposed",
                "caller": "orchestrator",
                "tool": "create_media_buy",
            }
        )
        assert isinstance(handler.captured_request, CheckGovernanceRequest)
        assert handler.captured_request.binding == "proposed"
        assert handler.captured_request.tool == "create_media_buy"

    @pytest.mark.asyncio
    async def test_campaign_governance_via_mcp_toolset(self):
        """CampaignGovernanceHandler should work with MCPToolSet."""
        from adcp.server import CampaignGovernanceHandler, create_mcp_tools
        from adcp.types.campaign_governance import (
            CheckGovernanceResponse,
            GetPlanAuditLogsResponse,
            ReportPlanOutcomeResponse,
            SyncPlansResponse,
        )

        class TestHandler(CampaignGovernanceHandler):
            async def handle_sync_plans(self, request, context=None):
                return SyncPlansResponse(plans=[])

            async def handle_check_governance(self, request, context=None):
                return CheckGovernanceResponse(
                    check_id="chk-1",
                    status="approved",
                    binding=request.binding,
                    plan_id=request.plan_id,
                    buyer_campaign_ref=request.buyer_campaign_ref,
                    explanation="OK",
                    mode="enforce",
                )

            async def handle_report_plan_outcome(self, request, context=None):
                return ReportPlanOutcomeResponse(
                    outcome_id="out-1", status="recorded"
                )

            async def handle_get_plan_audit_logs(self, request, context=None):
                return GetPlanAuditLogsResponse(plans=[])

        handler = TestHandler()
        tools = create_mcp_tools(handler)

        # sync_plans via MCP tool
        result = await tools.call_tool(
            "sync_plans",
            {
                "plans": [
                    {
                        "plan_id": "p1",
                        "brand": {"domain": "test.com"},
                        "objectives": "awareness",
                        "budget": {
                            "total": 10000,
                            "currency": "USD",
                            "authority_level": "agent_full",
                        },
                        "flight": {"start": "2026-04-01", "end": "2026-04-30"},
                    }
                ]
            },
        )
        assert result["plans"] == []

        # check_governance via MCP tool
        result = await tools.call_tool(
            "check_governance",
            {
                "plan_id": "p1",
                "buyer_campaign_ref": "c1",
                "binding": "proposed",
                "caller": "orchestrator",
            },
        )
        assert result["status"] == "approved"
        assert result["check_id"] == "chk-1"

        # Non-governance tools should return not_supported
        result = await tools.call_tool("get_products", {})
        assert result["supported"] is False


class TestServerModuleExports:
    """Test that server module exports are correct."""

    def test_all_exports_available(self):
        """Test all expected exports are available from adcp.server."""
        from adcp.server import (
            ADCPHandler,
            CampaignGovernanceHandler,
            ContentStandardsHandler,
            NotImplementedResponse,
            ProposalBuilder,
            ProposalNotSupported,
            SponsoredIntelligenceHandler,
            ToolContext,
            create_mcp_tools,
            not_supported,
        )

        # Just verify they're importable and are the right types
        assert ADCPHandler is not None
        assert CampaignGovernanceHandler is not None
        assert ContentStandardsHandler is not None
        assert SponsoredIntelligenceHandler is not None
        assert ProposalBuilder is not None
        assert ProposalNotSupported is not None
        assert NotImplementedResponse is not None
        assert ToolContext is not None
        assert create_mcp_tools is not None
        assert not_supported is not None
