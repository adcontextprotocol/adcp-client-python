"""Tests for discriminated union types."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.types.generated import (
    ActivateSignalError,
    ActivateSignalSuccess,
    AgentDeployment,
    AgentDestination,
    CreateMediaBuyError,
    CreateMediaBuySuccess,
    Error,
    MediaSubAsset,
    PlatformDeployment,
    PlatformDestination,
    SyncCreativesError,
    SyncCreativesSuccess,
    TextSubAsset,
    UpdateMediaBuyError,
    UpdateMediaBuySuccess,
)


class TestResponseUnions:
    """Test discriminated union response types."""

    def test_create_media_buy_success_variant(self):
        """CreateMediaBuySuccess should validate with required fields."""
        success = CreateMediaBuySuccess(
            media_buy_id="mb_123",
            buyer_ref="ref_456",
            packages=[],
        )
        assert success.media_buy_id == "mb_123"
        assert success.buyer_ref == "ref_456"
        assert not hasattr(success, "errors")

    def test_create_media_buy_error_variant(self):
        """CreateMediaBuyError should validate with errors field."""
        error = CreateMediaBuyError(
            errors=[Error(code="invalid_budget", message="Budget too low")]
        )
        assert len(error.errors) == 1
        assert error.errors[0].code == "invalid_budget"
        assert not hasattr(error, "media_buy_id")

    def test_update_media_buy_success_variant(self):
        """UpdateMediaBuySuccess should validate with required fields."""
        success = UpdateMediaBuySuccess(
            media_buy_id="mb_123",
            buyer_ref="ref_456",
            packages=[],
        )
        assert success.media_buy_id == "mb_123"
        assert success.buyer_ref == "ref_456"
        assert not hasattr(success, "errors")

    def test_update_media_buy_error_variant(self):
        """UpdateMediaBuyError should validate with errors field."""
        error = UpdateMediaBuyError(
            errors=[Error(code="not_found", message="Media buy not found")]
        )
        assert len(error.errors) == 1
        assert not hasattr(error, "media_buy_id")

    def test_activate_signal_success_variant(self):
        """ActivateSignalSuccess should validate with required fields."""
        success = ActivateSignalSuccess(
            decisioning_platform_segment_id="seg_123",
        )
        assert success.decisioning_platform_segment_id == "seg_123"
        assert not hasattr(success, "errors")

    def test_activate_signal_error_variant(self):
        """ActivateSignalError should validate with errors field."""
        error = ActivateSignalError(
            errors=[Error(code="unauthorized", message="Not authorized")]
        )
        assert len(error.errors) == 1
        assert not hasattr(error, "decisioning_platform_segment_id")

    def test_sync_creatives_success_variant(self):
        """SyncCreativesSuccess should validate with required fields."""
        success = SyncCreativesSuccess(
            assignments=[],
        )
        assert len(success.assignments) == 0
        assert not hasattr(success, "errors")

    def test_sync_creatives_error_variant(self):
        """SyncCreativesError should validate with errors field."""
        error = SyncCreativesError(
            errors=[Error(code="sync_failed", message="Sync failed")]
        )
        assert len(error.errors) == 1
        assert not hasattr(error, "assignments")


class TestAssetDiscriminators:
    """Test asset discriminator fields."""

    def test_media_sub_asset_requires_content_uri(self):
        """MediaSubAsset requires content_uri field."""
        asset = MediaSubAsset(
            asset_kind="media",
            asset_type="thumbnail_image",
            asset_id="thumb_1",
            content_uri="https://example.com/image.jpg",
        )
        assert asset.asset_kind == "media"
        assert asset.content_uri == "https://example.com/image.jpg"
        assert not hasattr(asset, "content")

    def test_media_sub_asset_missing_content_uri_fails(self):
        """MediaSubAsset without content_uri should fail validation."""
        with pytest.raises(ValidationError) as exc_info:
            MediaSubAsset(
                asset_kind="media",
                asset_type="thumbnail_image",
                asset_id="thumb_1",
            )
        assert "content_uri" in str(exc_info.value)

    def test_text_sub_asset_requires_content(self):
        """TextSubAsset requires content field."""
        asset = TextSubAsset(
            asset_kind="text",
            asset_type="headline",
            asset_id="headline_1",
            content="Amazing Product!",
        )
        assert asset.asset_kind == "text"
        assert asset.content == "Amazing Product!"
        assert not hasattr(asset, "content_uri")

    def test_text_sub_asset_content_can_be_array(self):
        """TextSubAsset content can be array for A/B testing."""
        asset = TextSubAsset(
            asset_kind="text",
            asset_type="headline",
            asset_id="headline_1",
            content=["Variant A", "Variant B"],
        )
        assert isinstance(asset.content, list)
        assert len(asset.content) == 2

    def test_text_sub_asset_missing_content_fails(self):
        """TextSubAsset without content should fail validation."""
        with pytest.raises(ValidationError) as exc_info:
            TextSubAsset(
                asset_kind="text",
                asset_type="headline",
                asset_id="headline_1",
            )
        assert "content" in str(exc_info.value)


class TestDestinationDiscriminators:
    """Test destination discriminator fields."""

    def test_platform_destination_requires_platform(self):
        """PlatformDestination requires platform field."""
        dest = PlatformDestination(
            type="platform",
            platform="google_ads",
            account="123",
        )
        assert dest.type == "platform"
        assert dest.platform == "google_ads"
        assert not hasattr(dest, "agent_url")

    def test_platform_destination_missing_platform_fails(self):
        """PlatformDestination without platform should fail."""
        with pytest.raises(ValidationError) as exc_info:
            PlatformDestination(
                type="platform",
                account="123",
            )
        assert "platform" in str(exc_info.value)

    def test_agent_destination_requires_agent_url(self):
        """AgentDestination requires agent_url field."""
        dest = AgentDestination(
            type="agent",
            agent_url="https://agent.example.com",
            account="123",
        )
        assert dest.type == "agent"
        assert dest.agent_url == "https://agent.example.com"
        assert not hasattr(dest, "platform")

    def test_agent_destination_missing_agent_url_fails(self):
        """AgentDestination without agent_url should fail."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDestination(
                type="agent",
                account="123",
            )
        assert "agent_url" in str(exc_info.value)


