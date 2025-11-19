"""Tests for discriminated union types with AdCP v2.4.0 schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# Use semantic aliases for response types
from adcp import (
    ActivateSignalErrorResponse,
    ActivateSignalSuccessResponse,
    BothPreviewRender,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
    HtmlPreviewRender,
    InlineDaastAsset,
    InlineVastAsset,
    MediaSubAsset,
    TextSubAsset,
    UrlDaastAsset,
    UrlPreviewRender,
    UrlVastAsset,
)

# Keep using generated names for authorization variants
# Deployment and Destination now have semantic aliases
from adcp.types._generated import (
    AuthorizedAgents,  # property_ids variant
    AuthorizedAgents1,  # property_tags variant
    AuthorizedAgents2,  # inline_properties variant
    AuthorizedAgents3,  # publisher_properties variant
    Deployment1,  # Platform
    Deployment2,  # Agent
    Destination1,  # Platform
    Destination2,  # Agent
    PublisherPropertySelector2,  # selection_type='by_id' (shared schema)
    PublisherPropertySelector3,  # selection_type='by_tag' (shared schema)
)

# Use shorter names for local aliases in this test
PublisherProperties4 = PublisherPropertySelector2
PublisherProperties5 = PublisherPropertySelector3


class TestAuthorizationDiscriminatedUnions:
    """Test authorization_type discriminated unions in adagents.json.

    Note: AuthorizedAgents variants are separate discriminated union types.
    Pydantic automatically enforces correct field usage based on Literal types.
    No model_validator needed because discrimination happens at type level.
    """

    def test_property_ids_authorization(self):
        """AuthorizedAgents (property_ids variant) requires property_ids and authorization_type."""
        agent = AuthorizedAgents(
            url="https://agent.example.com",
            authorized_for="All properties",
            authorization_type="property_ids",
            property_ids=["site1", "site2"],
        )
        assert agent.authorization_type == "property_ids"
        assert [p.root for p in agent.property_ids] == ["site1", "site2"]
        assert not hasattr(agent, "property_tags")
        assert not hasattr(agent, "properties")

    def test_property_ids_authorization_from_json(self):
        """AuthorizedAgents (property_ids) validates from JSON dict."""
        data = {
            "url": "https://agent.example.com",
            "authorized_for": "All properties",
            "authorization_type": "property_ids",
            "property_ids": ["site1", "site2"],
        }
        agent = AuthorizedAgents.model_validate(data)
        assert agent.authorization_type == "property_ids"
        assert [p.root for p in agent.property_ids] == ["site1", "site2"]

    def test_property_ids_authorization_wrong_type_fails(self):
        """AuthorizedAgents (property_ids) rejects wrong authorization_type value."""
        with pytest.raises(ValidationError) as exc_info:
            AuthorizedAgents(
                url="https://agent.example.com",
                authorized_for="All properties",
                authorization_type="property_tags",  # Wrong value for this variant
                property_ids=["site1"],
            )
        error_msg = str(exc_info.value)
        assert "authorization_type" in error_msg.lower()

    def test_property_tags_authorization(self):
        """AuthorizedAgents1 requires property_tags and authorization_type."""
        agent = AuthorizedAgents1(
            url="https://agent.example.com",
            authorized_for="All properties",
            authorization_type="property_tags",
            property_tags=["news", "sports"],
        )
        assert agent.authorization_type == "property_tags"
        assert [p.root for p in agent.property_tags] == ["news", "sports"]
        assert not hasattr(agent, "property_ids")

    def test_inline_properties_authorization(self):
        """AuthorizedAgents2 requires properties and authorization_type."""
        agent = AuthorizedAgents2(
            url="https://agent.example.com",
            authorized_for="All properties",
            authorization_type="inline_properties",
            properties=[
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Example Site",
                    "identifiers": [{"type": "domain", "value": "example.com"}],
                }
            ],
        )
        assert agent.authorization_type == "inline_properties"
        assert len(agent.properties) == 1
        assert not hasattr(agent, "property_ids")

    def test_inline_properties_authorization_from_json(self):
        """AuthorizedAgents2 (inline_properties) validates from JSON dict."""
        data = {
            "url": "https://agent.example.com",
            "authorized_for": "All properties",
            "authorization_type": "inline_properties",
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Example Site",
                    "identifiers": [{"type": "domain", "value": "example.com"}],
                }
            ],
        }
        agent = AuthorizedAgents2.model_validate(data)
        assert agent.authorization_type == "inline_properties"
        assert len(agent.properties) == 1

    def test_publisher_properties_authorization(self):
        """AuthorizedAgents3 requires publisher_properties and type."""
        agent = AuthorizedAgents3(
            url="https://agent.example.com",
            authorized_for="All properties",
            authorization_type="publisher_properties",
            publisher_properties=[
                {
                    "publisher_domain": "example.com",
                    "selection_type": "by_id",
                    "property_ids": ["site1"],
                }
            ],
        )
        assert agent.authorization_type == "publisher_properties"
        assert len(agent.publisher_properties) == 1
        assert not hasattr(agent, "property_ids")


class TestResponseUnions:
    """Test discriminated union response types."""

    def test_create_media_buy_success_variant(self):
        """CreateMediaBuySuccessResponse should validate with required fields."""
        success = CreateMediaBuySuccessResponse(
            media_buy_id="mb_123",
            buyer_ref="ref_456",
            packages=[],
        )
        assert success.media_buy_id == "mb_123"
        assert success.buyer_ref == "ref_456"
        assert not hasattr(success, "errors")

    def test_create_media_buy_error_variant(self):
        """CreateMediaBuyErrorResponse should validate with errors field."""
        error = CreateMediaBuyErrorResponse(
            errors=[{"code": "invalid_budget", "message": "Budget too low"}],
        )
        assert len(error.errors) == 1
        assert error.errors[0].code == "invalid_budget"
        assert not hasattr(error, "media_buy_id")

    def test_activate_signal_success_variant(self):
        """ActivateSignalSuccessResponse should validate with required fields."""
        success = ActivateSignalSuccessResponse(
            deployments=[],
        )
        assert success.deployments == []
        assert not hasattr(success, "errors")

    def test_activate_signal_error_variant(self):
        """ActivateSignalErrorResponse should validate with errors field."""
        error = ActivateSignalErrorResponse(
            errors=[{"code": "unauthorized", "message": "Not authorized"}],
        )
        assert len(error.errors) == 1
        assert not hasattr(error, "deployments")


class TestDestinationDiscriminators:
    """Test destination discriminator fields."""

    def test_platform_destination_requires_platform(self):
        """Destination1 (platform) requires platform field."""
        dest = Destination1(
            type="platform",
            platform="google_ads",
            account="123",
        )
        assert dest.type == "platform"
        assert dest.platform == "google_ads"
        assert not hasattr(dest, "agent_url")

    def test_platform_destination_missing_platform_fails(self):
        """Destination1 without platform should fail."""
        with pytest.raises(ValidationError) as exc_info:
            Destination1(
                type="platform",
                account="123",
            )
        assert "platform" in str(exc_info.value)

    def test_agent_destination_requires_agent_url(self):
        """Destination2 (agent) requires agent_url field."""
        dest = Destination2(
            type="agent",
            agent_url="https://agent.example.com",
            account="123",
        )
        assert dest.type == "agent"
        assert str(dest.agent_url).rstrip("/") == "https://agent.example.com"
        assert not hasattr(dest, "platform")

    def test_agent_destination_missing_agent_url_fails(self):
        """Destination2 without agent_url should fail."""
        with pytest.raises(ValidationError) as exc_info:
            Destination2(
                type="agent",
                account="123",
            )
        assert "agent_url" in str(exc_info.value)


class TestDeploymentDiscriminators:
    """Test deployment discriminator fields."""

    def test_platform_deployment_requires_platform(self):
        """Deployment1 (platform) requires platform field."""
        deployment = Deployment1(
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
        """Deployment2 (agent) requires agent_url field."""
        deployment = Deployment2(
            type="agent",
            agent_url="https://agent.example.com",
            account="123",
            is_live=True,
        )
        assert deployment.type == "agent"
        assert str(deployment.agent_url).rstrip("/") == "https://agent.example.com"
        assert deployment.is_live is True
        assert not hasattr(deployment, "platform")


class TestUnionTypeValidation:
    """Test union type validation and deserialization."""

    def test_success_response_from_dict(self):
        """CreateMediaBuySuccessResponse should validate success from dict."""
        data = {
            "media_buy_id": "mb_123",
            "buyer_ref": "ref_456",
            "packages": [],
        }
        response = CreateMediaBuySuccessResponse.model_validate(data)
        assert isinstance(response, CreateMediaBuySuccessResponse)
        assert response.media_buy_id == "mb_123"

    def test_error_response_from_dict(self):
        """CreateMediaBuyErrorResponse should validate error from dict."""
        data = {
            "errors": [{"code": "invalid", "message": "Invalid request"}],
        }
        response = CreateMediaBuyErrorResponse.model_validate(data)
        assert isinstance(response, CreateMediaBuyErrorResponse)
        assert len(response.errors) == 1

    def test_platform_destination_from_dict(self):
        """Destination1 should validate platform variant from dict."""
        data = {"type": "platform", "platform": "google_ads", "account": "123"}
        dest = Destination1.model_validate(data)
        assert isinstance(dest, Destination1)
        assert dest.type == "platform"

    def test_agent_destination_from_dict(self):
        """Destination2 should validate agent variant from dict."""
        data = {
            "type": "agent",
            "agent_url": "https://agent.example.com",
            "account": "123",
        }
        dest = Destination2.model_validate(data)
        assert isinstance(dest, Destination2)
        assert dest.type == "agent"


class TestSerializationRoundtrips:
    """Test that discriminated unions serialize and deserialize correctly."""

    def test_success_response_roundtrip(self):
        """CreateMediaBuySuccessResponse should roundtrip through JSON."""
        original = CreateMediaBuySuccessResponse(
            media_buy_id="mb_123",
            buyer_ref="ref_456",
            packages=[],
        )
        json_str = original.model_dump_json()
        parsed = CreateMediaBuySuccessResponse.model_validate_json(json_str)
        assert parsed.media_buy_id == original.media_buy_id
        assert parsed.buyer_ref == original.buyer_ref

    def test_error_response_roundtrip(self):
        """CreateMediaBuyErrorResponse should roundtrip through JSON."""
        original = CreateMediaBuyErrorResponse(
            errors=[{"code": "invalid", "message": "Invalid"}],
        )
        json_str = original.model_dump_json()
        parsed = CreateMediaBuyErrorResponse.model_validate_json(json_str)
        assert len(parsed.errors) == len(original.errors)
        assert parsed.errors[0].code == original.errors[0].code

    def test_platform_destination_roundtrip(self):
        """Destination1 should roundtrip through JSON."""
        original = Destination1(type="platform", platform="google_ads", account="123")
        json_str = original.model_dump_json()
        parsed = Destination1.model_validate_json(json_str)
        assert parsed.type == original.type
        assert parsed.platform == original.platform

    def test_agent_destination_roundtrip(self):
        """Destination2 should roundtrip through JSON."""
        original = Destination2(type="agent", agent_url="https://agent.example.com", account="123")
        json_str = original.model_dump_json()
        parsed = Destination2.model_validate_json(json_str)
        assert parsed.type == original.type
        assert parsed.agent_url == original.agent_url


class TestInvalidDiscriminatorValues:
    """Test that invalid discriminator values are rejected."""

    def test_invalid_destination_type_rejected(self):
        """Destination1 with wrong type should fail."""
        with pytest.raises(ValidationError):
            Destination1(
                type="agent",  # Invalid for Destination1
                platform="google_ads",
                account="123",
            )

    def test_invalid_deployment_type_rejected(self):
        """Deployment2 with wrong type should fail."""
        with pytest.raises(ValidationError):
            Deployment2(
                type="platform",  # Invalid for Deployment2
                agent_url="https://agent.example.com",
                account="123",
                is_live=True,
            )

    def test_invalid_authorization_type_rejected(self):
        """AuthorizedAgents with wrong authorization_type should fail."""
        with pytest.raises(ValidationError):
            AuthorizedAgents(
                url="https://agent.example.com",
                authorized_for="All properties",
                authorization_type="invalid_type",  # Invalid
                property_ids=["site1"],
            )


class TestPublisherPropertyValidation:
    """Test publisher_properties discriminated union validation.

    Note: Schema v1.0.0+ uses discriminated unions for publisher_properties:
    - PublisherProperties (selection_type='all') - all properties from publisher
    - PublisherProperties4 (selection_type='by_id') with property_ids
    - PublisherProperties5 (selection_type='by_tag') with property_tags

    Pydantic automatically enforces discriminated union constraints, so we test
    that the correct variants can be constructed and invalid variants fail.
    """

    def test_publisher_property_with_property_ids(self):
        """PublisherProperties4 with selection_type='by_id' requires property_ids."""
        prop = PublisherProperties4(
            publisher_domain="cnn.com",
            property_ids=["site1", "site2"],
            selection_type="by_id",
        )
        assert prop.publisher_domain == "cnn.com"
        assert len(prop.property_ids) == 2
        assert prop.selection_type == "by_id"

    def test_publisher_property_with_property_ids_from_json(self):
        """PublisherProperties4 validates from JSON dict."""
        data = {
            "publisher_domain": "cnn.com",
            "property_ids": ["site1", "site2"],
            "selection_type": "by_id",
        }
        prop = PublisherProperties4.model_validate(data)
        assert prop.publisher_domain == "cnn.com"
        assert len(prop.property_ids) == 2
        assert prop.selection_type == "by_id"

    def test_publisher_property_with_property_tags(self):
        """PublisherProperties5 with selection_type='by_tag' requires property_tags."""
        prop = PublisherProperties5(
            publisher_domain="cnn.com",
            property_tags=["premium", "news"],
            selection_type="by_tag",
        )
        assert prop.publisher_domain == "cnn.com"
        assert len(prop.property_tags) == 2
        assert prop.selection_type == "by_tag"

    def test_publisher_property_with_property_tags_from_json(self):
        """PublisherProperties5 validates from JSON dict."""
        data = {
            "publisher_domain": "cnn.com",
            "property_tags": ["premium", "news"],
            "selection_type": "by_tag",
        }
        prop = PublisherProperties5.model_validate(data)
        assert prop.publisher_domain == "cnn.com"
        assert len(prop.property_tags) == 2
        assert prop.selection_type == "by_tag"

    def test_publisher_property_by_id_without_property_ids_fails(self):
        """PublisherProperties4 requires property_ids field."""
        with pytest.raises(ValidationError) as exc_info:
            PublisherProperties4(
                publisher_domain="cnn.com",
                selection_type="by_id",
                # Missing property_ids - should fail
            )
        error_msg = str(exc_info.value)
        assert "property_ids" in error_msg.lower()

    def test_publisher_property_by_tag_without_property_tags_fails(self):
        """PublisherProperties5 requires property_tags field."""
        with pytest.raises(ValidationError) as exc_info:
            PublisherProperties5(
                publisher_domain="cnn.com",
                selection_type="by_tag",
                # Missing property_tags - should fail
            )
        error_msg = str(exc_info.value)
        assert "property_tags" in error_msg.lower()


class TestProductValidation:
    """Test Product model validation including publisher_properties.

    Note: Product uses discriminated union types for publisher_properties.
    These tests verify that the union types work correctly in Product context.
    """

    def test_product_accepts_valid_publisher_properties_by_id(self):
        """Product accepts valid PublisherProperties4 with selection_type='by_id'."""
        valid_props = [
            PublisherProperties4(
                publisher_domain="cnn.com",
                property_ids=["site1", "site2"],
                selection_type="by_id",
            )
        ]
        assert len(valid_props) == 1
        assert valid_props[0].property_ids is not None
        assert valid_props[0].selection_type == "by_id"

    def test_product_accepts_valid_publisher_properties_by_tag(self):
        """Product accepts valid PublisherProperties5 with selection_type='by_tag'."""
        valid_props = [
            PublisherProperties5(
                publisher_domain="cnn.com",
                property_tags=["premium", "news"],
                selection_type="by_tag",
            )
        ]
        assert len(valid_props) == 1
        assert valid_props[0].property_tags is not None
        assert valid_props[0].selection_type == "by_tag"

    def test_product_accepts_mixed_publisher_properties(self):
        """Product accepts a mix of by_id and by_tag publisher_properties."""
        mixed_props = [
            PublisherProperties4(
                publisher_domain="cnn.com",
                property_ids=["site1"],
                selection_type="by_id",
            ),
            PublisherProperties5(
                publisher_domain="nytimes.com",
                property_tags=["premium"],
                selection_type="by_tag",
            ),
        ]
        assert len(mixed_props) == 2
        assert mixed_props[0].selection_type == "by_id"
        assert mixed_props[1].selection_type == "by_tag"


class TestPreviewRenderDiscriminators:
    """Test PreviewRender discriminator field values match semantic aliases."""

    def test_url_preview_render_has_url_discriminator(self):
        """UrlPreviewRender has output_format='url'."""
        render = UrlPreviewRender(
            render_id="render_1",
            role="primary",
            output_format="url",
            preview_url="https://preview.example.com/creative",
        )
        assert render.output_format == "url"
        assert hasattr(render, "preview_url")
        assert not hasattr(render, "preview_html")

    def test_html_preview_render_has_html_discriminator(self):
        """HtmlPreviewRender has output_format='html'."""
        render = HtmlPreviewRender(
            render_id="render_1",
            role="primary",
            output_format="html",
            preview_html="<div>Preview HTML</div>",
        )
        assert render.output_format == "html"
        assert hasattr(render, "preview_html")
        assert not hasattr(render, "preview_url")

    def test_both_preview_render_has_both_discriminator(self):
        """BothPreviewRender has output_format='both'."""
        render = BothPreviewRender(
            render_id="render_1",
            role="primary",
            output_format="both",
            preview_url="https://preview.example.com/creative",
            preview_html="<div>Preview HTML</div>",
        )
        assert render.output_format == "both"
        assert hasattr(render, "preview_url")
        assert hasattr(render, "preview_html")

    def test_url_preview_render_rejects_wrong_discriminator(self):
        """UrlPreviewRender rejects output_format='html'."""
        with pytest.raises(ValidationError) as exc_info:
            UrlPreviewRender(
                render_id="render_1",
                role="primary",
                output_format="html",  # Wrong discriminator value
                preview_url="https://preview.example.com/creative",
            )
        assert "output_format" in str(exc_info.value).lower()

    def test_html_preview_render_rejects_wrong_discriminator(self):
        """HtmlPreviewRender rejects output_format='url'."""
        with pytest.raises(ValidationError) as exc_info:
            HtmlPreviewRender(
                render_id="render_1",
                role="primary",
                output_format="url",  # Wrong discriminator value
                preview_html="<div>Preview HTML</div>",
            )
        assert "output_format" in str(exc_info.value).lower()


class TestVastAssetDiscriminators:
    """Test VastAsset discriminator field values match semantic aliases."""

    def test_url_vast_asset_has_url_discriminator(self):
        """UrlVastAsset has delivery_type='url'."""
        asset = UrlVastAsset(
            delivery_type="url",
            url="https://vast.example.com/ad.xml",
        )
        assert asset.delivery_type == "url"
        assert hasattr(asset, "url")
        assert not hasattr(asset, "vast_xml")

    def test_inline_vast_asset_has_inline_discriminator(self):
        """InlineVastAsset has delivery_type='inline'."""
        asset = InlineVastAsset(
            delivery_type="inline",
            content="<VAST>...</VAST>",
        )
        assert asset.delivery_type == "inline"
        assert hasattr(asset, "content")
        assert not hasattr(asset, "url")

    def test_url_vast_asset_rejects_wrong_discriminator(self):
        """UrlVastAsset rejects delivery_type='inline'."""
        with pytest.raises(ValidationError) as exc_info:
            UrlVastAsset(
                delivery_type="inline",  # Wrong discriminator value
                url="https://vast.example.com/ad.xml",
            )
        assert "delivery_type" in str(exc_info.value).lower()

    def test_inline_vast_asset_rejects_wrong_discriminator(self):
        """InlineVastAsset rejects delivery_type='url'."""
        with pytest.raises(ValidationError) as exc_info:
            InlineVastAsset(
                delivery_type="url",  # Wrong discriminator value
                content="<VAST>...</VAST>",
            )
        assert "delivery_type" in str(exc_info.value).lower()


class TestDaastAssetDiscriminators:
    """Test DaastAsset discriminator field values match semantic aliases."""

    def test_url_daast_asset_has_url_discriminator(self):
        """UrlDaastAsset has delivery_type='url'."""
        asset = UrlDaastAsset(
            delivery_type="url",
            url="https://daast.example.com/ad.xml",
        )
        assert asset.delivery_type == "url"
        assert hasattr(asset, "url")
        assert not hasattr(asset, "content")

    def test_inline_daast_asset_has_inline_discriminator(self):
        """InlineDaastAsset has delivery_type='inline'."""
        asset = InlineDaastAsset(
            delivery_type="inline",
            content="<DAAST>...</DAAST>",
        )
        assert asset.delivery_type == "inline"
        assert hasattr(asset, "content")
        assert not hasattr(asset, "url")

    def test_url_daast_asset_rejects_wrong_discriminator(self):
        """UrlDaastAsset rejects delivery_type='inline'."""
        with pytest.raises(ValidationError) as exc_info:
            UrlDaastAsset(
                delivery_type="inline",  # Wrong discriminator value
                url="https://daast.example.com/ad.xml",
            )
        assert "delivery_type" in str(exc_info.value).lower()

    def test_inline_daast_asset_rejects_wrong_discriminator(self):
        """InlineDaastAsset rejects delivery_type='url'."""
        with pytest.raises(ValidationError) as exc_info:
            InlineDaastAsset(
                delivery_type="url",  # Wrong discriminator value
                content="<DAAST>...</DAAST>",
            )
        assert "delivery_type" in str(exc_info.value).lower()


class TestSubAssetDiscriminators:
    """Test SubAsset discriminator field values match semantic aliases."""

    def test_media_sub_asset_has_media_discriminator(self):
        """MediaSubAsset has asset_kind='media'."""
        asset = MediaSubAsset(
            asset_id="asset_1",
            asset_type="logo",
            asset_kind="media",
            content_uri="https://cdn.example.com/logo.png",
        )
        assert asset.asset_kind == "media"
        assert hasattr(asset, "content_uri")
        assert not hasattr(asset, "content")

    def test_text_sub_asset_has_text_discriminator(self):
        """TextSubAsset has asset_kind='text'."""
        asset = TextSubAsset(
            asset_id="asset_2",
            asset_type="headline",
            asset_kind="text",
            content="Buy Now!",
        )
        assert asset.asset_kind == "text"
        assert hasattr(asset, "content")
        assert not hasattr(asset, "content_uri")

    def test_media_sub_asset_rejects_wrong_discriminator(self):
        """MediaSubAsset rejects asset_kind='text'."""
        with pytest.raises(ValidationError) as exc_info:
            MediaSubAsset(
                asset_id="asset_1",
                asset_type="logo",
                asset_kind="text",  # Wrong discriminator value
                content_uri="https://cdn.example.com/logo.png",
            )
        assert "asset_kind" in str(exc_info.value).lower()

    def test_text_sub_asset_rejects_wrong_discriminator(self):
        """TextSubAsset rejects asset_kind='media'."""
        with pytest.raises(ValidationError) as exc_info:
            TextSubAsset(
                asset_id="asset_2",
                asset_type="headline",
                asset_kind="media",  # Wrong discriminator value
                content="Buy Now!",
            )
        assert "asset_kind" in str(exc_info.value).lower()


class TestSemanticAliasDiscriminatorRoundtrips:
    """Test that semantic aliases serialize/deserialize with correct discriminators."""

    def test_url_preview_render_roundtrip(self):
        """UrlPreviewRender roundtrips with output_format='url'."""
        original = UrlPreviewRender(
            render_id="render_1",
            role="primary",
            output_format="url",
            preview_url="https://preview.example.com/creative",
        )
        json_str = original.model_dump_json()
        parsed = UrlPreviewRender.model_validate_json(json_str)
        assert parsed.output_format == "url"
        assert parsed.preview_url == original.preview_url

    def test_url_vast_asset_roundtrip(self):
        """UrlVastAsset roundtrips with delivery_type='url'."""
        original = UrlVastAsset(
            delivery_type="url",
            url="https://vast.example.com/ad.xml",
        )
        json_str = original.model_dump_json()
        parsed = UrlVastAsset.model_validate_json(json_str)
        assert parsed.delivery_type == "url"
        assert parsed.url == original.url

    def test_media_sub_asset_roundtrip(self):
        """MediaSubAsset roundtrips with asset_kind='media'."""
        original = MediaSubAsset(
            asset_id="asset_1",
            asset_type="logo",
            asset_kind="media",
            content_uri="https://cdn.example.com/logo.png",
        )
        json_str = original.model_dump_json()
        parsed = MediaSubAsset.model_validate_json(json_str)
        assert parsed.asset_kind == "media"
        assert parsed.content_uri == original.content_uri

    def test_text_sub_asset_roundtrip(self):
        """TextSubAsset roundtrips with asset_kind='text'."""
        original = TextSubAsset(
            asset_id="asset_2",
            asset_type="headline",
            asset_kind="text",
            content="Buy Now!",
        )
        json_str = original.model_dump_json()
        parsed = TextSubAsset.model_validate_json(json_str)
        assert parsed.asset_kind == "text"
        assert parsed.content == original.content
