from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adcp.decisioning.types import AdcpError
from adcp.identifiers import normalize_identifier
from adcp.property_lists import (
    MCPPropertyListFetcher,
    ProductPropertyPolicy,
    ResolvedPropertyList,
    check_property_list_reference,
    request_property_list,
)
from adcp.types.core import TaskResult, TaskStatus

REF = {"agent_url": "https://buyer.example.com/mcp", "list_id": "list-1", "auth_token": "secret"}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host == "buyer.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def page(identifiers, *, cursor=None, expiry=None):
    return TaskResult(
        status=TaskStatus.COMPLETED,
        success=True,
        data={
            "list": {"list_id": "list-1", "name": "List"},
            "identifiers": identifiers,
            "pagination": {"has_more": cursor is not None, "cursor": cursor},
            "cache_valid_until": expiry,
        },
    )


def install_client(monkeypatch, fetcher, pages):
    client = SimpleNamespace(get_property_list=AsyncMock(side_effect=pages))

    class Context:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            pass

    def create(*args):
        return Context()

    monkeypatch.setattr(fetcher, "_create_client", create)
    return client


@pytest.mark.asyncio
async def test_pages_types_expiry_and_auth_isolated_cache(monkeypatch):
    fetcher = MCPPropertyListFetcher()
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    client = install_client(
        monkeypatch,
        fetcher,
        [
            page([{"type": "domain", "value": "EXAMPLE.com"}], cursor="next", expiry=expiry),
            page([{"type": "roku_store_id", "value": "123"}], expiry=expiry),
            page([], expiry=expiry),
        ],
    )
    resolved = await fetcher.resolve(REF)
    assert [(item.type, item.value) for item in resolved.identifiers] == [
        ("domain", "example.com"),
        ("roku_store_id", "123"),
    ]
    assert resolved.pages == 2
    assert client.get_property_list.call_args_list[1].args[0].pagination.cursor == "next"
    assert await fetcher.resolve(REF) is resolved
    assert client.get_property_list.await_count == 2
    assert not (await fetcher.resolve({**REF, "auth_token": "different"})).identifiers
    assert client.get_property_list.await_count == 3
    assert "secret" not in repr(fetcher._cache)


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [None, datetime(2000, 1, 1, tzinfo=timezone.utc)])
async def test_missing_or_expired_timestamp_does_not_cache(monkeypatch, expiry):
    fetcher = MCPPropertyListFetcher()
    client = install_client(monkeypatch, fetcher, [page([], expiry=expiry)] * 2)
    await fetcher.resolve(REF)
    await fetcher.resolve(REF)
    assert client.get_property_list.await_count == 2


@pytest.mark.asyncio
async def test_earliest_page_expiry_and_compliance_probe_bypasses_cache(monkeypatch):
    now = datetime.now(timezone.utc)
    fetcher = MCPPropertyListFetcher()
    install_client(
        monkeypatch,
        fetcher,
        [
            page([], cursor="n", expiry=now + timedelta(hours=2)),
            page([], expiry=now + timedelta(hours=1)),
            page([]),
        ],
    )
    result = await fetcher.resolve(REF)
    assert result.cache_valid_until == now + timedelta(hours=1)
    assert (await check_property_list_reference(REF, fetcher=fetcher)).pages == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages",
    [
        [page([], cursor="repeat"), page([], cursor="repeat")],
        [
            TaskResult(
                status=TaskStatus.COMPLETED,
                success=True,
                data={"list": {"list_id": "wrong"}, "identifiers": []},
            )
        ],
        [TaskResult(status=TaskStatus.FAILED, success=False, error="secret")],
    ],
)
async def test_bad_pages_fail_without_credential_leak(monkeypatch, pages, caplog):
    fetcher = MCPPropertyListFetcher()
    install_client(monkeypatch, fetcher, pages)
    with pytest.raises(AdcpError) as error:
        await fetcher.resolve(REF)
    assert error.value.recovery == "transient"
    assert "secret" not in str(error.value) + caplog.text
    assert not fetcher._cache


