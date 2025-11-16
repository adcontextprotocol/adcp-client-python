from __future__ import annotations

"""Tests for adagents.json validation functionality."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp.adagents import (
    AuthorizationContext,
    _normalize_domain,
    _validate_publisher_domain,
    domain_matches,
    fetch_agent_authorizations,
    get_all_properties,
    get_all_tags,
    get_properties_by_agent,
    identifiers_match,
    verify_agent_authorization,
)
from adcp.exceptions import (
    AdagentsValidationError,
)


def create_mock_httpx_client(mock_response):
    """Helper to create a properly mocked httpx.AsyncClient."""
    mock_get = AsyncMock(return_value=mock_response)
    mock_client_instance = MagicMock()
    mock_client_instance.get = mock_get
    mock_client_instance.__aenter__.return_value = mock_client_instance
    mock_client_instance.__aexit__.return_value = AsyncMock()
    return mock_client_instance


class TestDomainNormalization:
    """Test domain normalization function."""

    def test_normalize_basic(self):
        """Basic normalization should work."""
        assert _normalize_domain("Example.COM") == "example.com"
        assert _normalize_domain("  example.com  ") == "example.com"

    def test_normalize_trailing_slash(self):
        """Should remove trailing slashes."""
        assert _normalize_domain("example.com/") == "example.com"
        assert _normalize_domain("example.com///") == "example.com"

    def test_normalize_trailing_dot(self):
        """Should remove trailing dots."""
        assert _normalize_domain("example.com.") == "example.com"
        assert _normalize_domain("example.com...") == "example.com"

    def test_normalize_both(self):
        """Should remove both trailing slashes and dots."""
        assert _normalize_domain("example.com/.") == "example.com"

    def test_normalize_invalid_double_dots(self):
        """Double dots should raise error."""
        with pytest.raises(AdagentsValidationError, match="Invalid domain format"):
            _normalize_domain("example..com")

    def test_normalize_empty(self):
        """Empty string should raise error."""
        with pytest.raises(AdagentsValidationError, match="Invalid domain format"):
            _normalize_domain("")
        with pytest.raises(AdagentsValidationError, match="Invalid domain format"):
            _normalize_domain("   ")


class TestPublisherDomainValidation:
    """Test publisher domain validation for security."""

    def test_validate_basic(self):
        """Basic valid domains should pass."""
        assert _validate_publisher_domain("example.com") == "example.com"
        assert _validate_publisher_domain("sub.example.com") == "sub.example.com"

    def test_validate_removes_protocol(self):
        """Should strip protocol if present."""
        assert _validate_publisher_domain("https://example.com") == "example.com"
        assert _validate_publisher_domain("http://example.com") == "example.com"

    def test_validate_removes_path(self):
        """Should strip path if present."""
        assert _validate_publisher_domain("example.com/path") == "example.com"
        assert _validate_publisher_domain("https://example.com/path") == "example.com"

    def test_validate_case_insensitive(self):
        """Should normalize to lowercase."""
        assert _validate_publisher_domain("EXAMPLE.COM") == "example.com"

    def test_validate_empty(self):
        """Empty domain should raise error."""
        with pytest.raises(AdagentsValidationError, match="cannot be empty"):
            _validate_publisher_domain("")
        with pytest.raises(AdagentsValidationError, match="cannot be empty"):
            _validate_publisher_domain("   ")

    def test_validate_too_long(self):
        """Domain exceeding DNS max length should raise error."""
        long_domain = "a" * 254
        with pytest.raises(AdagentsValidationError, match="too long"):
            _validate_publisher_domain(long_domain)

    def test_validate_suspicious_chars(self):
        """Suspicious characters should raise error."""
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("example.com\\malicious")
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("user@example.com")
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("example.com with spaces")
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("example.com\n")

    def test_validate_no_dots(self):
        """Domain without dots should raise error."""
        with pytest.raises(AdagentsValidationError, match="must contain at least one dot"):
            _validate_publisher_domain("localhost")


class TestDomainMatching:
    """Test domain matching logic per AdCP spec."""

    def test_exact_match(self):
        """Exact domain match should succeed."""
        assert domain_matches("example.com", "example.com")
        assert domain_matches("sub.example.com", "sub.example.com")

    def test_case_insensitive(self):
        """Domain matching should be case-insensitive."""
        assert domain_matches("Example.com", "example.com")
        assert domain_matches("example.com", "EXAMPLE.COM")

    def test_bare_domain_matches_www(self):
        """Bare domain should match www subdomain."""
        assert domain_matches("www.example.com", "example.com")
        assert domain_matches("m.example.com", "example.com")

    def test_bare_domain_does_not_match_other_subdomains(self):
        """Bare domain should NOT match arbitrary subdomains."""
        assert not domain_matches("api.example.com", "example.com")
        assert not domain_matches("cdn.example.com", "example.com")

    def test_specific_subdomain_does_not_match_others(self):
        """Specific subdomain should only match itself."""
        assert not domain_matches("www.example.com", "api.example.com")
        assert domain_matches("api.example.com", "api.example.com")

    def test_wildcard_matches_all_subdomains(self):
        """Wildcard pattern should match all subdomains."""
        assert domain_matches("www.example.com", "*.example.com")
        assert domain_matches("api.example.com", "*.example.com")
        assert domain_matches("cdn.example.com", "*.example.com")
        assert domain_matches("sub.api.example.com", "*.example.com")

    def test_wildcard_does_not_match_base_domain(self):
        """Wildcard should not match the base domain without subdomain."""
        assert not domain_matches("example.com", "*.example.com")

    def test_no_match_different_domains(self):
        """Different domains should not match."""
        assert not domain_matches("example.com", "other.com")
        assert not domain_matches("www.example.com", "other.com")


class TestIdentifierMatching:
    """Test identifier matching logic."""

    def test_domain_identifier_uses_domain_matching(self):
        """Domain identifiers should use domain matching rules."""
        property_ids = [{"type": "domain", "value": "www.example.com"}]
        agent_ids = [{"type": "domain", "value": "example.com"}]
        assert identifiers_match(property_ids, agent_ids)

    def test_bundle_id_exact_match(self):
        """Bundle IDs require exact match."""
        property_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        agent_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        assert identifiers_match(property_ids, agent_ids)

    def test_bundle_id_no_partial_match(self):
        """Bundle IDs should not partially match."""
        property_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        agent_ids = [{"type": "bundle_id", "value": "com.example"}]
        assert not identifiers_match(property_ids, agent_ids)

    def test_type_mismatch(self):
        """Different identifier types should not match."""
        property_ids = [{"type": "domain", "value": "example.com"}]
        agent_ids = [{"type": "bundle_id", "value": "example.com"}]
        assert not identifiers_match(property_ids, agent_ids)

    def test_multiple_identifiers_any_match(self):
        """Should match if ANY identifier matches."""
        property_ids = [
            {"type": "domain", "value": "example.com"},
            {"type": "bundle_id", "value": "com.example.app"},
        ]
        agent_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        assert identifiers_match(property_ids, agent_ids)

    def test_no_match_empty_lists(self):
        """Empty lists should not match."""
        assert not identifiers_match([], [])
        assert not identifiers_match([{"type": "domain", "value": "example.com"}], [])


class TestVerifyAgentAuthorization:
    """Test agent authorization verification."""

    def test_agent_authorized_no_properties_restriction(self):
        """Agent with empty properties array is authorized for all properties."""
        adagents_data = {
            "authorized_agents": [{"url": "https://sales-agent.example.com", "properties": []}]
        }
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_agent_authorized_no_properties_field(self):
        """Agent without properties field is authorized for all properties."""
        adagents_data = {"authorized_agents": [{"url": "https://sales-agent.example.com"}]}
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_agent_url_protocol_agnostic(self):
        """Agent URL matching should ignore protocol."""
        adagents_data = {"authorized_agents": [{"url": "https://sales-agent.example.com"}]}
        assert verify_agent_authorization(
            adagents_data, "http://sales-agent.example.com", None, None
        )

    def test_agent_url_trailing_slash_ignored(self):
        """Agent URL matching should ignore trailing slash."""
        adagents_data = {"authorized_agents": [{"url": "https://sales-agent.example.com/"}]}
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_agent_authorized_specific_property(self):
        """Agent authorized for specific property type and identifiers."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Example Site",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        assert verify_agent_authorization(
            adagents_data,
            "https://sales-agent.example.com",
            "website",
            [{"type": "domain", "value": "www.example.com"}],
        )

    def test_agent_not_authorized_wrong_property_type(self):
        """Agent should not be authorized for wrong property type."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        assert not verify_agent_authorization(
            adagents_data,
            "https://sales-agent.example.com",
            "mobile_app",
            [{"type": "domain", "value": "example.com"}],
        )

    def test_agent_not_authorized_wrong_identifier(self):
        """Agent should not be authorized for wrong identifier."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        assert not verify_agent_authorization(
            adagents_data,
            "https://sales-agent.example.com",
            "website",
            [{"type": "domain", "value": "other.com"}],
        )

    def test_agent_not_in_list(self):
        """Agent not in authorized_agents list should not be authorized."""
        adagents_data = {
            "authorized_agents": [{"url": "https://other-agent.example.com", "properties": []}]
        }
        assert not verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_multiple_agents(self):
        """Should find correct agent in list."""
        adagents_data = {
            "authorized_agents": [
                {"url": "https://agent1.example.com", "properties": []},
                {"url": "https://agent2.example.com", "properties": []},
                {"url": "https://sales-agent.example.com", "properties": []},
            ]
        }
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_invalid_adagents_data_not_dict(self):
        """Should raise error if adagents_data is not a dict."""
        with pytest.raises(AdagentsValidationError, match="must be a dictionary"):
            verify_agent_authorization([], "https://agent.example.com", None, None)

    def test_invalid_adagents_data_no_authorized_agents(self):
        """Should raise error if authorized_agents field is missing."""
        with pytest.raises(AdagentsValidationError, match="authorized_agents"):
            verify_agent_authorization({}, "https://agent.example.com", None, None)

    def test_invalid_authorized_agents_not_list(self):
        """Should raise error if authorized_agents is not a list."""
        with pytest.raises(AdagentsValidationError, match="authorized_agents"):
            verify_agent_authorization(
                {"authorized_agents": "not a list"}, "https://agent.example.com", None, None
            )

    def test_property_type_match_without_identifiers(self):
        """Should match property type even without identifier check."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        # When property_identifiers is None, just check property_type
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", "website", None
        )


class TestFetchAdagents:
    """Test fetching adagents.json from publisher domains."""

    @pytest.mark.asyncio
    async def test_fetch_success(self):
        """Should successfully fetch and parse adagents.json."""
        from adcp.adagents import fetch_adagents

        mock_adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All properties",
                    "authorization_type": "property_ids",
                    "property_ids": ["site1", "site2"],
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_adagents_data
        mock_response.raise_for_status = MagicMock()

        mock_client = create_mock_httpx_client(mock_response)

        result = await fetch_adagents("example.com", client=mock_client)

        assert result == mock_adagents_data
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert "https://example.com/.well-known/adagents.json" in str(call_args)


class TestVerifyAgentForProperty:
    """Test convenience wrapper for fetching and verifying in one call."""

    @pytest.mark.asyncio
    async def test_verify_success(self):
        """Should fetch and verify authorization successfully."""
        from adcp.adagents import verify_agent_for_property

        mock_adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All properties",
                    "authorization_type": "property_ids",
                    "property_ids": ["site1", "site2"],
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_adagents_data
        mock_response.raise_for_status = MagicMock()

        mock_client = create_mock_httpx_client(mock_response)

        # Verify authorized agent
        result = await verify_agent_for_property(
            publisher_domain="example.com",
            agent_url="https://agent.example.com",
            property_identifiers=[{"type": "property_id", "value": "site1"}],
            client=mock_client,
        )

        assert result is True
        mock_client.get.assert_called_once()


class TestGetAllProperties:
    """Test extracting all properties from adagents.json data."""

    def test_get_all_properties(self):
        """Should extract all properties from all agents."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        },
                        {
                            "property_type": "mobile_app",
                            "name": "App 1",
                            "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
                        },
                    ],
                },
                {
                    "url": "https://agent2.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 2",
                            "identifiers": [{"type": "domain", "value": "site2.com"}],
                        }
                    ],
                },
            ]
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 3
        assert properties[0]["name"] == "Site 1"
        assert properties[0]["agent_url"] == "https://agent1.example.com"
        assert properties[1]["name"] == "App 1"
        assert properties[1]["agent_url"] == "https://agent1.example.com"
        assert properties[2]["name"] == "Site 2"
        assert properties[2]["agent_url"] == "https://agent2.example.com"

    def test_get_all_properties_with_empty_properties(self):
        """Should handle agents with empty properties array."""
        adagents_data = {
            "authorized_agents": [
                {"url": "https://agent1.example.com", "properties": []},
                {
                    "url": "https://agent2.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site",
                            "identifiers": [{"type": "domain", "value": "site.com"}],
                        }
                    ],
                },
            ]
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 1
        assert properties[0]["name"] == "Site"

    def test_get_all_properties_invalid_data(self):
        """Should raise error for invalid data."""
        with pytest.raises(AdagentsValidationError):
            get_all_properties([])


