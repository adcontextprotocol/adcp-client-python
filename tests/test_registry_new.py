"""Tests for new registry client methods (brand CRUD, property CRUD,
agent discovery, lookups, validation, search, probing, change feed).

Tests follow the same patterns as test_registry.py:
- Happy path with Pydantic model validation
- 404 → None (where applicable)
- Server error (500) → RegistryError with status_code
- Timeout → RegistryError with "timed out"
- Auth header verification (for auth methods)
- Parameter/body verification
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from adcp.exceptions import RegistryError
from adcp.registry import RegistryClient
from adcp.types.registry import (
    BrandActivity,
    BrandRegistryItem,
    DomainLookupResult,
    FederatedAgentWithDetails,
    FederatedPublisher,
    FeedPage,
    PropertyActivity,
    PropertyRegistryItem,
    ValidationResult,
)


def _mock_response(status_code: int = 200, json_data: object = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ========================================================================
# _request helper tests (covers GET, POST, error paths for all methods)
# ========================================================================


class TestRequestHelper:
    """Test the _request helper that all new methods delegate to."""

    @pytest.mark.asyncio
    async def test_get_returns_response(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"ok": True}))
        rc = RegistryClient(client=mock_client)
        result = await rc.get_registry_stats()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_get_500_raises_with_status_code(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.get_registry_stats()
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_get_timeout_raises(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.get_registry_stats()

    @pytest.mark.asyncio
    async def test_get_connection_error_raises(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="failed"):
            await rc.get_registry_stats()

    @pytest.mark.asyncio
    async def test_post_500_raises_with_status_code(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(500))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.validate_adagents("pub.com")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_post_timeout_raises(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("t"))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.validate_adagents("pub.com")

    @pytest.mark.asyncio
    async def test_allow_404_returns_none(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))
        rc = RegistryClient(client=mock_client)
        assert await rc.get_brand_json("x.com") is None

    @pytest.mark.asyncio
    async def test_expected_status_set_accepts_202(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(202, {"success": True}))
        rc = RegistryClient(client=mock_client)
        result = await rc.request_crawl("pub.com", auth_token="sk")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_expected_status_set_accepts_200(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200, {"success": True}))
        rc = RegistryClient(client=mock_client)
        result = await rc.request_crawl("pub.com", auth_token="sk")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_auth_token_sets_bearer_header(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200, {"success": True}))
        rc = RegistryClient(client=mock_client)
        await rc.save_brand("x.com", "X", auth_token="sk_secret")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk_secret"
        assert headers["User-Agent"] == "adcp-client-python"


# ========================================================================
# Parametrized error tests: 500 and timeout for every new method
# ========================================================================


# Each tuple: (method_name, http_method, call_args, call_kwargs)
# call_args are positional args to the method after self
# call_kwargs are keyword args

GET_METHODS = [
    ("get_brand_json", ("acme.com",), {}),
    ("list_brands", (), {}),
    ("brand_history", ("acme.com",), {}),
    ("enrich_brand", ("acme.com",), {}),
    ("list_properties", (), {}),
    ("property_history", ("acme.com",), {}),
    ("get_property_check_report", ("rpt-1",), {}),
    ("list_agents", (), {}),
    ("list_publishers", (), {}),
    ("get_registry_stats", (), {}),
    ("search_agents", (), {"auth_token": "sk"}),
    ("lookup_domain", ("pub.com",), {}),
    ("lookup_property_identifier", ("domain", "pub.com"), {}),
    ("get_agent_domains", ("https://agent.com",), {}),
    (
        "validate_property_authorization",
        ("https://agent.com", "domain", "pub.com"),
        {},
    ),
    ("api_discovery", (), {}),
    ("search", ("acme",), {}),
    ("lookup_manifest_ref", ("acme.com",), {}),
    ("discover_agent", ("https://agent.com",), {}),
    ("get_agent_formats", ("https://agent.com",), {}),
    ("get_agent_products", ("https://agent.com",), {}),
    ("validate_publisher", ("pub.com",), {}),
    ("get_feed", (), {"auth_token": "sk"}),
    ("validate_property", ("pub.com",), {}),
]

POST_METHODS = [
    ("save_brand", ("acme.com", "Acme"), {"auth_token": "sk"}),
    (
        "save_property",
        ("pub.com", [{"url": "https://a.com"}]),
        {"auth_token": "sk"},
    ),
    ("check_property_list", (["pub.com"],), {}),
    ("request_crawl", ("pub.com",), {"auth_token": "sk"}),
    (
        "validate_product_authorization",
        ("https://a.com", [{"publisher_domain": "pub.com"}]),
        {},
    ),
    (
        "expand_product_identifiers",
        ("https://a.com", [{"publisher_domain": "pub.com"}]),
        {},
    ),
    ("validate_adagents", ("pub.com",), {}),
    ("create_adagents", ([{"url": "https://a.com"}],), {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,args,kwargs",
    GET_METHODS,
    ids=[m[0] for m in GET_METHODS],
)
async def test_get_method_500_raises(method_name, args, kwargs):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_mock_response(500))
    rc = RegistryClient(client=mock_client)
    with pytest.raises(RegistryError) as exc_info:
        await getattr(rc, method_name)(*args, **kwargs)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,args,kwargs",
    GET_METHODS,
    ids=[m[0] for m in GET_METHODS],
)
async def test_get_method_timeout_raises(method_name, args, kwargs):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("t"))
    rc = RegistryClient(client=mock_client)
    with pytest.raises(RegistryError, match="timed out"):
        await getattr(rc, method_name)(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,args,kwargs",
    POST_METHODS,
    ids=[m[0] for m in POST_METHODS],
)
async def test_post_method_500_raises(method_name, args, kwargs):
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=_mock_response(500))
    rc = RegistryClient(client=mock_client)
    with pytest.raises(RegistryError) as exc_info:
        await getattr(rc, method_name)(*args, **kwargs)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,args,kwargs",
    POST_METHODS,
    ids=[m[0] for m in POST_METHODS],
)
async def test_post_method_timeout_raises(method_name, args, kwargs):
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("t"))
    rc = RegistryClient(client=mock_client)
    with pytest.raises(RegistryError, match="timed out"):
        await getattr(rc, method_name)(*args, **kwargs)


# ========================================================================
# Brand Registry Operations
# ========================================================================


class TestGetBrandJson:
    @pytest.mark.asyncio
    async def test_returns_data(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "domain": "acme.com",
                    "url": "https://acme.com/.well-known/brand.json",
                    "data": {"brand_name": "Acme"},
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.get_brand_json("acme.com")
        assert result is not None
        assert result["domain"] == "acme.com"
        assert result["data"]["brand_name"] == "Acme"

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))
        rc = RegistryClient(client=mock_client)
        assert await rc.get_brand_json("unknown.com") is None

    @pytest.mark.asyncio
    async def test_sends_fresh_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"domain": "x", "url": "x", "data": {}})
        )
        rc = RegistryClient(client=mock_client)
        await rc.get_brand_json("acme.com", fresh=True)
        params = mock_client.get.call_args.kwargs["params"]
        assert params["fresh"] == "true"
        assert params["domain"] == "acme.com"


class TestSaveBrand:
    @pytest.mark.asyncio
    async def test_saves_brand(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "domain": "acme.com",
                    "id": "123",
                    "message": "ok",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.save_brand("acme.com", "Acme Corp", auth_token="sk_test")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_sends_auth_header(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "domain": "x",
                    "id": "1",
                    "message": "ok",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.save_brand("acme.com", "Acme", auth_token="sk_secret")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk_secret"

    @pytest.mark.asyncio
    async def test_sends_body_with_manifest(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "domain": "x",
                    "id": "1",
                    "message": "ok",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.save_brand(
            "acme.com",
            "Acme",
            auth_token="sk",
            brand_manifest={"logo": "https://acme.com/logo.png"},
        )
        body = mock_client.post.call_args.kwargs["json"]
        assert body["domain"] == "acme.com"
        assert body["brand_name"] == "Acme"
        assert body["brand_manifest"]["logo"] == "https://acme.com/logo.png"

    @pytest.mark.asyncio
    async def test_raises_on_401(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(401))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.save_brand("acme.com", "Acme", auth_token="bad")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_on_409(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(409))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.save_brand("acme.com", "Acme", auth_token="sk")
        assert exc_info.value.status_code == 409


class TestListBrands:
    @pytest.mark.asyncio
    async def test_returns_brands(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "brands": [
                        {
                            "domain": "acme.com",
                            "source": "brand_json",
                            "has_manifest": True,
                            "verified": True,
                        },
                    ],
                    "stats": {"total": 1},
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.list_brands()
        assert len(result) == 1
        assert isinstance(result[0], BrandRegistryItem)
        assert result[0].domain == "acme.com"

    @pytest.mark.asyncio
    async def test_sends_search_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"brands": [], "stats": {}}))
        rc = RegistryClient(client=mock_client)
        await rc.list_brands(search="acme", limit=50, offset=10)
        params = mock_client.get.call_args.kwargs["params"]
        assert params["search"] == "acme"
        assert params["limit"] == 50
        assert params["offset"] == 10

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_brands(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"brands": [], "stats": {}}))
        rc = RegistryClient(client=mock_client)
        assert await rc.list_brands() == []


class TestBrandHistory:
    @pytest.mark.asyncio
    async def test_returns_history(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "domain": "acme.com",
                    "total": 2,
                    "revisions": [
                        {
                            "revision_number": 2,
                            "editor_name": "test",
                            "edit_summary": "Updated",
                            "is_rollback": False,
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                    ],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.brand_history("acme.com")
        assert isinstance(result, BrandActivity)
        assert result.total == 2
        assert result.domain == "acme.com"

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))
        rc = RegistryClient(client=mock_client)
        assert await rc.brand_history("unknown.com") is None


class TestEnrichBrand:
    @pytest.mark.asyncio
    async def test_returns_enrichment(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "domain": "acme.com",
                    "enrichment": {"logos": []},
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.enrich_brand("acme.com")
        assert result["domain"] == "acme.com"

    @pytest.mark.asyncio
    async def test_sends_domain_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        await rc.enrich_brand("acme.com")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["domain"] == "acme.com"


# ========================================================================
# Property Registry Operations
# ========================================================================


class TestListProperties:
    @pytest.mark.asyncio
    async def test_returns_properties(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "properties": [
                        {
                            "domain": "pub.com",
                            "source": "adagents_json",
                            "property_count": 5,
                            "agent_count": 2,
                            "verified": True,
                        },
                    ],
                    "stats": {"total": 1},
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.list_properties()
        assert len(result) == 1
        assert isinstance(result[0], PropertyRegistryItem)

    @pytest.mark.asyncio
    async def test_sends_search_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"properties": [], "stats": {}})
        )
        rc = RegistryClient(client=mock_client)
        await rc.list_properties(search="news", limit=25)
        params = mock_client.get.call_args.kwargs["params"]
        assert params["search"] == "news"
        assert params["limit"] == 25

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"properties": [], "stats": {}})
        )
        rc = RegistryClient(client=mock_client)
        assert await rc.list_properties() == []


class TestValidateProperty:
    @pytest.mark.asyncio
    async def test_returns_validation(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"valid": True, "domain": "pub.com"})
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.validate_property("pub.com")
        assert isinstance(result, ValidationResult)
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_sends_domain_as_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"valid": True}))
        rc = RegistryClient(client=mock_client)
        await rc.validate_property("pub.com")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["domain"] == "pub.com"


class TestSaveProperty:
    @pytest.mark.asyncio
    async def test_saves_property(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "message": "ok",
                    "id": "123",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.save_property(
            "pub.com", [{"url": "https://agent.example.com"}], auth_token="sk_test"
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_sends_auth_header(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(200, {"success": True, "message": "", "id": ""})
        )
        rc = RegistryClient(client=mock_client)
        await rc.save_property("pub.com", [{"url": "https://a.com"}], auth_token="sk_key")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk_key"

    @pytest.mark.asyncio
    async def test_sends_optional_fields(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(200, {"success": True, "message": "", "id": ""})
        )
        rc = RegistryClient(client=mock_client)
        await rc.save_property(
            "pub.com",
            [{"url": "https://a.com"}],
            auth_token="sk",
            properties=[{"type": "website", "name": "My Site"}],
            contact={"name": "Admin", "email": "admin@pub.com"},
        )
        body = mock_client.post.call_args.kwargs["json"]
        assert body["properties"] == [{"type": "website", "name": "My Site"}]
        assert body["contact"]["email"] == "admin@pub.com"

    @pytest.mark.asyncio
    async def test_raises_on_401(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(401))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.save_property("pub.com", [], auth_token="bad")
        assert exc_info.value.status_code == 401


class TestPropertyHistory:
    @pytest.mark.asyncio
    async def test_returns_history(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "domain": "pub.com",
                    "total": 1,
                    "revisions": [
                        {
                            "revision_number": 1,
                            "editor_name": "test",
                            "edit_summary": "Created",
                            "is_rollback": False,
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                    ],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.property_history("pub.com")
        assert isinstance(result, PropertyActivity)

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))
        rc = RegistryClient(client=mock_client)
        assert await rc.property_history("unknown.com") is None


class TestCheckPropertyList:
    @pytest.mark.asyncio
    async def test_returns_results(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "summary": {"total": 2, "remove": 0, "modify": 0, "assess": 1, "ok": 1},
                    "remove": [],
                    "modify": [],
                    "assess": [{"domain": "unknown.com"}],
                    "ok": [{"domain": "nytimes.com", "source": "adagents_json"}],
                    "report_id": "rpt-123",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.check_property_list(["nytimes.com", "unknown.com"])
        assert result["report_id"] == "rpt-123"
        assert result["summary"]["ok"] == 1

    @pytest.mark.asyncio
    async def test_sends_domains_in_body(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "summary": {"total": 0, "remove": 0, "modify": 0, "assess": 0, "ok": 0},
                    "remove": [],
                    "modify": [],
                    "assess": [],
                    "ok": [],
                    "report_id": "x",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.check_property_list(["a.com", "b.com"])
        body = mock_client.post.call_args.kwargs["json"]
        assert body["domains"] == ["a.com", "b.com"]


class TestGetPropertyCheckReport:
    @pytest.mark.asyncio
    async def test_returns_report(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "summary": {"total": 1, "remove": 0, "modify": 0, "assess": 0, "ok": 1},
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.get_property_check_report("rpt-123")
        assert result is not None
        assert result["summary"]["ok"] == 1

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))
        rc = RegistryClient(client=mock_client)
        assert await rc.get_property_check_report("bad-id") is None


# ========================================================================
# Agent Discovery
# ========================================================================


class TestListAgents:
    @pytest.mark.asyncio
    async def test_returns_agents(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "agents": [
                        {"url": "https://agent.example.com", "name": "Test", "type": "creative"},
                    ],
                    "count": 1,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.list_agents()
        assert len(result) == 1
        assert isinstance(result[0], FederatedAgentWithDetails)
        assert result[0].name == "Test"

    @pytest.mark.asyncio
    async def test_sends_all_boolean_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"agents": []}))
        rc = RegistryClient(client=mock_client)
        await rc.list_agents(
            type="creative",
            health=True,
            capabilities=True,
            properties=True,
            compliance=True,
        )
        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "creative"
        assert params["health"] == "true"
        assert params["capabilities"] == "true"
        assert params["properties"] == "true"
        assert params["compliance"] == "true"

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"agents": []}))
        rc = RegistryClient(client=mock_client)
        assert await rc.list_agents() == []


class TestListPublishers:
    @pytest.mark.asyncio
    async def test_returns_publishers(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "publishers": [{"domain": "pub.com"}],
                    "count": 1,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.list_publishers()
        assert len(result) == 1
        assert isinstance(result[0], FederatedPublisher)

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {"publishers": []}))
        rc = RegistryClient(client=mock_client)
        assert await rc.list_publishers() == []


class TestSearchAgents:
    @pytest.mark.asyncio
    async def test_sends_filters(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "results": [],
                    "cursor": None,
                    "has_more": False,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.search_agents(
            auth_token="sk",
            channels="ctv,olv",
            markets="US",
            has_tmp=True,
            min_properties=10,
        )
        params = mock_client.get.call_args.kwargs["params"]
        assert params["channels"] == "ctv,olv"
        assert params["markets"] == "US"
        assert params["has_tmp"] == "true"
        assert params["min_properties"] == 10

    @pytest.mark.asyncio
    async def test_sends_auth_header(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "results": [],
                    "cursor": None,
                    "has_more": False,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.search_agents(auth_token="sk_secret")
        headers = mock_client.get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk_secret"

    @pytest.mark.asyncio
    async def test_raises_on_401(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(401))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.search_agents(auth_token="bad")
        assert exc_info.value.status_code == 401


class TestRequestCrawl:
    @pytest.mark.asyncio
    async def test_accepts_202(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(202, {"success": True, "message": "Crawl requested"})
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.request_crawl("pub.com", auth_token="sk_test")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_sends_auth_header(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(202, {"success": True}))
        rc = RegistryClient(client=mock_client)
        await rc.request_crawl("pub.com", auth_token="sk_key")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk_key"

    @pytest.mark.asyncio
    async def test_raises_on_401(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(401))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.request_crawl("pub.com", auth_token="bad")
        assert exc_info.value.status_code == 401


# ========================================================================
# Lookups & Authorization
# ========================================================================


class TestLookupDomain:
    @pytest.mark.asyncio
    async def test_returns_lookup(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "domain": "pub.com",
                    "authorized_agents": [{"url": "https://agent.com"}],
                    "sales_agents_claiming": [],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_domain("pub.com")
        assert isinstance(result, DomainLookupResult)
        assert result.domain == "pub.com"
        assert len(result.authorized_agents) == 1

    @pytest.mark.asyncio
    async def test_url_encodes_domain(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "domain": "pub.com",
                    "authorized_agents": [],
                    "sales_agents_claiming": [],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.lookup_domain("pub.com")
        url = mock_client.get.call_args.args[0]
        assert "/api/registry/lookup/domain/pub.com" in url


class TestLookupPropertyIdentifier:
    @pytest.mark.asyncio
    async def test_returns_result(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "identifier_type": "domain",
                    "identifier_value": "pub.com",
                    "agents": [{"url": "https://agent.com"}],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_property_identifier("domain", "pub.com")
        assert result["identifier_type"] == "domain"

    @pytest.mark.asyncio
    async def test_sends_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        await rc.lookup_property_identifier("app_id", "com.example.app")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "app_id"
        assert params["value"] == "com.example.app"


class TestValidateProductAuthorization:
    @pytest.mark.asyncio
    async def test_returns_result(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "authorized": True,
                    "agent_url": "https://agent.com",
                    "results": [{"authorized": True}],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.validate_product_authorization(
            "https://agent.com", [{"publisher_domain": "pub.com"}]
        )
        assert result["authorized"] is True

    @pytest.mark.asyncio
    async def test_sends_body(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        props = [{"publisher_domain": "pub.com", "property_types": ["website"]}]
        await rc.validate_product_authorization("https://agent.com", props)
        body = mock_client.post.call_args.kwargs["json"]
        assert body["agent_url"] == "https://agent.com"
        assert body["publisher_properties"] == props


class TestExpandProductIdentifiers:
    @pytest.mark.asyncio
    async def test_returns_result(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "agent_url": "https://agent.com",
                    "expanded": [{"domain": "pub.com", "identifiers": []}],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.expand_product_identifiers(
            "https://agent.com", [{"publisher_domain": "pub.com"}]
        )
        assert result["agent_url"] == "https://agent.com"

    @pytest.mark.asyncio
    async def test_sends_body(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        props = [{"publisher_domain": "pub.com"}]
        await rc.expand_product_identifiers("https://agent.com", props)
        body = mock_client.post.call_args.kwargs["json"]
        assert body["agent_url"] == "https://agent.com"
        assert body["publisher_properties"] == props


class TestValidatePropertyAuthorization:
    @pytest.mark.asyncio
    async def test_returns_result(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "agent_url": "https://agent.com",
                    "identifier_type": "domain",
                    "identifier_value": "pub.com",
                    "authorized": True,
                    "checked_at": "2026-01-01T00:00:00Z",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.validate_property_authorization("https://agent.com", "domain", "pub.com")
        assert result["authorized"] is True

    @pytest.mark.asyncio
    async def test_sends_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        await rc.validate_property_authorization("https://agent.com", "domain", "pub.com")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["agent_url"] == "https://agent.com"
        assert params["identifier_type"] == "domain"
        assert params["identifier_value"] == "pub.com"


class TestGetAgentDomains:
    @pytest.mark.asyncio
    async def test_returns_domains(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "agent_url": "https://agent.com",
                    "properties": [{"domain": "pub.com"}],
                    "identifiers": [],
                    "property_count": 1,
                    "identifier_count": 0,
                    "generated_at": "2026-01-01T00:00:00Z",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.get_agent_domains("https://agent.com")
        assert result["property_count"] == 1

    @pytest.mark.asyncio
    async def test_url_encodes_agent_url(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "agent_url": "https://agent.com",
                    "properties": [],
                    "identifiers": [],
                    "property_count": 0,
                    "identifier_count": 0,
                    "generated_at": "",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.get_agent_domains("https://agent.com/path")
        url = mock_client.get.call_args.args[0]
        # URL should contain the encoded agent URL
        assert "https%3A%2F%2Fagent.com%2Fpath" in url


# ========================================================================
# Validation Tools
# ========================================================================


class TestValidateAdagents:
    @pytest.mark.asyncio
    async def test_returns_validation(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "data": {"domain": "pub.com", "found": True},
                    "timestamp": "2026-01-01T00:00:00Z",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.validate_adagents("pub.com")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_sends_domain_in_body(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        await rc.validate_adagents("pub.com")
        body = mock_client.post.call_args.kwargs["json"]
        assert body["domain"] == "pub.com"


class TestCreateAdagents:
    @pytest.mark.asyncio
    async def test_returns_generated(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "data": {"success": True},
                    "timestamp": "2026-01-01T00:00:00Z",
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.create_adagents([{"url": "https://agent.com"}])
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_sends_optional_params(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        await rc.create_adagents(
            [{"url": "https://agent.com"}],
            include_schema=True,
            include_timestamp=True,
            properties=[{"type": "website", "name": "My Site"}],
        )
        body = mock_client.post.call_args.kwargs["json"]
        assert body["include_schema"] is True
        assert body["include_timestamp"] is True
        assert body["properties"] == [{"type": "website", "name": "My Site"}]


# ========================================================================
# Search
# ========================================================================


class TestApiDiscovery:
    @pytest.mark.asyncio
    async def test_returns_discovery(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "name": "AAO Registry",
                    "version": "1.0.0",
                    "documentation": "https://docs.example.com",
                    "openapi": "/openapi/registry.yaml",
                    "endpoints": {},
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.api_discovery()
        assert result["name"] == "AAO Registry"


class TestSearch:
    @pytest.mark.asyncio
    async def test_returns_results(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "brands": [{"domain": "acme.com"}],
                    "publishers": [],
                    "properties": [],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.search("acme")
        assert len(result["brands"]) == 1

    @pytest.mark.asyncio
    async def test_sends_query_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "brands": [],
                    "publishers": [],
                    "properties": [],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.search("test query")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["q"] == "test query"


class TestLookupManifestRef:
    @pytest.mark.asyncio
    async def test_returns_ref(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "found": True,
                    "reference": {
                        "reference_type": "url",
                        "manifest_url": "https://acme.com/.well-known/brand.json",
                        "agent_url": None,
                        "agent_id": None,
                        "verification_status": "valid",
                    },
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.lookup_manifest_ref("acme.com")
        assert result["found"] is True

    @pytest.mark.asyncio
    async def test_sends_type_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"success": False, "found": False})
        )
        rc = RegistryClient(client=mock_client)
        await rc.lookup_manifest_ref("acme.com", type="brand.json")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["type"] == "brand.json"
        assert params["domain"] == "acme.com"


# ========================================================================
# Agent Probing
# ========================================================================


class TestDiscoverAgent:
    @pytest.mark.asyncio
    async def test_returns_agent_info(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "name": "Test Agent",
                    "protocols": ["mcp"],
                    "type": "creative",
                    "stats": {"format_count": 5},
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.discover_agent("https://agent.example.com")
        assert result["name"] == "Test Agent"

    @pytest.mark.asyncio
    async def test_sends_url_param(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {}))
        rc = RegistryClient(client=mock_client)
        await rc.discover_agent("https://agent.example.com")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["url"] == "https://agent.example.com"


class TestGetAgentFormats:
    @pytest.mark.asyncio
    async def test_returns_formats(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "formats": [{"format_id": "banner_300x250"}],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.get_agent_formats("https://agent.example.com")
        assert len(result["formats"]) == 1


class TestGetAgentProducts:
    @pytest.mark.asyncio
    async def test_returns_products(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "success": True,
                    "products": [{"product_id": "premium_video"}],
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.get_agent_products("https://agent.example.com")
        assert len(result["products"]) == 1


class TestValidatePublisher:
    @pytest.mark.asyncio
    async def test_returns_validation(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "valid": True,
                    "domain": "pub.com",
                    "agent_count": 2,
                    "property_count": 5,
                    "property_type_counts": {"website": 5},
                    "tag_count": 3,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.validate_publisher("pub.com")
        assert result["valid"] is True
        assert result["agent_count"] == 2


# ========================================================================
# Change Feed
# ========================================================================


class TestGetFeed:
    @pytest.mark.asyncio
    async def test_returns_feed_page(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "events": [
                        {
                            "event_id": "evt-1",
                            "event_type": "property.created",
                            "entity_type": "property",
                            "entity_id": "pub.com",
                            "payload": {},
                            "actor": "system",
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                    ],
                    "cursor": "evt-1",
                    "has_more": False,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.get_feed(auth_token="sk_test")
        assert isinstance(result, FeedPage)
        assert len(result.events) == 1
        assert result.events[0].event_type == "property.created"
        assert result.events[0].event_id == "evt-1"
        assert result.cursor == "evt-1"
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_sends_auth_and_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "events": [],
                    "cursor": None,
                    "has_more": False,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        await rc.get_feed(
            auth_token="sk_secret",
            cursor="cur-1",
            types="property.*",
            limit=50,
        )
        call_args = mock_client.get.call_args
        headers = call_args.kwargs["headers"]
        params = call_args.kwargs["params"]
        assert headers["Authorization"] == "Bearer sk_secret"
        assert params["cursor"] == "cur-1"
        assert params["types"] == "property.*"
        assert params["limit"] == 50

    @pytest.mark.asyncio
    async def test_raises_on_401(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(401))
        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.get_feed(auth_token="bad")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_feed(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "events": [],
                    "cursor": None,
                    "has_more": False,
                },
            )
        )
        rc = RegistryClient(client=mock_client)
        result = await rc.get_feed(auth_token="sk")
        assert result.events == []
        assert result.cursor is None
        assert result.has_more is False


# ========================================================================
# Public API Exports
# ========================================================================


class TestNewRegistryExports:
    def test_registry_types_importable(self):
        from adcp import (  # noqa: F401
            AgentCapabilities,
            AgentCompliance,
            AgentHealth,
            AgentStats,
            BrandActivity,
            BrandRegistryItem,
            DomainLookupResult,
            FederatedAgentWithDetails,
            FederatedPublisher,
            FeedEvent,
            FeedPage,
            PropertyActivity,
            PropertyIdentifier,
            PropertyRegistryItem,
            PropertySummary,
            ValidationResult,
        )

    def test_registry_sync_importable(self):
        from adcp import (  # noqa: F401
            ChangeHandler,
            CursorStore,
            FileCursorStore,
            RegistrySync,
        )
