"""Tests for .summary() method on response types.

These tests verify that response types return human-readable messages
suitable for MCP tool results, A2A task communications, and REST API responses.
"""

from __future__ import annotations

from adcp.types._generated import (
    ActivateSignalResponse1,
    ActivateSignalResponse2,
    BuildCreativeResponse1,
    BuildCreativeResponse2,
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
    GetMediaBuyDeliveryResponse,
    GetProductsResponse,
    GetSignalsResponse,
    ListAuthorizedPropertiesResponse,
    ListCreativeFormatsResponse,
    ListCreativesResponse,
    PreviewCreativeResponse1,
    PreviewCreativeResponse2,
    ProvidePerformanceFeedbackResponse1,
    ProvidePerformanceFeedbackResponse2,
    SyncCreativesResponse1,
    SyncCreativesResponse2,
    UpdateMediaBuyResponse1,
    UpdateMediaBuyResponse2,
)


class TestGetProductsResponseMessage:
    """Tests for GetProductsResponse.summary()."""

    def test_singular_product(self):
        """Single product uses singular form."""
        response = GetProductsResponse.model_construct(
            products=[{"product_id": "p1", "name": "Test"}]
        )
        assert response.summary() == "Found 1 product matching your requirements."

    def test_multiple_products(self):
        """Multiple products uses plural form."""
        response = GetProductsResponse.model_construct(
            products=[
                {"product_id": "p1", "name": "Test 1"},
                {"product_id": "p2", "name": "Test 2"},
                {"product_id": "p3", "name": "Test 3"},
            ]
        )
        assert response.summary() == "Found 3 products matching your requirements."

    def test_zero_products(self):
        """Zero products uses conversational message."""
        response = GetProductsResponse.model_construct(products=[])
        assert response.summary() == "No products matched your requirements."


class TestListCreativeFormatsResponseMessage:
    """Tests for ListCreativeFormatsResponse.summary()."""

    def test_singular_format(self):
        """Single format uses singular form."""
        response = ListCreativeFormatsResponse.model_construct(
            formats=[{"format_id": "f1", "name": "Banner"}]
        )
        assert response.summary() == "Found 1 supported creative format."

    def test_multiple_formats(self):
        """Multiple formats uses plural form."""
        response = ListCreativeFormatsResponse.model_construct(
            formats=[
                {"format_id": "f1", "name": "Banner 1"},
                {"format_id": "f2", "name": "Banner 2"},
            ]
        )
        assert response.summary() == "Found 2 supported creative formats."


class TestGetSignalsResponseMessage:
    """Tests for GetSignalsResponse.summary()."""

    def test_singular_signal(self):
        """Single signal uses singular form."""
        response = GetSignalsResponse.model_construct(signals=[{"signal_id": "s1"}])
        assert response.summary() == "Found 1 signal available for targeting."

    def test_multiple_signals(self):
        """Multiple signals uses plural form."""
        response = GetSignalsResponse.model_construct(
            signals=[{"signal_id": "s1"}, {"signal_id": "s2"}]
        )
        assert response.summary() == "Found 2 signals available for targeting."


class TestListAuthorizedPropertiesResponseMessage:
    """Tests for ListAuthorizedPropertiesResponse.summary()."""

    def test_singular_domain(self):
        """Single domain uses singular form."""
        response = ListAuthorizedPropertiesResponse.model_construct(
            publisher_domains=["example.com"]
        )
        assert response.summary() == "Authorized to represent 1 publisher domain."

    def test_multiple_domains(self):
        """Multiple domains uses plural form."""
        response = ListAuthorizedPropertiesResponse.model_construct(
            publisher_domains=["example.com", "test.com", "demo.com"]
        )
        assert response.summary() == "Authorized to represent 3 publisher domains."


