from __future__ import annotations

"""Tests for AdCP registry client."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from adcp.exceptions import RegistryError
from adcp.registry import DEFAULT_REGISTRY_URL, MAX_BULK_DOMAINS, RegistryClient
from adcp.types.core import ResolvedBrand, ResolvedProperty

BRAND_DATA = {
    "canonical_id": "nike.com",
    "canonical_domain": "nike.com",
    "brand_name": "Nike",
    "keller_type": "master",
    "source": "brand_json",
    "brand_manifest": {"name": "Nike"},
}

PROPERTY_DATA = {
    "publisher_domain": "nytimes.com",
    "source": "adagents_json",
    "authorized_agents": [{"url": "https://agent.example.com"}],
    "properties": [{"id": "nyt_main", "type": "website", "name": "NYT Main"}],
    "verified": True,
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
        # external client should not be closed by RegistryClient

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
        mock_client.get = AsyncMock(
            return_value=_mock_response(404, {"error": "Brand not found"})
        )

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
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

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
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"unexpected": "data"})
        )

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
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.lookup_brands(["nike.com"])
        assert exc_info.value.status_code == 500


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
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"unexpected": "data"})
        )

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


class TestRegistryTypes:
    """Test ResolvedBrand and ResolvedProperty Pydantic models."""

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
        assert brand.brand_manifest is None
        assert brand.house_domain is None

    def test_resolved_property_validates(self):
        prop = ResolvedProperty.model_validate(PROPERTY_DATA)
        assert prop.publisher_domain == "nytimes.com"
        assert prop.verified is True

    def test_resolved_property_all_fields(self):
        prop = ResolvedProperty.model_validate(PROPERTY_DATA)
        assert prop.source == "adagents_json"
        assert len(prop.authorized_agents) == 1
        assert len(prop.properties) == 1


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
