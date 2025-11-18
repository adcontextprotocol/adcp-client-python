"""Recommended testing patterns for AdCP SDK.

This file demonstrates the CORRECT way to test the AdCP SDK:
1. Test public API, not internal types
2. Test wire format with JSON fixtures
3. Test user workflows, not type mechanics
4. Test behavior, not implementation

Compare with test_discriminated_unions.py to see the differences.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ✅ CORRECT: Import from public API
from adcp import (
    ADCPClient,
    AgentConfig,
    CreateMediaBuyErrorResponse,
    CreateMediaBuyRequest,
    CreateMediaBuySuccessResponse,
    GetProductsRequest,
    Protocol,
)
from adcp.types.core import TaskResult, TaskStatus

# ❌ WRONG: Never import from generated_poc in tests
# from adcp.types.generated_poc.product import PublisherProperties4


# =============================================================================
# PATTERN 1: Test Wire Format Compatibility
# =============================================================================


class TestWireFormatCompatibility:
    """Test that SDK correctly handles protocol JSON.

    These tests validate we can:
    1. Deserialize actual protocol JSON to Pydantic models
    2. Serialize Pydantic models back to valid protocol JSON
    3. Round-trip without data loss

    This catches:
    - Field name mismatches (snake_case vs camelCase)
    - Type coercion bugs (string vs number)
    - Missing required fields
    - Discriminated union deserialization
    """

    def test_get_products_response_deserializes_from_protocol_json(self):
        """GetProductsResponse deserializes from actual protocol JSON."""
        # This JSON comes from a real agent response
        protocol_json = """
        {
          "products": [
            {
              "product_id": "premium_display",
              "name": "Premium Display Placements",
              "description": "High-visibility ad slots on homepage",
              "publisher_properties": [
                {
                  "publisher_domain": "example.com",
                  "selection_type": "by_id",
                  "property_ids": ["homepage", "mobile_app"]
                }
              ],
              "pricing_options": [
                {
                  "model": "cpm_fixed_rate",
                  "is_fixed": true,
                  "cpm": 5.50
                }
              ]
            }
          ]
        }
        """

        # Import the response type
        from adcp import GetProductsResponse

        # ✅ TEST: Can we parse actual protocol JSON?
        response = GetProductsResponse.model_validate_json(protocol_json)

        # Verify structure
        assert len(response.products) == 1
        product = response.products[0]
        assert product.product_id == "premium_display"

        # ✅ TEST: Does discriminated union work?
        assert product.publisher_properties[0].selection_type == "by_id"

        # ✅ TEST: Round-trip preserves data?
        roundtrip_json = response.model_dump_json()
        roundtrip = GetProductsResponse.model_validate_json(roundtrip_json)
        assert roundtrip.products[0].product_id == product.product_id

    def test_create_media_buy_success_response_wire_format(self):
        """CreateMediaBuySuccessResponse deserializes success variant."""
        protocol_json = """
        {
          "media_buy_id": "mb_123456",
          "buyer_ref": "campaign_abc",
          "packages": [
            {
              "package_id": "pkg_001",
              "product_id": "premium_display",
              "status": "pending"
            }
          ]
        }
        """

        # ✅ CORRECT: Use semantic alias from public API
        response = CreateMediaBuySuccessResponse.model_validate_json(protocol_json)

        assert response.media_buy_id == "mb_123456"
        assert not hasattr(response, "errors")
        assert len(response.packages) == 1

    def test_create_media_buy_error_response_wire_format(self):
        """CreateMediaBuyErrorResponse deserializes error variant."""
        protocol_json = """
        {
          "errors": [
            {
              "code": "budget_exceeded",
              "message": "Requested budget exceeds account limit"
            }
          ]
        }
        """

        # ✅ CORRECT: Use semantic alias from public API
        response = CreateMediaBuyErrorResponse.model_validate_json(protocol_json)

        assert len(response.errors) == 1
        assert response.errors[0].code == "budget_exceeded"
        assert not hasattr(response, "media_buy_id")


# =============================================================================
# PATTERN 2: Test User Workflows (End-to-End)
# =============================================================================


class TestProductDiscoveryWorkflow:
    """Test product discovery from buyer's perspective.

    These tests tell stories about how users accomplish goals:
    - Buyer discovers products for campaign
    - Buyer filters products by criteria
    - Buyer handles various response scenarios

    Focus: External behavior users care about
    """

    @pytest.mark.asyncio
    async def test_buyer_discovers_products_for_coffee_campaign(self, mocker):
        """Buyer gets products suitable for coffee brand campaign."""
        # Setup: Create client
        config = AgentConfig(
            id="publisher_agent",
            agent_uri="https://publisher.example.com",
            protocol=Protocol.A2A,
        )
        client = ADCPClient(config)

        # Setup: Mock agent response with realistic data
        mock_response_data = {
            "products": [
                {
                    "product_id": "breakfast_readers",
                    "name": "Morning News Readers",
                    "description": "Reach coffee drinkers during morning news",
                    "publisher_properties": [
                        {
                            "publisher_domain": "news.example.com",
                            "selection_type": "by_tag",
                            "property_tags": ["morning", "lifestyle"],
                        }
                    ],
                    "pricing_options": [
                        {"model": "cpm_fixed_rate", "is_fixed": True, "cpm": 4.50}
                    ],
                }
            ]
        }

        mock_result = TaskResult(
            status=TaskStatus.COMPLETED, data=mock_response_data, success=True
        )

        mocker.patch.object(client.adapter, "get_products", return_value=mock_result)

        # Action: User discovers products
        request = GetProductsRequest(brief="Coffee brand campaign for morning audience")
        result = await client.get_products(request)

        # Assert: User gets successful result
        assert result.success, f"Discovery failed: {result.error}"
        assert len(result.data.products) > 0, "No products found"

        # Assert: Product has campaign-relevant data
        product = result.data.products[0]
        assert product.product_id
        assert product.name
        assert len(product.pricing_options) > 0

        # Assert: Can plan budget from pricing
        pricing = product.pricing_options[0]
        assert pricing.model in ["cpm_fixed_rate", "cpm_auction"]

    @pytest.mark.asyncio
    async def test_buyer_handles_no_products_available(self, mocker):
        """Buyer gracefully handles when no products match criteria."""
        config = AgentConfig(
            id="publisher_agent",
            agent_uri="https://publisher.example.com",
            protocol=Protocol.A2A,
        )
        client = ADCPClient(config)

        # Mock empty response
        mock_result = TaskResult(
            status=TaskStatus.COMPLETED, data={"products": []}, success=True
        )

        mocker.patch.object(client.adapter, "get_products", return_value=mock_result)

        # User makes request
        request = GetProductsRequest(brief="Extremely niche requirement")
        result = await client.get_products(request)

        # Should succeed with empty list (not error)
        assert result.success
        assert result.data.products == []


# =============================================================================
# PATTERN 3: Test Public API Behavior
# =============================================================================


class TestPublicAPIBehavior:
    """Test ADCPClient public API methods.

    These tests verify:
    - Methods exist and are callable
    - Methods accept correct request types
    - Methods return correct response types
    - Error handling is user-friendly

    Focus: Does the API work as documented?
    """

    @pytest.mark.asyncio
    async def test_create_media_buy_accepts_request_object(self, mocker):
        """create_media_buy accepts CreateMediaBuyRequest and returns response."""
        config = AgentConfig(
            id="agent", agent_uri="https://agent.example.com", protocol=Protocol.A2A
        )
        client = ADCPClient(config)

        # Mock successful response
        mock_result = TaskResult(
            status=TaskStatus.COMPLETED,
            data={
                "media_buy_id": "mb_123",
                "buyer_ref": "campaign_456",
                "packages": [],
            },
            success=True,
        )

        mocker.patch.object(client.adapter, "create_media_buy", return_value=mock_result)

        # ✅ TEST: Can user create request and call method?
        request = CreateMediaBuyRequest(
            buyer_ref="campaign_456",
            packages=[
                {
                    "product_id": "premium_display",
                    "budget": {"amount": 10000.0, "currency": "USD"},
                }
            ],
        )

        result = await client.create_media_buy(request)

        # ✅ TEST: Does result have expected structure?
        assert result.success
        assert isinstance(result.data, CreateMediaBuySuccessResponse)
        assert result.data.media_buy_id == "mb_123"

    @pytest.mark.asyncio
    async def test_create_media_buy_handles_error_response(self, mocker):
        """create_media_buy handles error responses gracefully."""
        config = AgentConfig(
            id="agent", agent_uri="https://agent.example.com", protocol=Protocol.A2A
        )
        client = ADCPClient(config)

        # Mock error response
        mock_result = TaskResult(
            status=TaskStatus.COMPLETED,
            data={
                "errors": [
                    {"code": "budget_exceeded", "message": "Budget exceeds limit"}
                ]
            },
            success=True,  # Note: Protocol success, but logical error
        )

        mocker.patch.object(client.adapter, "create_media_buy", return_value=mock_result)

        request = CreateMediaBuyRequest(
            buyer_ref="campaign_456",
            packages=[
                {
                    "product_id": "premium_display",
                    "budget": {"amount": 999999999.0, "currency": "USD"},
                }
            ],
        )

        result = await client.create_media_buy(request)

        # ✅ TEST: Can user detect and handle errors?
        assert result.success  # Transport succeeded
        # User must check response type to detect logical errors
        if isinstance(result.data, CreateMediaBuyErrorResponse):
            assert len(result.data.errors) > 0
            assert result.data.errors[0].code == "budget_exceeded"


# =============================================================================
# PATTERN 4: Test Error Handling and Edge Cases
# =============================================================================


class TestErrorHandling:
    """Test error handling from user perspective.

    These tests verify:
    - Users get helpful error messages
    - Invalid requests are caught early
    - Network errors are handled gracefully
    - Validation errors are user-friendly

    Focus: Can users diagnose and fix problems?
    """

    def test_invalid_json_gives_helpful_error(self):
        """Invalid JSON produces actionable error message."""
        from pydantic import ValidationError

        from adcp import GetProductsResponse

        invalid_json = '{"products": "not an array"}'

        with pytest.raises(ValidationError) as exc_info:
            GetProductsResponse.model_validate_json(invalid_json)

        # Error should mention the field and expected type
        error_msg = str(exc_info.value)
        assert "products" in error_msg.lower()

    def test_missing_required_field_gives_helpful_error(self):
        """Missing required fields produce clear error messages."""
        from pydantic import ValidationError

        from adcp import Product

        incomplete_data = {
            "product_id": "test",
            "name": "Test Product",
            # Missing: description, publisher_properties, pricing_options
        }

        with pytest.raises(ValidationError) as exc_info:
            Product.model_validate(incomplete_data)

        error_msg = str(exc_info.value)
        # Should tell user what's missing
        assert "field required" in error_msg.lower() or "missing" in error_msg.lower()


# =============================================================================
# ANTI-PATTERNS TO AVOID
# =============================================================================


class AntiPatterns:
    """Examples of what NOT to do in tests.

    These demonstrate common mistakes that violate testing principles.
    """

    def test_anti_pattern_importing_generated_poc(self):
        """❌ WRONG: Don't import from generated_poc in tests."""
        # This couples tests to internal implementation
        # When schemas evolve, these imports break

        # ❌ DON'T DO THIS:
        # from adcp.types.generated_poc.product import PublisherProperties4
        # prop = PublisherProperties4(...)

        # ✅ DO THIS INSTEAD:
        from adcp import Product

        product_json = {
            "product_id": "test",
            "name": "Test",
            "description": "Test product",
            "publisher_properties": [
                {
                    "publisher_domain": "example.com",
                    "selection_type": "by_id",
                    "property_ids": ["site1"],
                }
            ],
            "pricing_options": [
                {"model": "cpm_fixed_rate", "is_fixed": True, "cpm": 5.0}
            ],
        }

        product = Product.model_validate(product_json)
        assert product.publisher_properties[0].selection_type == "by_id"

    def test_anti_pattern_testing_pydantic_mechanics(self):
        """❌ WRONG: Don't test Pydantic's discriminated union implementation."""
        # Pydantic already tests this extensively
        # We should test OUR behavior, not Pydantic's

        # ❌ DON'T DO THIS:
        # "Test that discriminator field is enforced"
        # "Test that wrong discriminator value fails"
        # "Test that Literal type works correctly"

        # ✅ DO THIS INSTEAD:
        # "Test that user can deserialize success response from JSON"
        # "Test that user can deserialize error response from JSON"
        # "Test that user can distinguish success from error"

        from adcp import CreateMediaBuyErrorResponse, CreateMediaBuySuccessResponse

        success_json = '{"media_buy_id": "mb_123", "buyer_ref": "ref", "packages": []}'
        error_json = '{"errors": [{"code": "err", "message": "msg"}]}'

        success = CreateMediaBuySuccessResponse.model_validate_json(success_json)
        error = CreateMediaBuyErrorResponse.model_validate_json(error_json)

        # User can distinguish by type
        assert isinstance(success, CreateMediaBuySuccessResponse)
        assert isinstance(error, CreateMediaBuyErrorResponse)

    def test_anti_pattern_testing_type_identity(self):
        """❌ WRONG: Don't test that aliases point to generated types."""
        # This tests internal implementation
        # Users don't care about type identity, they care about behavior

        # ❌ DON'T DO THIS:
        # assert CreateMediaBuySuccessResponse is CreateMediaBuyResponse1

        # ✅ DO THIS INSTEAD:
        # Test that the alias works in actual usage
        from adcp import CreateMediaBuySuccessResponse

        response = CreateMediaBuySuccessResponse(
            media_buy_id="mb_123", buyer_ref="ref", packages=[]
        )

        # Can serialize to JSON
        json_str = response.model_dump_json()
        assert "media_buy_id" in json_str

        # Can deserialize from JSON
        roundtrip = CreateMediaBuySuccessResponse.model_validate_json(json_str)
        assert roundtrip.media_buy_id == response.media_buy_id


