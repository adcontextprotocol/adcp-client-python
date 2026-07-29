"""Tests for the upstream HTTP client and translation map helpers.

Mirrors the JS test/upstream-helpers.test.js coverage with two
adaptations for the Python shape:

* Methods return parsed JSON directly (``dict`` or ``None``), not a
  ``{status, body}`` envelope.
* Non-2xx responses raise :class:`AdcpError` with spec-conformant
  codes (AUTH_REQUIRED / PERMISSION_DENIED / MEDIA_BUY_NOT_FOUND /
  RATE_LIMITED / SERVICE_UNAVAILABLE / INVALID_REQUEST).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from adcp.decisioning import (
    ApiKey,
    DynamicBearer,
    NoAuth,
    StaticBearer,
    TranslationMap,
    UpstreamHttpClient,
    create_translation_map,
    create_upstream_http_client,
)
from adcp.decisioning.types import AdcpError

BASE = "https://upstream.example.com"


# ---------------------------------------------------------------------------
# create_translation_map
# ---------------------------------------------------------------------------


class TestTranslationMap:
    def setup_method(self) -> None:
        self.channel_map: TranslationMap[str, str] = create_translation_map(
            {
                "olv": "video",
                "ctv": "ctv",
                "display": "display",
                "streaming_audio": "audio",
            }
        )

    def test_to_upstream_returns_b_side(self) -> None:
        assert self.channel_map.to_upstream("olv") == "video"
        assert self.channel_map.to_upstream("ctv") == "ctv"

    def test_to_adcp_returns_a_side(self) -> None:
        assert self.channel_map.to_adcp("video") == "olv"
        assert self.channel_map.to_adcp("audio") == "streaming_audio"

    def test_to_upstream_raises_on_unknown(self) -> None:
        with pytest.raises(KeyError) as exc_info:
            self.channel_map.to_upstream("unknown")
        message = str(exc_info.value)
        assert "'unknown'" in message
        assert "'olv'" in message
        assert "'ctv'" in message
        assert "'display'" in message
        assert "'streaming_audio'" in message

    def test_to_adcp_raises_on_unknown(self) -> None:
        with pytest.raises(KeyError) as exc_info:
            self.channel_map.to_adcp("unknown_upstream")
        message = str(exc_info.value)
        assert "'unknown_upstream'" in message
        assert "'video'" in message
        assert "'audio'" in message

    def test_has_adcp(self) -> None:
        assert self.channel_map.has_adcp("olv") is True
        assert self.channel_map.has_adcp("video") is False
        assert self.channel_map.has_adcp("missing") is False

    def test_has_upstream(self) -> None:
        assert self.channel_map.has_upstream("video") is True
        assert self.channel_map.has_upstream("olv") is False
        assert self.channel_map.has_upstream("missing") is False

    def test_collision_detected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="translation collision"):
            create_translation_map({"a": "X", "b": "X"})

    def test_default_upstream_fallback(self) -> None:
        m: TranslationMap[str, str] = create_translation_map(
            {"olv": "video"}, default_upstream="STANDARD"
        )
        assert m.to_upstream("olv") == "video"
        assert m.to_upstream("unknown") == "STANDARD"

    def test_default_adcp_fallback(self) -> None:
        m: TranslationMap[str, str] = create_translation_map(
            {"olv": "video"}, default_adcp="display"
        )
        assert m.to_adcp("video") == "olv"
        assert m.to_adcp("unknown_upstream") == "display"


# ---------------------------------------------------------------------------
# create_upstream_http_client — happy paths and auth
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_returns_parsed_json_with_static_bearer() -> None:
    route = respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={"ok": True}))
    client = create_upstream_http_client(BASE, auth=StaticBearer(token="tok_123"))
    result = await client.get("/items")
    assert result == {"ok": True}
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok_123"
    await client.aclose()


@respx.mock
async def test_get_dynamic_bearer_called_per_request() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json=[]))
    calls: list[object] = []

    async def get_token(ctx: object) -> str:
        calls.append(ctx)
        return "fresh_token"

    client = create_upstream_http_client(BASE, auth=DynamicBearer(get_token=get_token))
    await client.get("/items")
    assert calls == [None]
    request = respx.calls.last.request
    assert request.headers["Authorization"] == "Bearer fresh_token"
    await client.aclose()


@respx.mock
async def test_get_api_key_header_injected() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    client = create_upstream_http_client(
        BASE, auth=ApiKey(header_name="X-Api-Key", key="secret_key")
    )
    await client.get("/items")
    assert respx.calls.last.request.headers["X-Api-Key"] == "secret_key"
    await client.aclose()


@respx.mock
async def test_get_no_auth_sends_no_authorization_header() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    client = create_upstream_http_client(BASE, auth=NoAuth())
    await client.get("/items")
    assert "Authorization" not in respx.calls.last.request.headers
    await client.aclose()


@respx.mock
async def test_default_auth_is_no_auth() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    client = create_upstream_http_client(BASE)  # no auth=
    await client.get("/items")
    assert "Authorization" not in respx.calls.last.request.headers
    await client.aclose()


@respx.mock
async def test_query_params_serialized_and_none_dropped() -> None:
    route = respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json=[]))
    client = create_upstream_http_client(BASE)
    await client.get("/items", params={"limit": 10, "q": "hello world", "skip_me": None})
    assert route.called
    url = str(respx.calls.last.request.url)
    assert "limit=10" in url
    # httpx URL-encodes the space as +
    assert "q=hello+world" in url or "q=hello%20world" in url
    assert "skip_me" not in url
    await client.aclose()


@respx.mock
async def test_post_sends_json_body_and_default_headers() -> None:
    route = respx.post(f"{BASE}/items").mock(return_value=httpx.Response(201, json={"id": "x"}))
    client = create_upstream_http_client(
        BASE,
        auth=NoAuth(),
        default_headers={"X-Tenant": "tenant_1"},
    )
    result = await client.post("/items", json={"name": "test"})
    assert result == {"id": "x"}
    assert route.called
    request = respx.calls.last.request
    assert request.method == "POST"
    assert request.headers["X-Tenant"] == "tenant_1"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {"name": "test"}
    await client.aclose()


@respx.mock
async def test_put_sends_json_body() -> None:
    respx.put(f"{BASE}/items/x").mock(
        return_value=httpx.Response(200, json={"id": "x", "name": "updated"})
    )
    client = create_upstream_http_client(BASE)
    result = await client.put("/items/x", json={"name": "updated"})
    assert result == {"id": "x", "name": "updated"}
    request = respx.calls.last.request
    assert request.method == "PUT"
    assert json.loads(request.content) == {"name": "updated"}
    await client.aclose()


@respx.mock
async def test_delete_sends_correct_method() -> None:
    respx.delete(f"{BASE}/items/1").mock(return_value=httpx.Response(204))
    client = create_upstream_http_client(BASE)
    result = await client.delete("/items/1")
    assert result == {}  # 204 → empty dict
    assert respx.calls.last.request.method == "DELETE"
    await client.aclose()


@respx.mock
async def test_per_call_headers_override_defaults() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    client = create_upstream_http_client(
        BASE, default_headers={"X-Tenant": "default", "X-Other": "stays"}
    )
    await client.get("/items", headers={"X-Tenant": "override"})
    headers = respx.calls.last.request.headers
    assert headers["X-Tenant"] == "override"
    assert headers["X-Other"] == "stays"
    await client.aclose()


@respx.mock
async def test_default_headers_merge_with_auth() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    client = create_upstream_http_client(
        BASE,
        auth=StaticBearer(token="abc"),
        default_headers={"X-Tenant": "t1"},
    )
    await client.get("/items")
    headers = respx.calls.last.request.headers
    assert headers["Authorization"] == "Bearer abc"
    assert headers["X-Tenant"] == "t1"
    await client.aclose()


# ---------------------------------------------------------------------------
# 404 → None vs MEDIA_BUY_NOT_FOUND
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_404_returns_none_when_treat_404_as_none_default() -> None:
    respx.get(f"{BASE}/items/missing").mock(return_value=httpx.Response(404, text="not found"))
    client = create_upstream_http_client(BASE)
    result = await client.get("/items/missing")
    assert result is None
    await client.aclose()


@respx.mock
async def test_get_404_raises_media_buy_not_found_when_disabled() -> None:
    respx.get(f"{BASE}/items/missing").mock(return_value=httpx.Response(404, text="not found"))
    client = create_upstream_http_client(BASE, treat_404_as_none=False)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/items/missing")
    assert exc_info.value.code == "MEDIA_BUY_NOT_FOUND"
    assert exc_info.value.recovery == "correctable"
    await client.aclose()


@respx.mock
async def test_get_404_with_custom_not_found_code() -> None:
    respx.get(f"{BASE}/creatives/x").mock(return_value=httpx.Response(404))
    client = create_upstream_http_client(BASE, treat_404_as_none=False)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/creatives/x", not_found_code="CREATIVE_NOT_FOUND")
    assert exc_info.value.code == "CREATIVE_NOT_FOUND"
    assert exc_info.value.recovery == "correctable"
    await client.aclose()


@respx.mock
async def test_post_404_always_raises() -> None:
    # POST is not a lookup; treat_404_as_none doesn't apply.
    respx.post(f"{BASE}/items").mock(return_value=httpx.Response(404))
    client = create_upstream_http_client(BASE)  # treat_404_as_none=True by default
    with pytest.raises(AdcpError) as exc_info:
        await client.post("/items", json={})
    assert exc_info.value.code == "MEDIA_BUY_NOT_FOUND"
    assert exc_info.value.recovery == "correctable"
    await client.aclose()


# ---------------------------------------------------------------------------
# Error projection — status → AdcpError code
# ---------------------------------------------------------------------------


@respx.mock
async def test_401_raises_auth_required() -> None:
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(401, text="unauthorized"))
    client = create_upstream_http_client(BASE)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/x")
    assert exc_info.value.code == "AUTH_REQUIRED"
    assert exc_info.value.recovery == "correctable"
    await client.aclose()


@respx.mock
async def test_403_raises_permission_denied() -> None:
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(403, text="forbidden"))
    client = create_upstream_http_client(BASE)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/x")
    assert exc_info.value.code == "PERMISSION_DENIED"
    assert exc_info.value.recovery == "correctable"
    await client.aclose()


@respx.mock
async def test_429_raises_rate_limited_transient() -> None:
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(429, text="too many"))
    client = create_upstream_http_client(BASE)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/x")
    assert exc_info.value.code == "RATE_LIMITED"
    assert exc_info.value.recovery == "transient"
    await client.aclose()


@respx.mock
async def test_500_raises_service_unavailable() -> None:
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(500, text="oops"))
    client = create_upstream_http_client(BASE)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/x")
    assert exc_info.value.code == "SERVICE_UNAVAILABLE"
    assert exc_info.value.recovery == "transient"
    await client.aclose()


@respx.mock
async def test_error_response_body_is_not_exposed() -> None:
    secret = "upstream-secret-token"
    respx.get(f"{BASE}/x").mock(
        return_value=httpx.Response(500, text=f"database failed Authorization=Bearer {secret}")
    )
    client = create_upstream_http_client(BASE)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/x")

    assert secret not in str(exc_info.value)
    assert "database failed" not in str(exc_info.value)
    await client.aclose()


@respx.mock
async def test_400_raises_invalid_request_correctable() -> None:
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(400, text="bad"))
    client = create_upstream_http_client(BASE)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/x")
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.recovery == "correctable"
    await client.aclose()


# ---------------------------------------------------------------------------
# auth_context passthrough
# ---------------------------------------------------------------------------


@respx.mock
async def test_dynamic_bearer_receives_auth_context() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    captured: list[object] = []

    async def get_token(ctx: object) -> str:
        captured.append(ctx)
        return "tok_for_acme"

    client = create_upstream_http_client(BASE, auth=DynamicBearer(get_token=get_token))
    await client.get("/items", auth_context={"operator_id": "acme"})
    assert captured == [{"operator_id": "acme"}]
    assert respx.calls.last.request.headers["Authorization"] == "Bearer tok_for_acme"
    await client.aclose()


@respx.mock
async def test_dynamic_bearer_per_call_routing() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))
    keys = {"acme": "tok_acme", "globex": "tok_globex"}

    async def get_token(ctx: object) -> str:
        if isinstance(ctx, dict):
            return keys.get(ctx.get("operator_id", ""), "master")
        return "master"

    client = create_upstream_http_client(BASE, auth=DynamicBearer(get_token=get_token))
    await client.get("/items", auth_context={"operator_id": "acme"})
    await client.post("/items", json={"x": 1}, auth_context={"operator_id": "globex"})
    auths = [c.request.headers["Authorization"] for c in respx.calls]
    assert auths == ["Bearer tok_acme", "Bearer tok_globex"]
    await client.aclose()


@respx.mock
async def test_dynamic_bearer_principal_passthrough() -> None:
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json={}))

    async def get_token(ctx: object) -> str:
        if isinstance(ctx, dict):
            principal = ctx.get("principal")
            if isinstance(principal, str):
                return principal
        return "fallback"

    client = create_upstream_http_client(BASE, auth=DynamicBearer(get_token=get_token))
    await client.get("/items", auth_context={"principal": "caller_token_xyz"})
    assert respx.calls.last.request.headers["Authorization"] == "Bearer caller_token_xyz"
    await client.aclose()


# ---------------------------------------------------------------------------
# Pool reuse + context manager
# ---------------------------------------------------------------------------


@respx.mock
async def test_async_context_manager_closes_client() -> None:
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200, json={}))
    async with create_upstream_http_client(BASE) as client:
        assert isinstance(client, UpstreamHttpClient)
        await client.get("/x")
        assert client._client is not None  # type: ignore[union-attr]
    assert client._client is None  # type: ignore[union-attr]


@respx.mock
async def test_200_with_malformed_json_raises_service_unavailable() -> None:
    """A 2xx response with non-JSON body (e.g. CDN/proxy HTML page) must
    surface as SERVICE_UNAVAILABLE, not a raw JSONDecodeError."""
    respx.get(f"{BASE}/items").mock(
        return_value=httpx.Response(200, content=b"<html>bad gateway</html>")
    )
    client = create_upstream_http_client(BASE)
    with pytest.raises(AdcpError) as exc_info:
        await client.get("/items")
    assert exc_info.value.code == "SERVICE_UNAVAILABLE"
    assert exc_info.value.recovery == "transient"
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)
    await client.aclose()


@respx.mock
async def test_pool_reused_across_calls() -> None:
    respx.get(f"{BASE}/a").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/b").mock(return_value=httpx.Response(200, json={}))
    client = create_upstream_http_client(BASE)
    await client.get("/a")
    underlying = client._client  # type: ignore[union-attr]
    await client.get("/b")
    assert client._client is underlying  # type: ignore[union-attr]
    await client.aclose()