class TestGetAllTags:
    """Test extracting all unique tags from adagents.json data."""

    def test_get_all_tags(self):
        """Should extract all unique tags from properties."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                            "tags": ["premium", "news"],
                        },
                        {
                            "property_type": "mobile_app",
                            "name": "App 1",
                            "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
                            "tags": ["mobile", "premium"],
                        },
                    ],
                },
                {
                    "url": "https://agent2.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 2",
                            "identifiers": [{"type": "domain", "value": "site2.com"}],
                            "tags": ["sports"],
                        }
                    ],
                },
            ]
        }

        tags = get_all_tags(adagents_data)
        assert tags == {"premium", "news", "mobile", "sports"}

    def test_get_all_tags_no_tags(self):
        """Should return empty set when no tags present."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        }
                    ],
                }
            ]
        }

        tags = get_all_tags(adagents_data)
        assert tags == set()


class TestGetPropertiesByAgent:
    """Test getting properties for a specific agent."""

    def test_get_properties_by_agent(self):
        """Should return properties for specified agent."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        },
                        {
                            "property_type": "mobile_app",
                            "name": "App 1",
                            "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
                        },
                    ],
                },
                {
                    "url": "https://agent2.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 2",
                            "identifiers": [{"type": "domain", "value": "site2.com"}],
                        }
                    ],
                },
            ]
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 2
        assert properties[0]["name"] == "Site 1"
        assert properties[1]["name"] == "App 1"

    def test_get_properties_by_agent_protocol_agnostic(self):
        """Should match agent URL regardless of protocol."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        }
                    ],
                }
            ]
        }

        properties = get_properties_by_agent(adagents_data, "http://agent1.example.com")
        assert len(properties) == 1
        assert properties[0]["name"] == "Site 1"

    def test_get_properties_by_agent_not_found(self):
        """Should return empty list for unknown agent."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        }
                    ],
                }
            ]
        }

        properties = get_properties_by_agent(adagents_data, "https://unknown-agent.com")
        assert len(properties) == 0


class TestAuthorizationContext:
    """Test AuthorizationContext class."""

    def test_extract_property_ids(self):
        """Should extract property IDs from properties."""
        properties = [
            {
                "id": "prop1",
                "property_type": "website",
                "name": "Site 1",
                "identifiers": [{"type": "domain", "value": "site1.com"}],
            },
            {
                "id": "prop2",
                "property_type": "mobile_app",
                "name": "App 1",
                "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
            },
        ]

        ctx = AuthorizationContext(properties)
        assert ctx.property_ids == ["prop1", "prop2"]

    def test_extract_property_tags(self):
        """Should extract unique tags from properties."""
        properties = [
            {
                "id": "prop1",
                "property_type": "website",
                "name": "Site 1",
                "tags": ["premium", "news"],
            },
            {
                "id": "prop2",
                "property_type": "website",
                "name": "Site 2",
                "tags": ["premium", "sports"],
            },
        ]

        ctx = AuthorizationContext(properties)
        assert set(ctx.property_tags) == {"premium", "news", "sports"}

    def test_deduplicate_tags(self):
        """Should deduplicate tags."""
        properties = [
            {
                "id": "prop1",
                "tags": ["premium", "news"],
            },
            {
                "id": "prop2",
                "tags": ["premium", "sports"],
            },
        ]

        ctx = AuthorizationContext(properties)
        # Each tag should appear only once
        assert ctx.property_tags.count("premium") == 1

    def test_handle_missing_fields(self):
        """Should handle properties without ID or tags."""
        properties = [
            {
                "property_type": "website",
                "name": "Site 1",
            }
        ]

        ctx = AuthorizationContext(properties)
        assert ctx.property_ids == []
        assert ctx.property_tags == []

    def test_raw_properties_preserved(self):
        """Should preserve raw properties data."""
        properties = [
            {
                "id": "prop1",
                "property_type": "website",
                "name": "Site 1",
                "custom_field": "custom_value",
            }
        ]

        ctx = AuthorizationContext(properties)
        assert ctx.raw_properties == properties
        assert ctx.raw_properties[0]["custom_field"] == "custom_value"

    def test_repr(self):
        """Should have useful string representation."""
        properties = [
            {
                "id": "prop1",
                "tags": ["premium"],
            }
        ]

        ctx = AuthorizationContext(properties)
        repr_str = repr(ctx)
        assert "AuthorizationContext" in repr_str
        assert "property_ids" in repr_str
        assert "property_tags" in repr_str


@pytest.mark.asyncio
class TestFetchAgentAuthorizations:
    """Test fetch_agent_authorizations function."""

    async def test_single_publisher_authorized(self):
        """Should return authorization context for authorized publisher."""
        from unittest.mock import patch

        # Mock adagents.json data
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "properties": [
                        {
                            "id": "prop1",
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "nytimes.com"}],
                            "tags": ["premium", "news"],
                        }
                    ],
                }
            ]
        }

        # Mock fetch_adagents to return our test data
        with patch("adcp.adagents.fetch_adagents", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = adagents_data

            contexts = await fetch_agent_authorizations("https://our-agent.com", ["nytimes.com"])

            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            ctx = contexts["nytimes.com"]
            assert ctx.property_ids == ["prop1"]
            assert "premium" in ctx.property_tags
            assert "news" in ctx.property_tags

    async def test_multiple_publishers(self):
        """Should fetch and return contexts for multiple publishers in parallel."""
        from unittest.mock import patch

        # Mock adagents.json data for different publishers
        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "properties": [
                        {
                            "id": "nyt_prop1",
                            "tags": ["news"],
                        }
                    ],
                }
            ]
        }

        wsj_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "properties": [
                        {
                            "id": "wsj_prop1",
                            "tags": ["business"],
                        }
                    ],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                return wsj_data
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            assert len(contexts) == 2
            assert "nytimes.com" in contexts
            assert "wsj.com" in contexts
            assert contexts["nytimes.com"].property_ids == ["nyt_prop1"]
            assert contexts["wsj.com"].property_ids == ["wsj_prop1"]

    async def test_skip_unauthorized_publishers(self):
        """Should skip publishers where agent is not authorized."""
        from unittest.mock import patch

        # nytimes authorizes our agent
        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "properties": [{"id": "prop1"}],
                }
            ]
        }

        # wsj does NOT authorize our agent
        wsj_data = {
            "authorized_agents": [
                {
                    "url": "https://different-agent.com",
                    "properties": [{"id": "prop2"}],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                return wsj_data
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            # Should only include nytimes
            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            assert "wsj.com" not in contexts

    async def test_skip_missing_adagents_json(self):
        """Should silently skip publishers with missing adagents.json."""
        from unittest.mock import patch

        from adcp.exceptions import AdagentsNotFoundError

        # nytimes has adagents.json
        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "properties": [{"id": "prop1"}],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                # wsj doesn't have adagents.json (404)
                raise AdagentsNotFoundError("wsj.com")
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            # Should only include nytimes
            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            assert "wsj.com" not in contexts

    async def test_skip_invalid_adagents_json(self):
        """Should silently skip publishers with invalid adagents.json."""
        from unittest.mock import patch

        from adcp.exceptions import AdagentsValidationError

        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "properties": [{"id": "prop1"}],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                # wsj has invalid adagents.json
                raise AdagentsValidationError("Invalid JSON")
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            # Should only include nytimes
            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            assert "wsj.com" not in contexts

    async def test_empty_result_when_no_authorizations(self):
        """Should return empty dict when no publishers authorize the agent."""
        from unittest.mock import patch

        # No publishers authorize our agent
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://different-agent.com",
                    "properties": [{"id": "prop1"}],
                }
            ]
        }

        with patch("adcp.adagents.fetch_adagents", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = adagents_data

            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            assert len(contexts) == 0
            assert contexts == {}

    async def test_uses_provided_http_client(self):
        """Should use provided HTTP client for connection pooling."""
        from unittest.mock import MagicMock, patch

        import httpx

        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "properties": [{"id": "prop1"}],
                }
            ]
        }

        mock_client = MagicMock(spec=httpx.AsyncClient)

        with patch("adcp.adagents.fetch_adagents", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = adagents_data

            await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com"], client=mock_client
            )

            # Verify fetch_adagents was called with the provided client
            mock_fetch.assert_called_once()
            call_kwargs = mock_fetch.call_args[1]
            assert call_kwargs.get("client") == mock_client
