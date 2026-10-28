from __future__ import annotations

import socket
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx2
import pytest
from asgi_lifespan import LifespanManager
from pydantic import TypeAdapter

from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
from adcp.decisioning.capabilities import Features, MediaBuy
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.task_registry import InMemoryTaskRegistry
from adcp.decisioning.types import AdcpError
from adcp.property_lists import MCPPropertyListFetcher, check_property_list_reference
from adcp.server import GovernanceHandler, ToolContext, create_mcp_server
from adcp.types import ListProductsRequest, ListProductsResponse
from adcp.validation import validate_response


@pytest.mark.asyncio
@pytest.mark.parametrize("model_response", [False, True])
async def test_list_products_dispatch_filters_compact_criteria(model_response):
    products = [
        {
            "product_id": name,
            "name": name,
            "publisher_properties": [
                {
                    "selection_type": "by_id",
                    "publisher_domain": "publisher.com",
                    "property_ids": [name],
                }
            ],
        }
        for name in ["allowed", "outside"]
    ]
    body = {
        "outcome": "listed",
        "products": products,
        "feed_version": "feed",
        "cache_scope": "public",
    }

    class Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            media_buy=MediaBuy(features=Features(property_list_filtering=True)),
        )
        accounts = SingletonAccounts(account_id="account")

        async def list_products(self, req, ctx):
            return (
                TypeAdapter(ListProductsResponse).validate_python(body) if model_response else body
            )

    fetcher = AsyncMock()
    fetcher.fetch.return_value = ["allowed"]
    handler = PlatformHandler(
        Seller(), executor=None, registry=InMemoryTaskRegistry(), property_list_fetcher=fetcher
    )
    req = ListProductsRequest.model_validate(
        {
            "criteria": {
                "offer_filters": {
                    "property_list": {
                        "agent_url": "https://buyer.example.com/mcp",
                        "list_id": "approved",
                    }
                }
            }
        }
    )
    # Custom fetchers retain the destination policy even without SDK HTTP.
    from unittest.mock import patch

    with patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    ):
        result = await handler.list_products(req, ToolContext())
    wire = result if isinstance(result, dict) else result.model_dump(mode="json", exclude_none=True)
    assert [item["product_id"] for item in wire["products"]] == ["allowed"]
    assert wire["ext"]["property_list_applied"] is True
    assert "property_list_applied" not in wire
    assert validate_response("list_products", wire, version="3.2.1").valid
    assert len(body["products"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("model_response", [False, True])
async def test_projected_list_products_requests_inventory_before_filtering(
    monkeypatch, model_response
):
    from adcp.identifiers import normalize_identifier
    from adcp.property_lists import ResolvedPropertyList

    allowed = ResolvedPropertyList((normalize_identifier("domain", "allowed.example.com"),))
    monkeypatch.setattr(MCPPropertyListFetcher, "resolve", AsyncMock(return_value=allowed))
    monkeypatch.setattr(
        "adcp.adagents.fetch_adagents",
        AsyncMock(
            return_value={
                "properties": [
                    {
                        "property_id": name,
                        "identifiers": [{"type": "domain", "value": f"{name}.example.com"}],
                    }
                    for name in ["allowed", "outside"]
                ]
            }
        ),
    )
    observed = []
    platform_responses = []

    class Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            media_buy=MediaBuy(features=Features(property_list_filtering=True)),
        )
        accounts = SingletonAccounts(account_id="account")

        async def list_products(self, req, ctx):
            observed.append(req)
            fields = set(req.fields.root) | {"product_id", "name"}
            products = [
                {
                    "product_id": name,
                    "name": name,
                    "publisher_properties": [
                        {
                            "selection_type": "by_id",
                            "publisher_domain": "publisher.example.com",
                            "property_ids": [name],
                        }
                    ],
                    "overlay_support": {"property_list": True},
                }
                for name in ["allowed", "outside"]
            ]
            body = {
                "outcome": "listed",
                "products": [
                    {key: value for key, value in p.items() if key in fields} for p in products
                ],
                "feed_version": "feed",
                "cache_scope": "public",
                "next_cursor": "next",
            }
            result = (
                TypeAdapter(ListProductsResponse).validate_python(body) if model_response else body
            )
            platform_responses.append(result)
            return result

    handler = PlatformHandler(Seller(), executor=None, registry=InMemoryTaskRegistry())
    req = ListProductsRequest.model_validate(
        {
            "fields": ["name"],
            "criteria": {
                "offer_filters": {
                    "property_list": {
                        "agent_url": "https://buyer.example.com/mcp",
                        "list_id": "approved",
                    }
                }
            },
        }
    )
    result = await handler.list_products(req, ToolContext())
    wire = result if isinstance(result, dict) else result.model_dump(mode="json", exclude_none=True)
    assert set(observed[0].fields.root) == {"name", "publisher_properties", "overlay_support"}
    assert req.fields.root == ["name"]
    assert len(wire["products"]) == 1
    assert wire["products"][0]["product_id"] == "allowed"
    assert wire["products"][0]["name"] == "allowed"
    assert not {"publisher_properties", "overlay_support"} & wire["products"][0].keys()
    assert wire["next_cursor"] == "next"
    assert wire["ext"]["property_list_applied"] is True
    assert validate_response("list_products", wire, version="3.2.1").valid
    original = platform_responses[0]
    original = (
        original
        if isinstance(original, dict)
        else original.model_dump(mode="json", exclude_none=True)
    )
    assert len(original["products"]) == 2
    assert "publisher_properties" in original["products"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_response", [False, True])
@pytest.mark.parametrize("kind", ["proposed", "listed_purchase", "legacy_create"])
@pytest.mark.parametrize("allowed_ids", [["allowed", "outside"], ["allowed"], []])
async def test_request_proposals_filters_without_breaking_bound_references(
    monkeypatch, model_response, kind, allowed_ids
):
    from adcp.negotiation import compute_terms_digest
    from adcp.types import RequestProposalsRequest, RequestProposalsResponse

    products = [
        {
            "product_id": name,
            "name": name,
            "pricing_options": [
                {
                    "pricing_option_id": "cpm",
                    "pricing_model": "cpm",
                    "currency": "USD",
                    "fixed_price": 5,
                }
            ],
            "publisher_properties": [
                {
                    "selection_type": "by_id",
                    "publisher_domain": "publisher.example.com",
                    "property_ids": [name],
                }
            ],
        }
        for name in ["allowed", "outside"]
    ]
    body = {
        "outcome": "products_available",
        "products": products,
        "context": {"trace": "keep"},
        "incomplete": [{"scope": "forecast", "description": "Forecast pending"}],
    }
    if kind == "proposed":
        terms = {
            "brand": {"domain": "buyer.example.com"},
            "purchases": [
                {
                    "product_id": p["product_id"],
                    "pricing_option_id": "cpm",
                    "pricing": {
                        "pricing_option_id": "cpm",
                        "pricing_model": "cpm",
                        "currency": "USD",
                        "fixed_price": 5,
                    },
                    "impressions": 1000,
                    "start_time": "2099-01-01T00:00:00Z",
                    "end_time": "2099-02-01T00:00:00Z",
                }
                for p in products
            ],
            "start_time": "2099-01-01T00:00:00Z",
            "end_time": "2099-02-01T00:00:00Z",
        }
        body.update(
            outcome="proposed",
            proposals=[
                {
                    "proposal_id": "proposal-1",
                    "proposal_kind": "new_media_buy",
                    "proposal_status": "draft",
                    "expires_at": "2098-12-31T00:00:00Z",
                    "name": "Plan",
                    "commercial_terms": terms,
                    "terms_digest": compute_terms_digest(terms),
                }
            ],
        )
    else:
        continuation = {"kind": kind, "product_ids": ["allowed", "outside"]}
        if kind == "listed_purchase":
            continuation.update(cache_scope="account", feed_version="feed")
        else:
            continuation.update(
                continuation_token="opaque-bound-token-1234",
                continuation_expires_at="2099-01-01T00:00:00Z",
                source_adcp_version="3.1",
                losses=["feed_version_not_atomic", "pricing_version_not_atomic"],
                requires_explicit_acceptance=True,
            )
        body["purchase_continuation"] = continuation
    original_body = deepcopy(body)

    class Seller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            media_buy=MediaBuy(features=Features(property_list_filtering=True)),
        )
        accounts = SingletonAccounts(account_id="account")

        async def request_proposals(self, req, ctx):
            return (
                TypeAdapter(RequestProposalsResponse).validate_python(body)
                if model_response
                else body
            )

    fetcher = AsyncMock()
    fetcher.fetch.return_value = allowed_ids
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    handler = PlatformHandler(
        Seller(), executor=None, registry=InMemoryTaskRegistry(), property_list_fetcher=fetcher
    )
    # media-buy/request-proposals-request.json anyOf: a top-level brand, or an
    # account carrying brand and operator. An account_id reference names neither.
    request = RequestProposalsRequest.model_validate(
        {
            "idempotency_key": "proposal-request-1234",
            "account": {"account_id": "account"},
            "brand": {"domain": "buyer.example.com"},
            "brief": "News",
            "criteria": {
                "offer_filters": {
                    "property_list": {
                        "agent_url": "https://buyer.example.com/mcp",
                        "list_id": "approved",
                    }
                }
            },
        }
    )
    result = await handler.request_proposals(request, ToolContext())
    wire = result if isinstance(result, dict) else result.model_dump(mode="json", exclude_none=True)
    rejected = len(allowed_ids) < 2 and (kind != "listed_purchase" or not allowed_ids)
    if rejected:
        assert wire["outcome"] == "rejected"
        assert wire["reason"]
        assert not {"products", "proposals", "purchase_continuation", "incomplete"} & wire.keys()
    else:
        assert [p["product_id"] for p in wire["products"]] == allowed_ids
        if kind == "proposed":
            assert (
                wire["proposals"][0]["terms_digest"]
                == original_body["proposals"][0]["terms_digest"]
            )
        else:
            assert wire["purchase_continuation"]["product_ids"] == allowed_ids
    assert wire["ext"]["property_list_applied"] is True
    assert wire["context"] == {"trace": "keep"}
    assert validate_response("request_proposals", wire, version="3.2.1").valid
    assert body == original_body


@pytest.mark.asyncio
async def test_issued_reference_resolves_through_real_mcp_and_list_token_check(monkeypatch):
    calls = []
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    async def unused(self, *args, **kwargs):
        raise NotImplementedError

    async def get_list(self, request, context=None):
        calls.append((request.list_id, request.pagination.cursor))
        cursor = request.pagination.cursor
        return {
            "list": {"list_id": request.list_id, "name": "Approved"},
            "identifiers": (
                [{"type": "domain", "value": "example.com"}]
                if cursor is None
                else [{"type": "roku_store_id", "value": "123"}]
            ),
            "pagination": {
                "has_more": cursor is None,
                **({"cursor": "second"} if cursor is None else {}),
            },
            "cache_valid_until": expiry,
        }

    methods = {name: unused for name in GovernanceHandler.__abstractmethods__}
    methods["handle_get_property_list"] = get_list
    issuer_type = type("Issuer", (GovernanceHandler,), methods)

    def verify(list_id, context):
        return list_id == "approved" and context.metadata.get("list_token") == "Bearer list-secret"

    handler = issuer_type(property_list_token_verifier=verify)

    def context_factory(meta):
        # This dedicated shared-list endpoint accepts no other task with a list token.
        if meta.tool_name != "get_property_list":
            raise ValueError("List credentials only permit get_property_list")
        return ToolContext(
            metadata={"list_token": meta.request_context.headers.get("authorization")}
        )

    mcp = create_mcp_server(
        handler,
        stateless_http=True,
        context_factory=context_factory,
        allowed_hosts=["buyer.example.com"],
    )
    mcp.settings.json_response = True
    app = mcp.streamable_http_app(streamable_http_path="/mcp")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(
        "adcp.signing.build_async_ip_pinned_transport2",
        lambda *args, **kwargs: httpx2.ASGITransport(app=app),
    )
    ref = {
        "agent_url": "https://buyer.example.com/mcp",
        "list_id": "approved",
        "auth_token": "list-secret",
    }
    async with LifespanManager(app):
        resolver = MCPPropertyListFetcher()
        result = await check_property_list_reference(ref, fetcher=resolver)
        assert result.pages == 2
        assert [(item.type, item.value) for item in result.identifiers] == [
            ("domain", "example.com"),
            ("roku_store_id", "123"),
        ]
        assert calls == [("approved", None), ("approved", "second")]
        assert await resolver.resolve(ref) is result
        with pytest.raises(AdcpError):
            await resolver.resolve({**ref, "list_id": "other"})
        with pytest.raises(AdcpError):
            await resolver.resolve({**ref, "auth_token": "wrong"})
        assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["allow", "async", "deny", "raise", "truthy"])