class TestListCreativesResponseMessage:
    """Tests for ListCreativesResponse.summary()."""

    def test_singular_creative(self):
        """Single creative uses singular form."""
        response = ListCreativesResponse.model_construct(
            creatives=[{"creative_id": "c1"}]
        )
        assert response.summary() == "Found 1 creative in the system."

    def test_multiple_creatives(self):
        """Multiple creatives uses plural form."""
        response = ListCreativesResponse.model_construct(
            creatives=[{"creative_id": "c1"}, {"creative_id": "c2"}]
        )
        assert response.summary() == "Found 2 creatives in the system."


class TestCreateMediaBuyResponseMessage:
    """Tests for CreateMediaBuyResponse success/error variants."""

    def test_success_singular_package(self):
        """Success with single package."""
        response = CreateMediaBuyResponse1.model_construct(
            media_buy_id="mb_123",
            buyer_ref="ref_456",
            packages=[{"package_id": "pkg_1"}],
        )
        assert response.summary() == "Media buy mb_123 created with 1 package."

    def test_success_multiple_packages(self):
        """Success with multiple packages."""
        response = CreateMediaBuyResponse1.model_construct(
            media_buy_id="mb_456",
            buyer_ref="ref_789",
            packages=[{"package_id": "pkg_1"}, {"package_id": "pkg_2"}],
        )
        assert response.summary() == "Media buy mb_456 created with 2 packages."

    def test_error_singular(self):
        """Error with single error."""
        response = CreateMediaBuyResponse2.model_construct(
            errors=[{"code": "invalid", "message": "Failed"}]
        )
        assert response.summary() == "Media buy creation failed with 1 error."

    def test_error_multiple(self):
        """Error with multiple errors."""
        response = CreateMediaBuyResponse2.model_construct(
            errors=[
                {"code": "invalid", "message": "Error 1"},
                {"code": "invalid", "message": "Error 2"},
            ]
        )
        assert response.summary() == "Media buy creation failed with 2 errors."


class TestUpdateMediaBuyResponseMessage:
    """Tests for UpdateMediaBuyResponse success/error variants."""

    def test_success(self):
        """Success message includes media buy ID."""
        response = UpdateMediaBuyResponse1.model_construct(
            media_buy_id="mb_789",
            packages=[],
        )
        assert response.summary() == "Media buy mb_789 updated successfully."

    def test_error(self):
        """Error message includes error count."""
        response = UpdateMediaBuyResponse2.model_construct(
            errors=[{"code": "not_found", "message": "Not found"}]
        )
        assert response.summary() == "Media buy update failed with 1 error."


class TestSyncCreativesResponseMessage:
    """Tests for SyncCreativesResponse success/error variants."""

    def test_success_singular(self):
        """Success with single creative synced."""
        response = SyncCreativesResponse1.model_construct(
            creatives=[{"creative_id": "c1", "action": "created"}]
        )
        assert response.summary() == "Synced 1 creative successfully."

    def test_success_multiple(self):
        """Success with multiple creatives synced."""
        response = SyncCreativesResponse1.model_construct(
            creatives=[
                {"creative_id": "c1", "action": "created"},
                {"creative_id": "c2", "action": "updated"},
                {"creative_id": "c3", "action": "created"},
            ]
        )
        assert response.summary() == "Synced 3 creatives successfully."

    def test_error(self):
        """Error message includes error count."""
        response = SyncCreativesResponse2.model_construct(
            errors=[{"code": "sync_failed", "message": "Failed"}]
        )
        assert response.summary() == "Creative sync failed with 1 error."


class TestActivateSignalResponseMessage:
    """Tests for ActivateSignalResponse success/error variants."""

    def test_success(self):
        """Success message is simple confirmation."""
        response = ActivateSignalResponse1.model_construct(
            activation_status="active"
        )
        assert response.summary() == "Signal activated successfully."

    def test_error(self):
        """Error message includes error count."""
        response = ActivateSignalResponse2.model_construct(
            errors=[{"code": "activation_failed", "message": "Failed"}]
        )
        assert response.summary() == "Signal activation failed with 1 error."