class TestDeploymentDiscriminators:
    """Test deployment discriminator fields."""

    def test_platform_deployment_requires_platform(self):
        """PlatformDeployment requires platform field."""
        deployment = PlatformDeployment(
            type="platform",
            platform="google_ads",
            account="123",
            is_live=True,
        )
        assert deployment.type == "platform"
        assert deployment.platform == "google_ads"
        assert deployment.is_live is True
        assert not hasattr(deployment, "agent_url")

    def test_agent_deployment_requires_agent_url(self):
        """AgentDeployment requires agent_url field."""
        deployment = AgentDeployment(
            type="agent",
            agent_url="https://agent.example.com",
            account="123",
            is_live=True,
        )
        assert deployment.type == "agent"
        assert deployment.agent_url == "https://agent.example.com"
        assert deployment.is_live is True
        assert not hasattr(deployment, "platform")


class TestUnionTypeValidation:
    """Test union type validation and deserialization."""

    def test_success_response_from_dict(self):
        """CreateMediaBuyResponse should validate success from dict."""
        data = {
            "media_buy_id": "mb_123",
            "buyer_ref": "ref_456",
            "packages": [],
        }
        # Should validate as CreateMediaBuySuccess
        response = CreateMediaBuySuccess.model_validate(data)
        assert isinstance(response, CreateMediaBuySuccess)
        assert response.media_buy_id == "mb_123"

    def test_error_response_from_dict(self):
        """CreateMediaBuyResponse should validate error from dict."""
        data = {"errors": [{"code": "invalid", "message": "Invalid request"}]}
        # Should validate as CreateMediaBuyError
        response = CreateMediaBuyError.model_validate(data)
        assert isinstance(response, CreateMediaBuyError)
        assert len(response.errors) == 1

    def test_media_asset_from_dict(self):
        """SubAsset should validate media variant from dict."""
        data = {
            "asset_kind": "media",
            "asset_type": "thumbnail_image",
            "asset_id": "thumb_1",
            "content_uri": "https://example.com/image.jpg",
        }
        asset = MediaSubAsset.model_validate(data)
        assert isinstance(asset, MediaSubAsset)
        assert asset.asset_kind == "media"

    def test_text_asset_from_dict(self):
        """SubAsset should validate text variant from dict."""
        data = {
            "asset_kind": "text",
            "asset_type": "headline",
            "asset_id": "headline_1",
            "content": "Amazing Product!",
        }
        asset = TextSubAsset.model_validate(data)
        assert isinstance(asset, TextSubAsset)
        assert asset.asset_kind == "text"

    def test_platform_destination_from_dict(self):
        """Destination should validate platform variant from dict."""
        data = {"type": "platform", "platform": "google_ads", "account": "123"}
        dest = PlatformDestination.model_validate(data)
        assert isinstance(dest, PlatformDestination)
        assert dest.type == "platform"

    def test_agent_destination_from_dict(self):
        """Destination should validate agent variant from dict."""
        data = {
            "type": "agent",
            "agent_url": "https://agent.example.com",
            "account": "123",
        }
        dest = AgentDestination.model_validate(data)
        assert isinstance(dest, AgentDestination)
        assert dest.type == "agent"