@pytest.mark.asyncio
async def test_destination_rejected_before_client_creation(monkeypatch):
    fetcher = MCPPropertyListFetcher()
    create = AsyncMock()
    monkeypatch.setattr(fetcher, "_create_client", create)
    with pytest.raises(AdcpError) as error:
        await fetcher.resolve({**REF, "agent_url": "http://127.0.0.1:8080/mcp"})
    assert error.value.recovery == "correctable"
    create.assert_not_called()


def product(pid, prop_id="web"):
    return {
        "product_id": pid,
        "publisher_properties": [
            {
                "publisher_domain": "publisher.com",
                "selection_type": "by_id",
                "property_ids": [prop_id],
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["filter", "flag"])
async def test_buyer_checks_web_apps_ctv_and_retains_unjudgeable(monkeypatch, mode):
    allowed = ResolvedPropertyList(
        tuple(
            normalize_identifier(kind, value)
            for kind, value in [
                ("domain", "example.com"),
                ("android_package", "com.example.app"),
                ("roku_store_id", "123"),
            ]
        )
    )
    fetcher = MCPPropertyListFetcher()
    monkeypatch.setattr(fetcher, "resolve", AsyncMock(return_value=allowed))
    expansion = AsyncMock(
        side_effect=[
            {
                "identifiers": [
                    {
                        "type": "domain",
                        "value": "www.example.com",
                        "publisher_domain": "publisher.com",
                        "property_id": "web",
                    }
                ]
            },
            {
                "identifiers": [
                    {
                        "type": "android_package",
                        "value": "com.example.app",
                        "publisher_domain": "publisher.com",
                        "property_id": "app",
                    }
                ]
            },
            {
                "identifiers": [
                    {
                        "type": "roku_store_id",
                        "value": "123",
                        "publisher_domain": "publisher.com",
                        "property_id": "ctv",
                    }
                ]
            },
            {
                "identifiers": [
                    {
                        "type": "domain",
                        "value": "outside.com",
                        "publisher_domain": "publisher.com",
                        "property_id": "bad",
                    }
                ]
            },
            {"identifiers": []},
        ]
    )
    policy = ProductPropertyPolicy(mode=mode, fetcher=fetcher, expand_identifiers=expansion)
    products = [product(kind, kind) for kind in ["web", "app", "ctv", "bad", "unknown"]]
    result = TaskResult(
        status=TaskStatus.COMPLETED,
        success=True,
        data={"products": products, "next_cursor": "next"},
        metadata={"existing": True},
    )
    checked = await policy.apply(
        {"criteria": {"offer_filters": {"property_list": REF}}},
        result,
        seller_url="https://seller.com/mcp",
    )
    assert [item["status"] for item in checked.metadata["product_property_policy"]] == [
        "match",
        "match",
        "match",
        "mismatch",
        "unjudgeable",
    ]
    assert len(checked.data["products"]) == (4 if mode == "filter" else 5)
    assert checked.data["next_cursor"] == "next"
    assert checked.metadata["existing"] is True
    assert len(result.data["products"]) == 5


def test_request_location_precedence_and_legacy():
    assert request_property_list({"property_list": REF}) is REF
    newer = {**REF, "list_id": "new"}
    assert (
        request_property_list(
            {"property_list": REF, "criteria": {"offer_filters": {"property_list": newer}}}
        )
        is newer
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["get_products", "list_products"])
@pytest.mark.parametrize("generic", [False, True])
async def test_client_opt_in_policy_runs_once_for_explicit_and_generic_calls(
    monkeypatch, method_name, generic
):
    from adcp import ADCPClient
    from adcp.types import AgentConfig, GetProductsRequest, ListProductsRequest, Protocol

    policy = ProductPropertyPolicy()
    raw = TaskResult(status=TaskStatus.COMPLETED, success=True, data={"products": []})
    checked = raw.model_copy(update={"metadata": {"checked": True}})
    apply = AsyncMock(return_value=checked)
    monkeypatch.setattr(policy, "apply", apply)
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.com/mcp", protocol=Protocol.MCP),
        product_property_policy=policy,
        account_policy="off",
    )
    if method_name == "list_products":
        request = ListProductsRequest.model_validate(
            {"criteria": {"offer_filters": {"property_list": REF}}}
        )
        monkeypatch.setattr(client, "_execute_typed_task", AsyncMock(return_value=raw))
    else:
        request = GetProductsRequest.model_validate(
            {"buying_mode": "brief", "brief": "news", "property_list": REF}
        )
        monkeypatch.setattr(client.adapter, "get_products", AsyncMock(return_value=raw))
        monkeypatch.setattr(client, "_canonicalize_get_products_result", lambda result: result)
    try:
        result = (
            await client.execute_task(method_name, request)
            if generic
            else await getattr(client, method_name)(request)
        )
        assert result.metadata["checked"] is True
        apply.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_buyer_does_not_change_submitted_or_unchanged_envelopes():
    policy = ProductPropertyPolicy()
    for body in [
        {"status": "submitted", "task_id": "pending"},
        {"outcome": "unchanged", "feed_version": "v1"},
    ]:
        result = TaskResult(status=TaskStatus.COMPLETED, success=True, data=body)
        assert (
            await policy.apply({"property_list": REF}, result, seller_url="https://seller.com")
            is result
        )


@pytest.mark.asyncio
async def test_seller_default_matches_typed_catalog_inventory(monkeypatch):
    from adcp.decisioning.property_list import maybe_apply_property_list_filter

    allowed = ResolvedPropertyList((normalize_identifier("roku_store_id", "123"),))
    monkeypatch.setattr(MCPPropertyListFetcher, "resolve", AsyncMock(return_value=allowed))
    monkeypatch.setattr(
        "adcp.adagents.fetch_adagents",
        AsyncMock(
            return_value={
                "properties": [
                    {
                        "property_id": "ctv",
                        "identifiers": [{"type": "roku_store_id", "value": "123"}],
                    },
                    {
                        "property_id": "web",
                        "identifiers": [{"type": "domain", "value": "outside.com"}],
                    },
                ]
            }
        ),
    )
    ctv, web = product("ctv", "ctv"), product("web", "web")
    all_properties = {
        "product_id": "all",
        "publisher_properties": [{"publisher_domain": "publisher.com", "selection_type": "all"}],
    }
    response = {"products": [ctv, web, all_properties]}
    filtered = await maybe_apply_property_list_filter(
        params={"property_list": REF}, response=response, fetcher=None, capability_enabled=True
    )
    assert filtered["products"] == [ctv]
    assert filtered["property_list_applied"] is True
    assert len(response["products"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("permissive,expected", [(False, "mismatch"), (True, "match")])
async def test_property_identifier_alternatives_and_strict_vs_targetable(permissive, expected):
    allowed = ResolvedPropertyList((normalize_identifier("apple_app_store_id", "123"),))
    expansion = AsyncMock(
        return_value={
            "identifiers": [
                {
                    "type": "ios_bundle",
                    "value": "com.example.app",
                    "property_id": "a",
                    "publisher_domain": "publisher.com",
                },
                {
                    "type": "apple_app_store_id",
                    "value": "123",
                    "property_id": "a",
                    "publisher_domain": "publisher.com",
                },
                {
                    "type": "domain",
                    "value": "outside.com",
                    "property_id": "b",
                    "publisher_domain": "publisher.com",
                },
            ]
        }
    )
    policy = ProductPropertyPolicy(expand_identifiers=expansion)
    p = {
        "product_id": "p",
        "property_targeting_allowed": permissive,
        "publisher_properties": [
            {
                "selection_type": "by_id",
                "publisher_domain": "publisher.com",
                "property_ids": ["a", "b"],
            }
        ],
    }
    assert (await policy.assess([p], allowed, seller_url="https://seller.com"))[
        0
    ].status == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("pins", ["3.2", {"seller": "3.2"}])
async def test_multi_client_forwards_opt_in_policy(pins):
    from adcp import ADCPMultiAgentClient
    from adcp.types import AgentConfig, Protocol

    policy = ProductPropertyPolicy()
    async with ADCPMultiAgentClient(
        [AgentConfig(id="seller", agent_uri="https://seller.com/mcp", protocol=Protocol.MCP)],
        product_property_policy=policy,
        adcp_version=pins,
    ) as client:
        assert client.agents["seller"].product_property_policy is policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,expected",
    [
        ("example.com", "mismatch"),
        ("edition.example.com", "match"),
        ("nested.edition.example.com", "match"),
        ("otherexample.com", "mismatch"),
    ],
)
async def test_domain_wildcards_do_not_match_apex_or_hostname_suffixes(value, expected):
    allowed = ResolvedPropertyList((normalize_identifier("domain", "*.example.com"),))
    expansion = AsyncMock(
        return_value={
            "identifiers": [
                {
                    "type": "domain",
                    "value": value,
                    "property_id": "web",
                    "publisher_domain": "publisher.com",
                }
            ]
        }
    )
    policy = ProductPropertyPolicy(expand_identifiers=expansion)
    result = await policy.assess([product("p")], allowed, seller_url="https://seller.com")
    assert result[0].status == expected


@pytest.mark.asyncio
async def test_legacy_resolver_helper_defaults_to_typed_mcp_fetcher(monkeypatch):
    from adcp.decisioning.property_list import resolve_property_list

    resolve = AsyncMock(
        return_value=ResolvedPropertyList((normalize_identifier("roku_store_id", "123"),))
    )
    monkeypatch.setattr(MCPPropertyListFetcher, "resolve", resolve)
    ref = {**REF, "list_id": "opaque/list/id"}
    assert await resolve_property_list(ref) == {"123"}
    resolve.assert_awaited_once_with(ref)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_page_count", [2, 3])
async def test_resolution_identifier_limit_fails_without_caching(monkeypatch, first_page_count):
    identifiers = [
        {"type": "domain", "value": f"site{index}.com"} for index in range(first_page_count)
    ]
    resolver = MCPPropertyListFetcher(max_identifiers=2)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    install_client(
        monkeypatch,
        resolver,
        [
            page(identifiers, cursor="next", expiry=expiry),
            page([{"type": "domain", "value": "extra.com"}], expiry=expiry),
        ],
    )
    with pytest.raises(AdcpError):
        await resolver.resolve(REF)
    assert not resolver._cache


@pytest.mark.asyncio
async def test_resolution_bytes_limit_across_small_pages(monkeypatch):
    resolver = MCPPropertyListFetcher(max_resolution_bytes=300)
    install_client(
        monkeypatch,
        resolver,
        [
            page([{"type": "domain", "value": "first.com"}], cursor="next"),
            page([{"type": "domain", "value": "second.com"}]),
        ],
    )
    with pytest.raises(AdcpError):
        await resolver.resolve(REF)
    assert not resolver._cache


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["count", "bytes"])
async def test_large_resolutions_return_without_exceeding_cache_budget(monkeypatch, budget):
    resolver = MCPPropertyListFetcher(
        **({"max_cached_identifiers": 1} if budget == "count" else {"max_cache_bytes": 1})
    )
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    two = page(
        [{"type": "domain", "value": "first.com"}, {"type": "domain", "value": "second.com"}],
        expiry=expiry,
    )
    client = install_client(monkeypatch, resolver, [two, two])
    assert len((await resolver.resolve(REF)).identifiers) == 2
    assert len((await resolver.resolve(REF)).identifiers) == 2
    assert client.get_property_list.await_count == 2
    assert not resolver._cache
    assert resolver._cached_identifiers == resolver._cache_bytes == 0


@pytest.mark.asyncio
async def test_weighted_cache_eviction_and_clear(monkeypatch):
    resolver = MCPPropertyListFetcher(max_cached_identifiers=3)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    two = page(
        [{"type": "domain", "value": "first.com"}, {"type": "domain", "value": "second.com"}],
        expiry=expiry,
    )
    client = install_client(monkeypatch, resolver, [two, two, two])
    await resolver.resolve(REF)
    await resolver.resolve({**REF, "auth_token": "different"})
    assert resolver._cached_identifiers == 2
    assert len(resolver._cache) == 1
    await resolver.resolve(REF)
    assert client.get_property_list.await_count == 3
    resolver.clear_cache()
    assert resolver._cached_identifiers == resolver._cache_bytes == 0
    assert not resolver._cache_weights


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [False, True])
async def test_canonical_property_list_overlay_support_allows_inventory_subset(model):
    from adcp.types.domains.core.canonical_product import CanonicalProduct

    allowed = ResolvedPropertyList((normalize_identifier("domain", "allowed.com"),))
    item = {
        "product_id": "p",
        "name": "Product",
        "overlay_support": {"property_list": True},
        "publisher_properties": [
            {
                "publisher_domain": "publisher.com",
                "selection_type": "by_id",
                "property_ids": ["a", "b"],
            }
        ],
    }
    expansion = AsyncMock(
        return_value={
            "identifiers": [
                {
                    "type": "domain",
                    "value": "allowed.com",
                    "publisher_domain": "publisher.com",
                    "property_id": "a",
                },
                {
                    "type": "domain",
                    "value": "outside.com",
                    "publisher_domain": "publisher.com",
                    "property_id": "b",
                },
            ]
        }
    )
    policy = ProductPropertyPolicy(expand_identifiers=expansion)
    candidate = CanonicalProduct.model_validate(item) if model else item
    result = await policy.assess([candidate], allowed, seller_url="https://seller.com")
    assert result[0].status == "match"