class TestPreviewCreativeResponseMessage:
    """Tests for PreviewCreativeResponse single/batch variants."""

    def test_single_singular(self):
        """Single request with one preview."""
        response = PreviewCreativeResponse1.model_construct(
            response_type="single",
            expires_at="2025-12-01T00:00:00Z",
            previews=[{"preview_id": "p1"}],
        )
        assert response.summary() == "Generated 1 preview."

    def test_single_multiple(self):
        """Single request with multiple previews."""
        response = PreviewCreativeResponse1.model_construct(
            response_type="single",
            expires_at="2025-12-01T00:00:00Z",
            previews=[{"preview_id": "p1"}, {"preview_id": "p2"}],
        )
        assert response.summary() == "Generated 2 previews."

    def test_batch_singular(self):
        """Batch request with one manifest."""
        response = PreviewCreativeResponse2.model_construct(
            response_type="batch",
            results=[{"manifest_id": "m1"}],
        )
        assert response.summary() == "Generated previews for 1 manifest."

    def test_batch_multiple(self):
        """Batch request with multiple manifests."""
        response = PreviewCreativeResponse2.model_construct(
            response_type="batch",
            results=[{"manifest_id": "m1"}, {"manifest_id": "m2"}],
        )
        assert response.summary() == "Generated previews for 2 manifests."


class TestBuildCreativeResponseMessage:
    """Tests for BuildCreativeResponse success/error variants."""

    def test_success(self):
        """Success message is simple confirmation."""
        response = BuildCreativeResponse1.model_construct(
            assets=[{"url": "https://example.com/asset"}]
        )
        assert response.summary() == "Creative built successfully."

    def test_error(self):
        """Error message includes error count."""
        response = BuildCreativeResponse2.model_construct(
            errors=[{"code": "build_failed", "message": "Failed"}]
        )
        assert response.summary() == "Creative build failed with 1 error."


class TestGetMediaBuyDeliveryResponseMessage:
    """Tests for GetMediaBuyDeliveryResponse.summary()."""

    def test_with_single_media_buy(self):
        """Response with single media buy delivery data."""
        response = GetMediaBuyDeliveryResponse.model_construct(
            media_buy_deliveries=[{"media_buy_id": "mb_123"}]
        )
        assert response.summary() == "Retrieved delivery data for 1 media buy."

    def test_with_multiple_media_buys(self):
        """Response with multiple media buy delivery data."""
        response = GetMediaBuyDeliveryResponse.model_construct(
            media_buy_deliveries=[
                {"media_buy_id": "mb_123"},
                {"media_buy_id": "mb_456"},
            ]
        )
        assert response.summary() == "Retrieved delivery data for 2 media buys."


class TestProvidePerformanceFeedbackResponseMessage:
    """Tests for ProvidePerformanceFeedbackResponse success/error variants."""

    def test_success(self):
        """Success message is simple confirmation."""
        response = ProvidePerformanceFeedbackResponse1.model_construct(
            acknowledged=True
        )
        assert response.summary() == "Performance feedback recorded successfully."

    def test_error(self):
        """Error message includes error count."""
        response = ProvidePerformanceFeedbackResponse2.model_construct(
            errors=[{"code": "feedback_failed", "message": "Failed"}]
        )
        assert response.summary() == "Performance feedback recording failed with 1 error."


class TestNonResponseTypeMessage:
    """Tests for .summary() on non-response types."""

    def test_request_type_returns_generic_message(self):
        """Request types return generic message with class name."""
        from adcp.types import GetProductsRequest

        request = GetProductsRequest(brief="Test brief")
        assert request.summary() == "GetProductsRequest response"

    def test_str_returns_pydantic_default(self):
        """str() returns Pydantic's default representation for inspection."""
        from adcp.types import GetProductsRequest

        request = GetProductsRequest(brief="Test brief")
        result = str(request)
        # Should be Pydantic's default format, not a custom message
        assert "GetProductsRequest" in result or "brief=" in result