class TestSerializationRoundtrips:
    """Test that discriminated unions serialize and deserialize correctly."""

    def test_success_response_roundtrip(self):
        """CreateMediaBuySuccess should roundtrip through JSON."""
        original = CreateMediaBuySuccess(
            media_buy_id="mb_123", buyer_ref="ref_456", packages=[]
        )
        json_str = original.model_dump_json()
        parsed = CreateMediaBuySuccess.model_validate_json(json_str)
        assert parsed.media_buy_id == original.media_buy_id
        assert parsed.buyer_ref == original.buyer_ref

    def test_error_response_roundtrip(self):
        """CreateMediaBuyError should roundtrip through JSON."""
        original = CreateMediaBuyError(
            errors=[Error(code="invalid", message="Invalid")]
        )
        json_str = original.model_dump_json()
        parsed = CreateMediaBuyError.model_validate_json(json_str)
        assert len(parsed.errors) == len(original.errors)
        assert parsed.errors[0].code == original.errors[0].code

    def test_media_asset_roundtrip(self):
        """MediaSubAsset should roundtrip through JSON."""
        original = MediaSubAsset(
            asset_kind="media",
            asset_type="thumbnail_image",
            asset_id="thumb_1",
            content_uri="https://example.com/image.jpg",
        )
        json_str = original.model_dump_json()
        parsed = MediaSubAsset.model_validate_json(json_str)
        assert parsed.asset_kind == original.asset_kind
        assert parsed.content_uri == original.content_uri

    def test_text_asset_roundtrip(self):
        """TextSubAsset should roundtrip through JSON."""
        original = TextSubAsset(
            asset_kind="text",
            asset_type="headline",
            asset_id="headline_1",
            content="Amazing Product!",
        )
        json_str = original.model_dump_json()
        parsed = TextSubAsset.model_validate_json(json_str)
        assert parsed.asset_kind == original.asset_kind
        assert parsed.content == original.content

    def test_platform_destination_roundtrip(self):
        """PlatformDestination should roundtrip through JSON."""
        original = PlatformDestination(
            type="platform", platform="google_ads", account="123"
        )
        json_str = original.model_dump_json()
        parsed = PlatformDestination.model_validate_json(json_str)
        assert parsed.type == original.type
        assert parsed.platform == original.platform

    def test_agent_destination_roundtrip(self):
        """AgentDestination should roundtrip through JSON."""
        original = AgentDestination(
            type="agent", agent_url="https://agent.example.com", account="123"
        )
        json_str = original.model_dump_json()
        parsed = AgentDestination.model_validate_json(json_str)
        assert parsed.type == original.type
        assert parsed.agent_url == original.agent_url


class TestInvalidDiscriminatorValues:
    """Test that invalid discriminator values are rejected."""

    def test_invalid_asset_kind_rejected(self):
        """SubAsset with invalid asset_kind should fail."""
        # MediaSubAsset only accepts "media"
        with pytest.raises(ValidationError):
            MediaSubAsset(
                asset_kind="video",  # Invalid
                asset_type="thumbnail_image",
                asset_id="thumb_1",
                content_uri="https://example.com/image.jpg",
            )

    def test_invalid_destination_type_rejected(self):
        """Destination with invalid type should fail."""
        # PlatformDestination only accepts "platform"
        with pytest.raises(ValidationError):
            PlatformDestination(
                type="agent",  # Invalid for PlatformDestination
                platform="google_ads",
                account="123",
            )

    def test_invalid_deployment_type_rejected(self):
        """Deployment with invalid type should fail."""
        # AgentDeployment only accepts "agent"
        with pytest.raises(ValidationError):
            AgentDeployment(
                type="platform",  # Invalid for AgentDeployment
                agent_url="https://agent.example.com",
                account="123",
                media_buy_id="mb_123",
            )