async def test_governance_verifier_fails_closed_without_exposing_errors(behavior):
    from adcp.exceptions import ADCPTaskError

    calls = []

    async def unused(self, *args, **kwargs):
        raise NotImplementedError

    async def handle_get(self, req, context=None):
        calls.append(req.list_id)
        return {"list": {"list_id": req.list_id, "name": "List"}, "identifiers": []}

    async def permit():
        return True

    def verify(list_id, context):
        assert list_id == "approved"
        if behavior == "raise":
            raise ValueError("secret credential")
        if behavior == "async":
            return permit()
        return {"allow": True, "deny": False, "truthy": "yes"}.get(behavior)

    methods = {name: unused for name in GovernanceHandler.__abstractmethods__}
    methods["handle_get_property_list"] = handle_get
    issuer_type = type("VerifierIssuer", (GovernanceHandler,), methods)
    issuer = issuer_type(property_list_token_verifier=verify)
    if behavior in {"allow", "async"}:
        await issuer.get_property_list({"list_id": "approved"})
        assert calls == ["approved"]
    else:
        with pytest.raises(ADCPTaskError) as error:
            await issuer.get_property_list({"list_id": "approved"})
        assert "secret" not in str(error.value)
        assert error.value.errors[0].code == "UNAUTHORIZED"
        assert not calls
