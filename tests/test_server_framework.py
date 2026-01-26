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
                return CreateContentStandardsResponse()

            async def handle_get_content_standards(self, request, context=None):
                return GetContentStandardsResponse()

            async def handle_list_content_standards(self, request, context=None):
                return ListContentStandardsResponse()

            async def handle_update_content_standards(self, request, context=None):
                return UpdateContentStandardsResponse()

            async def handle_calibrate_content(self, request, context=None):
                return CalibrateContentResponse()

            async def handle_validate_content_delivery(self, request, context=None):
                return ValidateContentDeliveryResponse()

            async def handle_get_media_buy_artifacts(self, request, context=None):
                return GetMediaBuyArtifactsResponse()

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

    @pytest.mark.asyncio
    async def test_list_authorized_properties_suggests_alternative(self):
        """Test list_authorized_properties suggests using get_property_list."""
        handler = self.create_concrete_handler()
        result = await handler.list_authorized_properties({})
        assert isinstance(result, NotImplementedResponse)
        assert "get_property_list" in result.reason


class TestProposalBuilder:
    """Tests for ProposalBuilder."""

    def test_build_simple_proposal(self):
        """Test building a simple proposal."""
        proposal = (
            ProposalBuilder("Test Campaign")
            .add_allocation("product-1", 100)
            .build()
        )

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


class TestServerModuleExports:
    """Test that server module exports are correct."""

    def test_all_exports_available(self):
        """Test all expected exports are available from adcp.server."""
        from adcp.server import (
            ADCPHandler,
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
        assert ContentStandardsHandler is not None
        assert SponsoredIntelligenceHandler is not None
        assert ProposalBuilder is not None
        assert ProposalNotSupported is not None
        assert NotImplementedResponse is not None
        assert ToolContext is not None
        assert create_mcp_tools is not None
        assert not_supported is not None