@pytest.mark.asyncio
async def test_cache_accounts_for_large_reference_keys(monkeypatch):
    resolver = MCPPropertyListFetcher(max_cache_bytes=100)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    client = install_client(monkeypatch, resolver, [page([], expiry=expiry)] * 2)
    await resolver.resolve(REF)
    await resolver.resolve(REF)
    assert client.get_property_list.await_count == 2
    assert not resolver._cache


@pytest.mark.asyncio
async def test_large_opaque_cursors_still_detect_cycles(monkeypatch):
    resolver = MCPPropertyListFetcher()
    cursor = "opaque" * 100_000
    client = install_client(monkeypatch, resolver, [page([], cursor=cursor)] * 2)
    with pytest.raises(AdcpError):
        await resolver.resolve(REF)
    assert client.get_property_list.await_count == 2
    assert not resolver._cache


@pytest.mark.asyncio
@pytest.mark.parametrize("selectors", [None, []])
async def test_seller_excludes_products_without_selectors_in_mixed_catalog(monkeypatch, selectors):
    from adcp.decisioning.property_list import maybe_apply_property_list_filter

    allowed = ResolvedPropertyList((normalize_identifier("domain", "allowed.com"),))
    monkeypatch.setattr(MCPPropertyListFetcher, "resolve", AsyncMock(return_value=allowed))
    monkeypatch.setattr(
        "adcp.adagents.fetch_adagents",
        AsyncMock(
            return_value={
                "properties": [
                    {
                        "property_id": "web",
                        "identifiers": [{"type": "domain", "value": "allowed.com"}],
                    }
                ]
            }
        ),
    )
    declared = product("declared")
    undeclared = {"product_id": "undeclared", "publisher_properties": selectors}
    response = {"products": [undeclared, declared]}
    filtered = await maybe_apply_property_list_filter(
        params={"property_list": REF}, response=response, fetcher=None, capability_enabled=True
    )
    assert filtered["products"] == [declared]
    assert filtered["property_list_applied"] is True
    assert len(response["products"]) == 2


@pytest.mark.asyncio
async def test_seller_catalog_fetch_failure_remains_transient(monkeypatch):
    from adcp.decisioning.property_list import maybe_apply_property_list_filter

    monkeypatch.setattr(
        MCPPropertyListFetcher,
        "resolve",
        AsyncMock(
            return_value=ResolvedPropertyList((normalize_identifier("domain", "allowed.com"),))
        ),
    )
    monkeypatch.setattr("adcp.adagents.fetch_adagents", AsyncMock(side_effect=OSError("timeout")))
    with pytest.raises(AdcpError) as error:
        await maybe_apply_property_list_filter(
            params={"property_list": REF},
            response={"products": [product("p")]},
            fetcher=None,
            capability_enabled=True,
        )
    assert error.value.recovery == "transient"
