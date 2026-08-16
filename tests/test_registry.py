from __future__ import annotations

"""Tests for AdCP registry client."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from adcp.exceptions import RegistryError
from adcp.registry import (
    DEFAULT_REGISTRY_URL,
    MAX_BULK_DOMAINS,
    RegistryClient,
    build_community_mirror_adagents,
)
from adcp.types.core import Member, ResolvedBrand, ResolvedProperty
from adcp.types.registry import (
    CommunityMirrorDeleteResponse,
    CommunityMirrorGetResponse,
    CommunityMirrorListResponse,
    CommunityMirrorPublishResponse,
)

BRAND_DATA = {
    "canonical_id": "nike.com",
    "canonical_domain": "nike.com",
    "brand_name": "Nike",
    "keller_type": "master",
    "source": "brand_json",
    "brand": {"name": "Nike"},
}

PROPERTY_DATA = {
    "publisher_domain": "nytimes.com",
    "source": "adagents_json",
    "authorized_agents": [{"url": "https://agent.example.com"}],
    "properties": [{"id": "nyt_main", "type": "website", "name": "NYT Main"}],
    "verified": True,
}

MEMBER_DATA = {
    "id": "d5b4a558-fdff-4b0c-9876-8327b0d09d7f",
    "slug": "adgentek",
    "display_name": "Adgentek",
    "description": "AI-native advertising infrastructure for the agentic web.",
    "tagline": None,
    "logo_url": "https://example.com/logo.png",
    "logo_light_url": None,
    "logo_dark_url": None,
    "contact_email": "hello@adgentek.ai",
    "contact_website": "https://adgentek.ai",
    "offerings": ["buyer_agent", "sales_agent", "signals_agent"],
    "markets": ["North America", "EMEA"],
    "agents": [],
    "brands": [],
    "is_public": True,
    "is_founding_member": True,
    "featured": False,
    "si_enabled": False,
}


def _mock_response(status_code: int = 200, json_data: object = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


class TestRegistryClientLifecycle:
    """Test RegistryClient lifecycle management."""

    @pytest.mark.asyncio
    async def test_uses_external_client(self):
        external = MagicMock()
        external.get = AsyncMock(return_value=_mock_response(404))
        rc = RegistryClient(client=external)
        await rc.lookup_brand("test.com")
        external.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_owned_client(self):
        rc = RegistryClient()
        client = await rc._get_client()
        assert client is not None
        assert rc._owned_client is client
        await rc.close()
        assert rc._owned_client is None

    @pytest.mark.asyncio
    async def test_close_noop_for_external_client(self):
        external = MagicMock()
        rc = RegistryClient(client=external)
        await rc.close()
        external.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with RegistryClient() as rc:
            client = await rc._get_client()
            assert client is not None
        assert rc._owned_client is None

    def test_default_base_url(self):
        rc = RegistryClient()
        assert rc._base_url == DEFAULT_REGISTRY_URL

    def test_custom_base_url_strips_trailing_slash(self):
        rc = RegistryClient(base_url="https://example.com/")
        assert rc._base_url == "https://example.com"


class TestLookupBrand:
    """Test single brand lookup."""

    @pytest.mark.asyncio
    async def test_resolves_known_domain(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, BRAND_DATA))

        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_brand("nike.com")

        assert result is not None
        assert isinstance(result, ResolvedBrand)
        assert result.canonical_id == "nike.com"
        assert result.brand_name == "Nike"
        assert result.source == "brand_json"

    @pytest.mark.asyncio
    async def test_returns_none_for_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404, {"error": "Brand not found"}))

        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_brand("unknown.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_brand("nike.com")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.lookup_brand("nike.com")

    @pytest.mark.asyncio
    async def test_raises_on_connection_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="failed"):
            await rc.lookup_brand("nike.com")

    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.lookup_brand("nike.com")

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/brands/resolve",
            params={"domain": "nike.com"},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_fresh_lookup_requests_live_origin_check(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(client=mock_client)
        await rc.lookup_brand("nike.com", fresh=True)

        assert mock_client.get.call_args.kwargs["params"] == {
            "domain": "nike.com",
            "fresh": "true",
        }

    @pytest.mark.asyncio
    async def test_http_error_exposes_bounded_recovery_metadata(self):
        response = _mock_response(429, {"code": "RATE_LIMITED", "retry_after": 17})
        response.headers = {"retry-after": "17"}
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_brand("nike.com")

        error = exc_info.value
        assert error.status_code == 429
        assert error.method == "GET"
        assert error.retry_after_seconds == 17
        assert error.details == {"code": "RATE_LIMITED", "retry_after": 17}

    @pytest.mark.asyncio
    async def test_http_error_drops_oversized_details(self):
        response = httpx.Response(
            500,
            json={"message": "x" * (64 * 1024)},
        )
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_brand("nike.com")

        assert exc_info.value.details is None

    @pytest.mark.asyncio
    async def test_returns_none_for_null_body(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, None))

        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_brand("empty.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_extra_fields_preserved(self):
        data = {**BRAND_DATA, "extra_field": "extra_value"}
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, data))

        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_brand("nike.com")
        assert result is not None
        assert result.extra_field == "extra_value"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_raises_on_invalid_response_data(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"unexpected": "data"}))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="invalid response"):
            await rc.lookup_brand("nike.com")


class TestLookupBrands:
    """Test bulk brand lookup."""

    @pytest.mark.asyncio
    async def test_resolves_multiple_domains(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "results": {
                        "nike.com": BRAND_DATA,
                        "unknown.com": None,
                    }
                },
            )
        )

        rc = RegistryClient(client=mock_client)
        results = await rc.lookup_brands(["nike.com", "unknown.com"])

        assert len(results) == 2
        assert isinstance(results["nike.com"], ResolvedBrand)
        assert results["unknown.com"] is None

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_dict(self):
        rc = RegistryClient(client=MagicMock())
        results = await rc.lookup_brands([])
        assert results == {}

    @pytest.mark.asyncio
    async def test_auto_chunks_over_limit(self):
        domains = [f"domain-{i}.com" for i in range(150)]

        call_count = 0

        async def mock_post(url, json, headers, timeout):
            nonlocal call_count
            call_count += 1
            chunk_domains = json["domains"]
            results = {d: None for d in chunk_domains}
            return _mock_response(200, {"results": results})

        mock_client = MagicMock()
        mock_client.post = mock_post

        rc = RegistryClient(client=mock_client)
        results = await rc.lookup_brands(domains)

        assert call_count == 2  # 100 + 50
        assert len(results) == 150

    @pytest.mark.asyncio
    async def test_domain_absent_from_response_defaults_to_none(self):
        """Domain omitted from results dict (not explicitly null) defaults to None."""
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(200, {"results": {"nike.com": BRAND_DATA}})
        )

        rc = RegistryClient(client=mock_client)
        results = await rc.lookup_brands(["nike.com", "other.com"])

        assert isinstance(results["nike.com"], ResolvedBrand)
        assert results["other.com"] is None

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_brands(["nike.com"])
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_chunk_failure_propagates(self):
        """A failure in one chunk raises RegistryError from lookup_brands."""
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(503))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_brands([f"domain-{i}.com" for i in range(150)])
        assert exc_info.value.status_code == 503


class TestLookupProperty:
    """Test single property lookup."""

    @pytest.mark.asyncio
    async def test_resolves_known_domain(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, PROPERTY_DATA))

        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_property("nytimes.com")

        assert result is not None
        assert isinstance(result, ResolvedProperty)
        assert result.publisher_domain == "nytimes.com"
        assert result.source == "adagents_json"
        assert result.verified is True
        assert len(result.authorized_agents) == 1

    @pytest.mark.asyncio
    async def test_returns_none_for_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(404, {"error": "Property not found"})
        )

        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_property("unknown.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_property("nytimes.com")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.lookup_property("nytimes.com")

    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.lookup_property("nytimes.com")

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/properties/resolve",
            params={"domain": "nytimes.com"},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_raises_on_invalid_response_data(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"unexpected": "data"}))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="invalid response"):
            await rc.lookup_property("nytimes.com")


class TestLookupProperties:
    """Test bulk property lookup."""

    @pytest.mark.asyncio
    async def test_resolves_multiple_domains(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "results": {
                        "nytimes.com": PROPERTY_DATA,
                        "unknown.com": None,
                    }
                },
            )
        )

        rc = RegistryClient(client=mock_client)
        results = await rc.lookup_properties(["nytimes.com", "unknown.com"])

        assert len(results) == 2
        assert isinstance(results["nytimes.com"], ResolvedProperty)
        assert results["unknown.com"] is None

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_dict(self):
        rc = RegistryClient(client=MagicMock())
        results = await rc.lookup_properties([])
        assert results == {}

    @pytest.mark.asyncio
    async def test_auto_chunks_over_limit(self):
        domains = [f"pub-{i}.com" for i in range(250)]

        call_count = 0

        async def mock_post(url, json, headers, timeout):
            nonlocal call_count
            call_count += 1
            chunk_domains = json["domains"]
            results = {d: None for d in chunk_domains}
            return _mock_response(200, {"results": results})

        mock_client = MagicMock()
        mock_client.post = mock_post

        rc = RegistryClient(client=mock_client)
        results = await rc.lookup_properties(domains)

        assert call_count == 3  # 100 + 100 + 50
        assert len(results) == 250

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_properties(["nytimes.com"])
        assert exc_info.value.status_code == 500


class TestListMembers:
    """Test AAO member directory listing."""

    @pytest.mark.asyncio
    async def test_lists_members(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"members": [MEMBER_DATA]}))

        rc = RegistryClient(client=mock_client)
        members = await rc.list_members()

        assert len(members) == 1
        assert isinstance(members[0], Member)
        assert members[0].slug == "adgentek"
        assert members[0].display_name == "Adgentek"

    @pytest.mark.asyncio
    async def test_empty_member_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"members": []}))

        rc = RegistryClient(client=mock_client)
        members = await rc.list_members()
        assert members == []

    @pytest.mark.asyncio
    async def test_sends_limit_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"members": []}))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.list_members(limit=25)

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/members",
            params={"limit": 25},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.list_members()
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.list_members()

    @pytest.mark.asyncio
    async def test_raises_on_invalid_limit(self):
        rc = RegistryClient(client=MagicMock())
        with pytest.raises(ValueError, match="limit must be at least 1"):
            await rc.list_members(limit=0)

    @pytest.mark.asyncio
    async def test_missing_members_key_returns_empty_list(self):
        """Malformed 200 response with no 'members' key returns empty list."""
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {}))

        rc = RegistryClient(client=mock_client)
        members = await rc.list_members()
        assert members == []


class TestGetMember:
    """Test AAO member lookup by slug."""

    @pytest.mark.asyncio
    async def test_resolves_known_slug(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, MEMBER_DATA))

        rc = RegistryClient(client=mock_client)
        member = await rc.get_member("adgentek")

        assert member is not None
        assert isinstance(member, Member)
        assert member.slug == "adgentek"
        assert member.contact_email == "hello@adgentek.ai"
        assert "buyer_agent" in member.offerings
        assert member.is_founding_member is True

    @pytest.mark.asyncio
    async def test_returns_none_for_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(client=mock_client)
        member = await rc.get_member("unknown-org")
        assert member is None

    @pytest.mark.asyncio
    async def test_sends_correct_url(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.get_member("adgentek")

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/members/adgentek",
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.get_member("adgentek")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.get_member("adgentek")

    @pytest.mark.asyncio
    async def test_raises_on_invalid_response_data(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"unexpected": "data"}))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="invalid response"):
            await rc.get_member("adgentek")


class TestRegistryTypes:
    """Test ResolvedBrand, ResolvedProperty, and Member Pydantic models."""

    def test_resolved_brand_validates(self):
        brand = ResolvedBrand.model_validate(BRAND_DATA)
        assert brand.canonical_id == "nike.com"
        assert brand.brand_name == "Nike"

    def test_resolved_brand_optional_fields(self):
        minimal = {
            "canonical_id": "x.com",
            "canonical_domain": "x.com",
            "brand_name": "X",
            "source": "community",
        }
        brand = ResolvedBrand.model_validate(minimal)
        assert brand.keller_type is None
        assert brand.brand is None
        assert brand.house_domain is None

    def test_resolved_brand_reads_brand_field(self):
        brand = ResolvedBrand.model_validate(BRAND_DATA)
        assert brand.brand == {"name": "Nike"}

    def test_resolved_property_validates(self):
        prop = ResolvedProperty.model_validate(PROPERTY_DATA)
        assert prop.publisher_domain == "nytimes.com"
        assert prop.verified is True

    def test_resolved_property_all_fields(self):
        prop = ResolvedProperty.model_validate(PROPERTY_DATA)
        assert prop.source == "adagents_json"
        assert len(prop.authorized_agents) == 1
        assert len(prop.properties) == 1

    def test_member_validates(self):
        member = Member.model_validate(MEMBER_DATA)
        assert member.slug == "adgentek"
        assert member.display_name == "Adgentek"
        assert member.is_founding_member is True
        assert len(member.offerings) == 3

    def test_member_defaults(self):
        minimal = {
            "id": "abc123",
            "slug": "test-org",
            "display_name": "Test Org",
        }
        member = Member.model_validate(minimal)
        assert member.offerings == []
        assert member.markets == []
        assert member.is_public is True
        assert member.si_enabled is False


class TestPublicApiExports:
    """Test that registry types are exported from the adcp package."""

    def test_registry_client_exported(self):
        import adcp

        assert adcp.RegistryClient is RegistryClient

    def test_registry_error_exported(self):
        import adcp

        assert adcp.RegistryError is RegistryError

    def test_resolved_brand_exported_from_types(self):
        import adcp.types

        assert adcp.types.ResolvedBrand is ResolvedBrand

    def test_resolved_property_exported_from_types(self):
        import adcp.types

        assert adcp.types.ResolvedProperty is ResolvedProperty

    def test_resolved_brand_exported_from_root(self):
        import adcp

        assert adcp.ResolvedBrand is ResolvedBrand

    def test_resolved_property_exported_from_root(self):
        import adcp

        assert adcp.ResolvedProperty is ResolvedProperty

    def test_member_exported_from_types(self):
        import adcp.types

        assert adcp.types.Member is Member

    def test_member_exported_from_root(self):
        import adcp

        assert adcp.Member is Member


class TestRegistryError:
    """Test RegistryError exception."""

    def test_basic_error(self):
        err = RegistryError("something failed")
        assert "something failed" in str(err)
        assert err.status_code is None

    def test_error_with_status_code(self):
        err = RegistryError("HTTP 500", status_code=500)
        assert err.status_code == 500

    def test_inherits_from_adcp_error(self):
        from adcp.exceptions import ADCPError

        err = RegistryError("test")
        assert isinstance(err, ADCPError)

    def test_max_bulk_domains_constant(self):
        assert MAX_BULK_DOMAINS == 100


# ========================================================================
# Policy Registry Tests
# ========================================================================

from adcp.registry import MAX_BULK_POLICIES
from adcp.types.core import (
    Policy,
    PolicyExemplar,
    PolicyExemplars,
    PolicyHistory,
    PolicyRevision,
    PolicySummary,
)

POLICY_SUMMARY_DATA = {
    "policy_id": "gdpr_consent",
    "version": "1.0.0",
    "name": "GDPR Consent Requirements",
    "description": "Requirements for valid consent under GDPR",
    "category": "regulation",
    "enforcement": "must",
    "jurisdictions": ["EU", "EEA"],
    "region_aliases": {"EU": ["DE", "FR", "IT"]},
    "verticals": ["finance", "healthcare"],
    "channels": ["display", "video"],
    "governance_domains": ["campaign", "creative"],
    "effective_date": "2025-05-25",
    "sunset_date": None,
    "source_url": "https://eur-lex.europa.eu/eli/reg/2016/679/oj",
    "source_name": "EUR-Lex",
    "source_type": "registry",
    "review_status": "approved",
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-06-01T00:00:00Z",
}

POLICY_DATA = {
    **POLICY_SUMMARY_DATA,
    "policy": "Data subjects must provide freely given, specific, informed consent.",
    "guidance": "Consent must be obtained before processing personal data.",
    "exemplars": {
        "pass": [
            {
                "scenario": "Clear opt-in checkbox with explanation",
                "explanation": "User actively consents with full information",
            }
        ],
        "fail": [
            {
                "scenario": "Pre-checked consent box",
                "explanation": "Pre-checked boxes do not constitute valid consent",
            }
        ],
    },
    "ext": {"custom_field": "custom_value"},
}

POLICY_HISTORY_DATA = {
    "policy_id": "gdpr_consent",
    "total": 2,
    "revisions": [
        {
            "revision_number": 2,
            "editor_name": "Pinnacle Media",
            "edit_summary": "Clarified consent requirements for minors",
            "is_rollback": False,
            "created_at": "2025-06-01T00:00:00Z",
        },
        {
            "revision_number": 1,
            "editor_name": "Registry",
            "edit_summary": "Initial policy creation",
            "is_rollback": False,
            "created_at": "2025-01-01T00:00:00Z",
        },
    ],
}


class TestListPolicies:
    """Test policy listing."""

    @pytest.mark.asyncio
    async def test_lists_policies(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": [POLICY_SUMMARY_DATA]})
        )

        rc = RegistryClient(client=mock_client)
        policies = await rc.list_policies()

        assert len(policies) == 1
        assert isinstance(policies[0], PolicySummary)
        assert policies[0].policy_id == "gdpr_consent"
        assert policies[0].enforcement == "must"

    @pytest.mark.asyncio
    async def test_empty_policy_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"policies": []}))

        rc = RegistryClient(client=mock_client)
        policies = await rc.list_policies()
        assert policies == []

    @pytest.mark.asyncio
    async def test_missing_policies_key_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {}))

        rc = RegistryClient(client=mock_client)
        policies = await rc.list_policies()
        assert policies == []

    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"policies": []}))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.list_policies(
            search="gdpr",
            category="regulation",
            enforcement="must",
            jurisdiction="EU",
            vertical="finance",
            domain="campaign",
            limit=10,
            offset=5,
        )

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/policies/registry",
            params={
                "limit": 10,
                "offset": 5,
                "search": "gdpr",
                "category": "regulation",
                "enforcement": "must",
                "jurisdiction": "EU",
                "vertical": "finance",
                "domain": "campaign",
            },
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_omits_none_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"policies": []}))

        rc = RegistryClient(client=mock_client)
        await rc.list_policies(category="standard")

        call_args = mock_client.get.call_args
        params = call_args.kwargs["params"]
        assert "search" not in params
        assert "jurisdiction" not in params
        assert params["category"] == "standard"

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.list_policies()
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.list_policies()


class TestResolvePolicy:
    """Test single policy resolution."""

    @pytest.mark.asyncio
    async def test_resolves_known_policy(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, POLICY_DATA))

        rc = RegistryClient(client=mock_client)
        result = await rc.resolve_policy("gdpr_consent")

        assert result is not None
        assert isinstance(result, Policy)
        assert result.policy_id == "gdpr_consent"
        assert result.enforcement == "must"
        assert "freely given" in result.policy

    @pytest.mark.asyncio
    async def test_returns_none_for_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404, {"error": "Policy not found"}))

        rc = RegistryClient(client=mock_client)
        result = await rc.resolve_policy("unknown_policy")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_null_body(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, None))

        rc = RegistryClient(client=mock_client)
        result = await rc.resolve_policy("empty")
        assert result is None

    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.resolve_policy("gdpr_consent", version="1.0.0")

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/policies/resolve",
            params={"policy_id": "gdpr_consent", "version": "1.0.0"},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_omits_version_when_none(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(client=mock_client)
        await rc.resolve_policy("gdpr_consent")

        call_args = mock_client.get.call_args
        params = call_args.kwargs["params"]
        assert "version" not in params

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.resolve_policy("gdpr_consent")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.resolve_policy("gdpr_consent")

    @pytest.mark.asyncio
    async def test_raises_on_invalid_response_data(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"unexpected": "data"}))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="invalid response"):
            await rc.resolve_policy("gdpr_consent")


class TestResolvePolicies:
    """Test bulk policy resolution."""

    @pytest.mark.asyncio
    async def test_resolves_multiple_policies(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "results": {
                        "gdpr_consent": POLICY_DATA,
                        "unknown_policy": None,
                    }
                },
            )
        )

        rc = RegistryClient(client=mock_client)
        results = await rc.resolve_policies(["gdpr_consent", "unknown_policy"])

        assert len(results) == 2
        assert isinstance(results["gdpr_consent"], Policy)
        assert results["unknown_policy"] is None

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_dict(self):
        rc = RegistryClient(client=MagicMock())
        results = await rc.resolve_policies([])
        assert results == {}

    @pytest.mark.asyncio
    async def test_auto_chunks_over_limit(self):
        policy_ids = [f"policy_{i}" for i in range(150)]

        call_count = 0

        async def mock_post(url, json, headers, timeout):
            nonlocal call_count
            call_count += 1
            chunk_ids = json["policy_ids"]
            results = {pid: None for pid in chunk_ids}
            return _mock_response(200, {"results": results})

        mock_client = MagicMock()
        mock_client.post = mock_post

        rc = RegistryClient(client=mock_client)
        results = await rc.resolve_policies(policy_ids)

        assert call_count == 2  # 100 + 50
        assert len(results) == 150

    @pytest.mark.asyncio
    async def test_policy_absent_from_response_defaults_to_none(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(200, {"results": {"gdpr_consent": POLICY_DATA}})
        )

        rc = RegistryClient(client=mock_client)
        results = await rc.resolve_policies(["gdpr_consent", "other_policy"])

        assert isinstance(results["gdpr_consent"], Policy)
        assert results["other_policy"] is None

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.resolve_policies(["gdpr_consent"])
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_chunk_failure_propagates(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(503))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.resolve_policies([f"policy_{i}" for i in range(150)])
        assert exc_info.value.status_code == 503


class TestPolicyHistory:
    """Test policy history retrieval."""

    @pytest.mark.asyncio
    async def test_retrieves_history(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, POLICY_HISTORY_DATA))

        rc = RegistryClient(client=mock_client)
        result = await rc.policy_history("gdpr_consent")

        assert result is not None
        assert isinstance(result, PolicyHistory)
        assert result.policy_id == "gdpr_consent"
        assert result.total == 2
        assert len(result.revisions) == 2
        assert result.revisions[0].revision_number == 2

    @pytest.mark.asyncio
    async def test_returns_none_for_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(client=mock_client)
        result = await rc.policy_history("unknown_policy")
        assert result is None

    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.policy_history("gdpr_consent", limit=10, offset=5)

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/policies/history",
            params={"policy_id": "gdpr_consent", "limit": 10, "offset": 5},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.policy_history("gdpr_consent")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.policy_history("gdpr_consent")


class TestSavePolicy:
    """Test policy save (authenticated)."""

    @pytest.mark.asyncio
    async def test_saves_policy(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "message": "Policy created",
                    "policy_id": "my_policy",
                    "revision_number": 1,
                },
            )
        )

        rc = RegistryClient(client=mock_client)
        result = await rc.save_policy(
            policy_id="my_policy",
            version="1.0.0",
            name="My Policy",
            category="standard",
            enforcement="should",
            policy="Ads should not appear next to violent content.",
            auth_token="sk_test_123",
        )

        assert result["success"] is True
        assert result["policy_id"] == "my_policy"

    @pytest.mark.asyncio
    async def test_sends_auth_header(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {"success": True, "message": "ok", "policy_id": "x", "revision_number": 1},
            )
        )

        rc = RegistryClient(client=mock_client)
        await rc.save_policy(
            policy_id="x",
            version="1.0.0",
            name="X",
            category="standard",
            enforcement="should",
            policy="text",
            auth_token="sk_secret_key",
        )

        call_args = mock_client.post.call_args
        headers = call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk_secret_key"

    @pytest.mark.asyncio
    async def test_sends_optional_fields(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {"success": True, "message": "ok", "policy_id": "x", "revision_number": 1},
            )
        )

        rc = RegistryClient(client=mock_client)
        await rc.save_policy(
            policy_id="x",
            version="1.0.0",
            name="X",
            category="regulation",
            enforcement="must",
            policy="text",
            auth_token="sk_key",
            jurisdictions=["US"],
            governance_domains=["campaign"],
            guidance="Some guidance",
        )

        call_args = mock_client.post.call_args
        body = call_args.kwargs["json"]
        assert body["jurisdictions"] == ["US"]
        assert body["governance_domains"] == ["campaign"]
        assert body["guidance"] == "Some guidance"
        assert "channels" not in body  # None values omitted

    @pytest.mark.asyncio
    async def test_raises_on_401(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(401))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.save_policy(
                policy_id="x",
                version="1.0.0",
                name="X",
                category="standard",
                enforcement="should",
                policy="text",
                auth_token="bad_token",
            )
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_on_409(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(409))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.save_policy(
                policy_id="gdpr_consent",
                version="1.0.0",
                name="X",
                category="regulation",
                enforcement="must",
                policy="text",
                auth_token="sk_key",
            )
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.save_policy(
                policy_id="x",
                version="1.0.0",
                name="X",
                category="standard",
                enforcement="should",
                policy="text",
                auth_token="sk_key",
            )


class TestPolicyTypes:
    """Test policy Pydantic models."""

    def test_policy_summary_validates(self):
        summary = PolicySummary.model_validate(POLICY_SUMMARY_DATA)
        assert summary.policy_id == "gdpr_consent"
        assert summary.category == "regulation"
        assert summary.jurisdictions == ["EU", "EEA"]
        assert summary.region_aliases == {"EU": ["DE", "FR", "IT"]}

    def test_policy_summary_defaults(self):
        minimal = {
            "policy_id": "test",
            "version": "1.0.0",
            "name": "Test",
            "category": "standard",
            "enforcement": "should",
        }
        summary = PolicySummary.model_validate(minimal)
        assert summary.jurisdictions == []
        assert summary.region_aliases == {}
        assert summary.verticals == []
        assert summary.governance_domains == []
        assert summary.channels is None
        assert summary.description is None
        assert summary.effective_date is None

    def test_policy_validates_with_exemplars(self):
        policy = Policy.model_validate(POLICY_DATA)
        assert policy.policy_id == "gdpr_consent"
        assert "freely given" in policy.policy
        assert policy.exemplars is not None
        assert len(policy.exemplars.pass_) == 1
        assert len(policy.exemplars.fail) == 1
        assert policy.exemplars.pass_[0].scenario == "Clear opt-in checkbox with explanation"
        assert policy.exemplars.fail[0].scenario == "Pre-checked consent box"

    def test_policy_without_exemplars(self):
        data = {
            **POLICY_SUMMARY_DATA,
            "policy": "Some policy text.",
        }
        policy = Policy.model_validate(data)
        assert policy.exemplars is None
        assert policy.guidance is None

    def test_policy_history_validates(self):
        history = PolicyHistory.model_validate(POLICY_HISTORY_DATA)
        assert history.policy_id == "gdpr_consent"
        assert history.total == 2
        assert len(history.revisions) == 2

    def test_policy_revision_validates(self):
        rev = PolicyRevision.model_validate(POLICY_HISTORY_DATA["revisions"][0])
        assert rev.revision_number == 2
        assert rev.editor_name == "Pinnacle Media"
        assert rev.is_rollback is False
        assert rev.rolled_back_to is None

    def test_policy_revision_with_rollback(self):
        data = {
            "revision_number": 3,
            "editor_name": "Admin",
            "edit_summary": "Rolled back to revision 1",
            "is_rollback": True,
            "rolled_back_to": 1,
            "created_at": "2025-07-01T00:00:00Z",
        }
        rev = PolicyRevision.model_validate(data)
        assert rev.is_rollback is True
        assert rev.rolled_back_to == 1

    def test_policy_exemplar_validates(self):
        exemplar = PolicyExemplar.model_validate(
            {"scenario": "Test scenario", "explanation": "Test explanation"}
        )
        assert exemplar.scenario == "Test scenario"

    def test_policy_exemplars_pass_alias(self):
        """The 'pass' field uses alias since 'pass' is a Python keyword."""
        exemplars = PolicyExemplars.model_validate(
            {
                "pass": [{"scenario": "ok", "explanation": "fine"}],
                "fail": [{"scenario": "bad", "explanation": "not fine"}],
            }
        )
        assert len(exemplars.pass_) == 1
        assert len(exemplars.fail) == 1

    def test_policy_summary_extra_fields_preserved(self):
        data = {**POLICY_SUMMARY_DATA, "extra_field": "extra_value"}
        summary = PolicySummary.model_validate(data)
        assert summary.extra_field == "extra_value"  # type: ignore[attr-defined]


class TestPolicyExports:
    """Test that policy types are exported correctly."""

    def test_policy_exported_from_types(self):
        import adcp.types

        assert adcp.types.Policy is Policy

    def test_policy_summary_exported_from_types(self):
        import adcp.types

        assert adcp.types.PolicySummary is PolicySummary

    def test_policy_history_exported_from_types(self):
        import adcp.types

        assert adcp.types.PolicyHistory is PolicyHistory

    def test_policy_revision_exported_from_types(self):
        import adcp.types

        assert adcp.types.PolicyRevision is PolicyRevision

    def test_policy_exemplar_exported_from_types(self):
        import adcp.types

        assert adcp.types.PolicyExemplar is PolicyExemplar

    def test_policy_exported_from_root(self):
        import adcp

        assert adcp.Policy is Policy

    def test_policy_summary_exported_from_root(self):
        import adcp

        assert adcp.PolicySummary is PolicySummary

    def test_policy_history_exported_from_root(self):
        import adcp

        assert adcp.PolicyHistory is PolicyHistory

    def test_max_bulk_policies_constant(self):
        assert MAX_BULK_POLICIES == 100


# Catalog config shared across community-mirror tests. Mirrors the JS fixtures
# in adcp-client#2183 / #2187.
_MIRROR_FORMAT = {
    "format_option_id": "meta-feed-image",
    "format_kind": "image",
    "params": {"width": 1080, "height": 1080},
}
_MIRROR_CONFIG = {
    "catalog_etag": "meta-creative-formats-2026-05",
    "formats": [_MIRROR_FORMAT],
}


def _mirror_get_response(
    *,
    platform: str = "meta",
    superseded_by: object = "https://meta.example/.well-known/adagents.json",
    authorized_agents: object = None,
) -> dict[str, object]:
    """Build a GET /api/registry/mirrors/{platform} wrapper response."""
    return {
        "platform": platform,
        "catalog_etag": "meta-creative-formats-2026-05",
        "superseded_by": superseded_by,
        "adagents_json": {
            "authorized_agents": [] if authorized_agents is None else authorized_agents,
            "catalog_etag": "meta-creative-formats-2026-05",
            "formats": [_MIRROR_FORMAT],
        },
        "created_at": "2026-06-05T12:00:00.000Z",
        "updated_at": "2026-06-05T12:00:00.000Z",
    }


_PUBLISH_RESPONSE = {
    "success": True,
    "platform": "meta",
    "catalog_etag": "meta-creative-formats-2026-05",
    "superseded_by": None,
    "publisher_domains": ["creative.adcontextprotocol.org"],
    "updated_at": "2026-06-05T12:00:00.000Z",
}

_DELETE_RESPONSE = {"success": True, "platform": "meta"}


class TestBuildCommunityMirrorAdagents:
    """Test the catalog builder helper (no I/O)."""

    def test_omits_authorized_agents_and_strips_platform(self):
        catalog = build_community_mirror_adagents(
            {
                "platform": "meta",
                "catalog_etag": "meta-creative-formats-2026-05",
                "formats": [_MIRROR_FORMAT],
                "superseded_by": "https://meta.example/.well-known/adagents.json",
            }
        )

        # The publish body is catalog-only; the service forces authorized_agents: [].
        assert "authorized_agents" not in catalog
        assert "platform" not in catalog
        assert catalog["catalog_etag"] == "meta-creative-formats-2026-05"
        assert catalog["formats"][0]["format_kind"] == "image"
        assert catalog["superseded_by"] == "https://meta.example/.well-known/adagents.json"

    def test_rejects_authorized_agents(self):
        with pytest.raises(RegistryError, match="authorized_agents is not accepted"):
            build_community_mirror_adagents(
                {
                    "authorized_agents": [{"url": "https://agent.example.com"}],
                    "catalog_etag": "x",
                    "formats": [_MIRROR_FORMAT],
                }
            )

    def test_rejects_generator_only_flags(self):
        with pytest.raises(RegistryError, match="include_schema and include_timestamp"):
            build_community_mirror_adagents(
                {
                    "include_schema": False,
                    "catalog_etag": "meta-creative-formats-2026-05",
                    "formats": [_MIRROR_FORMAT],
                }
            )

    def test_requires_catalog_etag(self):
        with pytest.raises(RegistryError, match="catalog_etag is required"):
            build_community_mirror_adagents({"catalog_etag": "  ", "formats": [_MIRROR_FORMAT]})

    def test_requires_non_empty_formats(self):
        with pytest.raises(RegistryError, match="formats must contain at least one"):
            build_community_mirror_adagents({"catalog_etag": "x", "formats": []})


class TestPublishCommunityMirrorAdagents:
    """Test publish_community_mirror_adagents (PUT, authenticated)."""

    @pytest.mark.asyncio
    async def test_puts_catalog_with_auth(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        result = await rc.publish_community_mirror_adagents(
            "Meta",
            {
                "catalog_etag": "meta-creative-formats-2026-05",
                "superseded_by": "https://meta.example/.well-known/adagents.json",
                "properties": [{"domain": "creative.adcontextprotocol.org", "platform": "meta"}],
                "formats": [_MIRROR_FORMAT],
            },
            auth_token="sk_test",
        )

        assert isinstance(result, CommunityMirrorPublishResponse)
        assert result.platform == "meta"
        assert result.success.value is True
        assert result.catalog_etag == "meta-creative-formats-2026-05"
        call = mock_client.request.call_args
        assert call.args[0] == "PUT"
        assert call.args[1].endswith("/api/registry/mirrors/meta")
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk_test"
        body = call.kwargs["json"]
        # catalog-only publish body: authorized_agents is forced server-side
        assert "authorized_agents" not in body
        assert body["catalog_etag"] == "meta-creative-formats-2026-05"
        assert body["superseded_by"] == "https://meta.example/.well-known/adagents.json"
        assert body["formats"][0]["format_kind"] == "image"
        # platform is a routing key, never part of the catalog body
        assert "platform" not in body

    @pytest.mark.asyncio
    async def test_rejects_property_platform_mismatch(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match=r"properties\[\]\.platform must match meta"):
            await rc.publish_community_mirror_adagents(
                "meta",
                {
                    "catalog_etag": "meta-creative-formats-2026-05",
                    "properties": [
                        {"domain": "creative.adcontextprotocol.org", "platform": "google"}
                    ],
                    "formats": [_MIRROR_FORMAT],
                },
                auth_token="sk_test",
            )
        mock_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_empty_platform(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="platform is required"):
            await rc.publish_community_mirror_adagents(
                "   ", dict(_MIRROR_CONFIG), auth_token="sk_test"
            )

    @pytest.mark.asyncio
    async def test_rejects_invalid_platform(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="platform must match"):
            await rc.publish_community_mirror_adagents(
                "bad platform!", dict(_MIRROR_CONFIG), auth_token="sk_test"
            )

    @pytest.mark.asyncio
    async def test_raises_on_401(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(401))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.publish_community_mirror_adagents(
                "meta", dict(_MIRROR_CONFIG), auth_token="bad_token"
            )
        assert exc_info.value.status_code == 401


class TestGetCommunityMirrorAdagents:
    """Test get_community_mirror_adagents (GET, returns None on 404)."""

    @pytest.mark.asyncio
    async def test_returns_stored_catalog(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, _mirror_get_response()))

        rc = RegistryClient(client=mock_client)
        result = await rc.get_community_mirror_adagents("meta")

        url = mock_client.get.call_args.args[0]
        assert url.endswith("/api/registry/mirrors/meta")
        assert isinstance(result, CommunityMirrorGetResponse)
        assert result.platform == "meta"
        assert result.adagents_json.authorized_agents == []
        assert result.adagents_json.catalog_etag == "meta-creative-formats-2026-05"
        assert result.superseded_by == "https://meta.example/.well-known/adagents.json"
        assert result.adagents_json.formats is not None
        assert result.adagents_json.formats[0]["format_option_id"] == "meta-feed-image"

    @pytest.mark.asyncio
    async def test_exposes_wrapper_superseded_by(self):
        # The wrapper carries superseded_by; the inner catalog may omit it.
        response = _mirror_get_response()
        assert "superseded_by" not in response["adagents_json"]  # type: ignore[operator]
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, response))

        rc = RegistryClient(client=mock_client)
        result = await rc.get_community_mirror_adagents("meta")

        assert result is not None
        assert result.superseded_by == "https://meta.example/.well-known/adagents.json"
        assert result.adagents_json.superseded_by is None

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(404, {"error": "Community mirror not found"})
        )

        rc = RegistryClient(client=mock_client)
        result = await rc.get_community_mirror_adagents("meta")

        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_mismatched_platform(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, _mirror_get_response(platform="google"))
        )

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="mismatched community mirror platform"):
            await rc.get_community_mirror_adagents("meta")

    @pytest.mark.asyncio
    async def test_rejects_non_catalog_mirror(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                _mirror_get_response(authorized_agents=[{"url": "https://agent.example.com"}]),
            )
        )

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="invalid response"):
            await rc.get_community_mirror_adagents("meta")

    @pytest.mark.asyncio
    async def test_rejects_malformed_success_response(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"platform": "meta"}))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="invalid response"):
            await rc.get_community_mirror_adagents("meta")

    @pytest.mark.asyncio
    async def test_rejects_invalid_platform(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, _mirror_get_response()))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="platform must match"):
            await rc.get_community_mirror_adagents("bad platform!")
        mock_client.get.assert_not_called()


class TestListCommunityMirrorAdagents:
    """Test list_community_mirror_adagents (GET)."""

    @pytest.mark.asyncio
    async def test_lists_without_pagination(self):
        listed = {
            "mirrors": [
                {
                    "platform": "meta",
                    "catalog_etag": "meta-creative-formats-2026-05",
                    "superseded_by": None,
                    "updated_at": "2026-06-05T12:00:00.000Z",
                }
            ],
            "total": 1,
        }
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, listed))

        rc = RegistryClient(client=mock_client)
        result = await rc.list_community_mirror_adagents()

        call = mock_client.get.call_args
        assert call.args[0].endswith("/api/registry/mirrors")
        assert call.kwargs["params"] is None
        assert isinstance(result, CommunityMirrorListResponse)
        assert result.total == 1
        assert result.mirrors[0].platform == "meta"
        assert result.mirrors[0].catalog_etag == "meta-creative-formats-2026-05"

    @pytest.mark.asyncio
    async def test_encodes_pagination(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"mirrors": [], "total": 0}))

        rc = RegistryClient(client=mock_client)
        await rc.list_community_mirror_adagents(limit=25, offset=50)

        call = mock_client.get.call_args
        assert call.args[0].endswith("/api/registry/mirrors")
        assert call.kwargs["params"] == {"limit": 25, "offset": 50}


class TestUpsertCommunityMirrorAdagents:
    """Test upsert_community_mirror_adagents (platform inference + PUT)."""

    @pytest.mark.asyncio
    async def test_upserts_with_explicit_platform_kwarg(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        result = await rc.upsert_community_mirror_adagents(
            dict(_MIRROR_CONFIG), platform="Meta", auth_token="sk_test"
        )

        assert isinstance(result, CommunityMirrorPublishResponse)
        assert result.platform == "meta"
        call = mock_client.request.call_args
        assert call.args[0] == "PUT"
        assert call.args[1].endswith("/api/registry/mirrors/meta")
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk_test"
        assert "platform" not in call.kwargs["json"]

    @pytest.mark.asyncio
    async def test_infers_platform_from_config(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        await rc.upsert_community_mirror_adagents(
            {
                "platform": "Meta",
                "catalog_etag": "meta-creative-formats-2026-05",
                "formats": [_MIRROR_FORMAT],
            },
            auth_token="sk_test",
        )

        assert mock_client.request.call_args.args[1].endswith("/api/registry/mirrors/meta")

    @pytest.mark.asyncio
    async def test_infers_platform_from_single_property(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        await rc.upsert_community_mirror_adagents(
            {
                "catalog_etag": "meta-creative-formats-2026-05",
                "properties": [{"domain": "creative.adcontextprotocol.org", "platform": "Meta"}],
                "formats": [_MIRROR_FORMAT],
            },
            auth_token="sk_test",
        )

        assert mock_client.request.call_args.args[1].endswith("/api/registry/mirrors/meta")

    @pytest.mark.asyncio
    async def test_requires_platform_identity(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(
            RegistryError, match="platform is required for community mirror publish"
        ):
            await rc.upsert_community_mirror_adagents(dict(_MIRROR_CONFIG), auth_token="sk_test")
        mock_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_ambiguous_property_platforms(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _PUBLISH_RESPONSE))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="platform is ambiguous"):
            await rc.upsert_community_mirror_adagents(
                {
                    "catalog_etag": "meta-creative-formats-2026-05",
                    "properties": [
                        {"domain": "creative.adcontextprotocol.org", "platform": "meta"},
                        {"domain": "creative.adcontextprotocol.org", "platform": "tiktok"},
                    ],
                    "formats": [_MIRROR_FORMAT],
                },
                auth_token="sk_test",
            )
        mock_client.request.assert_not_called()


class TestDeleteCommunityMirrorAdagents:
    """Test delete_community_mirror_adagents (DELETE, authenticated)."""

    @pytest.mark.asyncio
    async def test_deletes_with_auth(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _DELETE_RESPONSE))

        rc = RegistryClient(client=mock_client)
        result = await rc.delete_community_mirror_adagents("Meta", auth_token="sk_test")

        assert isinstance(result, CommunityMirrorDeleteResponse)
        assert result.success.value is True
        assert result.platform == "meta"
        call = mock_client.request.call_args
        assert call.args[0] == "DELETE"
        assert call.args[1].endswith("/api/registry/mirrors/meta")
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk_test"
        # No force param unless requested.
        assert call.kwargs["params"] is None

    @pytest.mark.asyncio
    async def test_passes_force_param(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _DELETE_RESPONSE))

        rc = RegistryClient(client=mock_client)
        await rc.delete_community_mirror_adagents("meta", force=True, auth_token="sk_test")

        call = mock_client.request.call_args
        assert call.kwargs["params"] == {"force": "true"}

    @pytest.mark.asyncio
    async def test_maps_409_not_superseded_to_registry_error(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            return_value=_mock_response(409, {"error": "Mirror has not been superseded"})
        )

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.delete_community_mirror_adagents("meta", auth_token="sk_test")
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_rejects_invalid_platform(self):
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_mock_response(200, _DELETE_RESPONSE))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="platform must match"):
            await rc.delete_community_mirror_adagents("bad platform!", auth_token="sk_test")
        mock_client.request.assert_not_called()