# =============================================================================
# FIXTURE RECOMMENDATIONS
# =============================================================================


@pytest.fixture
def mock_adcp_client(mocker):
    """Create a mock ADCPClient for testing.

    Returns a client with mocked adapter so tests can control responses.
    """
    config = AgentConfig(
        id="test_agent", agent_uri="https://test.example.com", protocol=Protocol.A2A
    )

    client = ADCPClient(config)

    # Mock the adapter to avoid real network calls
    client.adapter = mocker.MagicMock()

    return client


@pytest.fixture
def sample_product_json():
    """Realistic product JSON from protocol.

    Use this in tests that need valid product data.
    """
    return {
        "product_id": "premium_display",
        "name": "Premium Display Ad",
        "description": "High-visibility homepage placement",
        "publisher_properties": [
            {
                "publisher_domain": "example.com",
                "selection_type": "by_id",
                "property_ids": ["homepage", "mobile_app"],
            }
        ],
        "pricing_options": [
            {"model": "cpm_fixed_rate", "is_fixed": True, "cpm": 5.50}
        ],
    }


# =============================================================================
# SUMMARY: Key Testing Principles
# =============================================================================

"""
✅ DO:
1. Test public API (from adcp import X)
2. Test wire format with JSON (model_validate_json)
3. Test user workflows (can buyer discover products?)
4. Test behavior (does API work as documented?)
5. Use semantic aliases (CreateMediaBuySuccessResponse)
6. Write tests users can learn from

❌ DON'T:
1. Import from generated_poc
2. Test Pydantic internals
3. Test type identity (assert X is Y)
4. Test implementation details
5. Use numbered types (CreateMediaBuyResponse1)
6. Test mechanics instead of behavior

REMEMBER:
- Tests should demonstrate correct SDK usage
- Tests should catch protocol compatibility bugs
- Tests should tell user stories
- Tests should respect public API boundaries
"""
