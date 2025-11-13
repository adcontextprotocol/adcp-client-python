from __future__ import annotations

"""Tests for adagents.json validation functionality."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adcp.adagents import (
    domain_matches,
    fetch_adagents,
    get_all_properties,
    get_all_tags,
    get_properties_by_agent,
    identifiers_match,
    verify_agent_authorization,
    verify_agent_for_property,
)
from adcp.exceptions import (
    AdagentsNotFoundError,
    AdagentsTimeoutError,
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
    """Test fetching adagents.json from publisher domains.

    Note: These tests would require proper httpx mocking or integration testing.
    For now, we focus on unit testing the core logic (domain matching,
    identifier matching, and authorization verification) which are tested above.
    The fetch_adagents function is straightforward HTTP + JSON parsing that
    calls verify_agent_authorization with the parsed data.
    """

    @pytest.mark.skip(reason="Integration test - requires httpx mocking or real HTTP calls")
    @pytest.mark.asyncio
    async def test_fetch_success(self):
        """Should successfully fetch and parse adagents.json."""
        pass


class TestVerifyAgentForProperty:
    """Test convenience wrapper for fetching and verifying in one call.

    Note: These tests would require proper httpx mocking or integration testing.
    The function is a thin wrapper around fetch_adagents + verify_agent_authorization,
    both of which are tested separately above.
    """

    @pytest.mark.skip(reason="Integration test - requires httpx mocking or real HTTP calls")
    @pytest.mark.asyncio
    async def test_verify_success(self):
        """Should fetch and verify authorization successfully."""
        pass


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
