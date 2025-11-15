"""Tests for discriminated union types with AdCP v2.4.0 schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.types.generated import (
    ActivateSignalResponse1,  # Success
    ActivateSignalResponse2,  # Error
    AuthorizedAgents,  # property_ids variant
    AuthorizedAgents1,  # property_tags variant
    AuthorizedAgents2,  # inline_properties variant
    AuthorizedAgents3,  # publisher_properties variant
    CreateMediaBuyResponse1,  # Success
    CreateMediaBuyResponse2,  # Error
    Destination1,  # Platform
    Destination2,  # Agent
    Deployment1,  # Platform
    Deployment2,  # Agent
    Error,
)
from adcp.types.generated_poc.product import PublisherProperty


class TestAuthorizationDiscriminatedUnions:
    """Test authorization_type discriminated unions in adagents.json."""

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

    def test_property_tags_authorization(self):
        """AuthorizedAgents1 (property_tags variant) requires property_tags and authorization_type."""
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
        """AuthorizedAgents2 (inline_properties variant) requires properties and authorization_type."""
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

    def test_publisher_properties_authorization(self):
        """AuthorizedAgents3 (publisher_properties variant) requires publisher_properties and authorization_type."""
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
        """CreateMediaBuyResponse1 (success) should validate with required fields."""
        success = CreateMediaBuyResponse1(
            media_buy_id="mb_123",
            buyer_ref="ref_456",
            packages=[],
        )
        assert success.media_buy_id == "mb_123"
        assert success.buyer_ref == "ref_456"
        assert not hasattr(success, "errors")

    def test_create_media_buy_error_variant(self):
        """CreateMediaBuyResponse2 (error) should validate with errors field."""
        error = CreateMediaBuyResponse2(
            errors=[{"code": "invalid_budget", "message": "Budget too low"}],
        )
        assert len(error.errors) == 1
        assert error.errors[0].code == "invalid_budget"
        assert not hasattr(error, "media_buy_id")

    def test_activate_signal_success_variant(self):
        """ActivateSignalResponse1 (success) should validate with required fields."""
        success = ActivateSignalResponse1(
            deployments=[],
        )
        assert success.deployments == []
        assert not hasattr(success, "errors")

    def test_activate_signal_error_variant(self):
        """ActivateSignalResponse2 (error) should validate with errors field."""
        error = ActivateSignalResponse2(
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
        """CreateMediaBuyResponse1 should validate success from dict."""
        data = {
            "media_buy_id": "mb_123",
            "buyer_ref": "ref_456",
            "packages": [],
        }
        response = CreateMediaBuyResponse1.model_validate(data)
        assert isinstance(response, CreateMediaBuyResponse1)
        assert response.media_buy_id == "mb_123"

    def test_error_response_from_dict(self):
        """CreateMediaBuyResponse2 should validate error from dict."""
        data = {
            "errors": [{"code": "invalid", "message": "Invalid request"}],
        }
        response = CreateMediaBuyResponse2.model_validate(data)
        assert isinstance(response, CreateMediaBuyResponse2)
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
        """CreateMediaBuyResponse1 should roundtrip through JSON."""
        original = CreateMediaBuyResponse1(
            media_buy_id="mb_123",
            buyer_ref="ref_456",
            packages=[],
        )
        json_str = original.model_dump_json()
        parsed = CreateMediaBuyResponse1.model_validate_json(json_str)
        assert parsed.media_buy_id == original.media_buy_id
        assert parsed.buyer_ref == original.buyer_ref

    def test_error_response_roundtrip(self):
        """CreateMediaBuyResponse2 should roundtrip through JSON."""
        original = CreateMediaBuyResponse2(
            errors=[{"code": "invalid", "message": "Invalid"}],
        )
        json_str = original.model_dump_json()
        parsed = CreateMediaBuyResponse2.model_validate_json(json_str)
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
        original = Destination2(
            type="agent", agent_url="https://agent.example.com", account="123"
        )
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
    """Test PublisherProperty mutual exclusivity validation."""

    def test_publisher_property_with_only_property_ids(self):
        """PublisherProperty should accept only property_ids."""
        prop = PublisherProperty(
            publisher_domain="cnn.com",
            property_ids=["site1", "site2"],
        )
        assert prop.publisher_domain == "cnn.com"
        assert len(prop.property_ids) == 2
        assert prop.property_tags is None

    def test_publisher_property_with_only_property_tags(self):
        """PublisherProperty should accept only property_tags."""
        prop = PublisherProperty(
            publisher_domain="cnn.com",
            property_tags=["premium", "news"],
        )
        assert prop.publisher_domain == "cnn.com"
        assert len(prop.property_tags) == 2
        assert prop.property_ids is None

    def test_publisher_property_mutual_exclusivity_both_fails(self):
        """PublisherProperty should reject both property_ids and property_tags."""
        with pytest.raises(ValidationError) as exc_info:
            PublisherProperty(
                publisher_domain="cnn.com",
                property_ids=["site1"],
                property_tags=["premium"],
            )
        error_msg = str(exc_info.value)
        assert (
            "mutually exclusive" in error_msg.lower()
            or "exactly one" in error_msg.lower()
        )

    def test_publisher_property_mutual_exclusivity_neither_fails(self):
        """PublisherProperty should reject neither property_ids nor property_tags."""
        with pytest.raises(ValidationError) as exc_info:
            PublisherProperty(
                publisher_domain="cnn.com",
            )
        error_msg = str(exc_info.value)
        assert (
            "mutually exclusive" in error_msg.lower()
            or "exactly one" in error_msg.lower()
            or "at least one is required" in error_msg.lower()
        )
