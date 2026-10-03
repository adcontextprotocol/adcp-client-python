"""AdCP 3.3 agent identity, publisher intersection, and governance regressions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from adcp.governance import resolve_governance_jwks
from adcp.protocols.a2a import A2AAdapter
from adcp.signing import agent_resolver
from adcp.signing.agent_resolver import AgentResolverError, async_resolve_agent
from adcp.signing.brand_jwks import BrandJsonResolverError, _select_agent
from adcp.signing.crypto import private_key_from_jwk
from adcp.signing.errors import SignatureVerificationError
from adcp.signing.publisher_pins import jwk_thumbprint, matches_publisher_pins
from adcp.signing.webhook_signer import sign_webhook
from adcp.signing.webhook_verifier import (
    WebhookVerifyOptions,
    verify_webhook_from_agent_url,
    verify_webhook_signature,
)

AGENT = "https://seller.example.com/mcp"
_real_send_and_aggregate = A2AAdapter._send_and_aggregate
RECORD = "https://example.com/operator/brand.json"
JWKS = "https://example.com/keys.json"
URL = "https://buyer.example.com/webhooks"
NOW = 1776520800
KEY = json.loads(
    (Path(__file__).parent / "conformance/vectors/request-signing/keys.json").read_text()
)["keys"][0]
KEY = {**KEY, "adcp_use": "request-signing"}


def entry(url: str = AGENT, **extra: Any) -> dict[str, Any]:
    return {"type": "sales", "url": url, "jwks_uri": JWKS, **extra}


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://SELLER.EXAMPLE.COM:443/mcp",
        "https://seller.example.com/a/../mcp",
        "https://seller.example.com/%6dcp",
    ],
)
def test_url_is_canonical_selector(url: str) -> None:
    selected = _select_agent(
        {"agents": [entry(url), entry("https://other.example.com/mcp")]},
        RECORD,
        agent_url=AGENT,
        agent_type="sales",
    )
    assert selected.url == AGENT


@pytest.mark.parametrize("url", ["https://other.example.com/mcp", AGENT + "/"])
def test_role_and_id_cannot_select_a_different_url(url: str) -> None:
    with pytest.raises(BrandJsonResolverError, match="does not list"):
        _select_agent(
            {"agents": [entry(url, id="seller")]},
            RECORD,
            agent_url=AGENT,
            agent_type="sales",
            agent_id="seller",
        )


def test_canonical_duplicates_are_ambiguous_even_with_id() -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent(
            {"agents": [entry(id="same"), entry("https://SELLER.example.com:443/mcp", id="same")]},
            RECORD,
            agent_url=AGENT,
            agent_id="same",
        )
    assert exc.value.code == "agent_ambiguous"


def test_shared_portfolio_attestations_count_once() -> None:
    record = {
        "house": {"agents": [entry()]},
        "brands": [{"id": "a", "agents": [entry()]}, {"id": "b", "agents": [entry()]}],
    }
    assert _select_agent(record, RECORD, agent_url=AGENT).url == AGENT


@pytest.mark.parametrize("extra", [{"type": "buying"}, {"jwks_uri": JWKS + "?other"}])
def test_conflicting_portfolio_attestations_are_ambiguous(extra: dict[str, str]) -> None:
    with pytest.raises(BrandJsonResolverError) as exc:
        _select_agent(
            {"house": {"agents": [entry()]}, "brands": [{"id": "a", "agents": [entry(**extra)]}]},
            RECORD,
            agent_url=AGENT,
        )
    assert exc.value.code == "agent_ambiguous"


def test_pin_thumbprint_ignores_kid_and_metadata() -> None:
    assert jwk_thumbprint(KEY) == jwk_thumbprint({**KEY, "kid": "publisher-alias", "use": "enc"})
    assert matches_publisher_pins(KEY, {"publisher": [{**KEY, "kid": "alias"}]}, now=NOW)


@pytest.mark.parametrize(
    "pin",
    [
        {"kid": KEY["kid"]},
        {**KEY, "x": "different-key"},
        {**KEY, "revoked_at": "2020-01-01T00:00:00Z"},
        {**KEY, "revoked_at": "invalid"},
    ],
)
def test_pin_rejects_incomplete_different_or_revoked_keys(pin: dict[str, Any]) -> None:
    assert not matches_publisher_pins(KEY, {"publisher": [pin]}, now=NOW)


def test_every_publisher_pin_applies() -> None:
    assert not matches_publisher_pins(KEY, {"a": [KEY], "b": []}, now=NOW)
    assert matches_publisher_pins(KEY, {"a": [KEY], "b": None}, now=NOW)


def signed_headers(body: bytes = b"{}") -> dict[str, str]:
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers={"content-type": "application/json"},
        body=body,
        private_key=private_key_from_jwk(KEY, d_field="_private_d_for_test_only"),
        key_id=KEY["kid"],
        alg="ed25519",
        created=NOW,
    )
    return {"content-type": "application/json", **signed.as_dict()}


def options(keys: list[dict[str, Any]], pins: Any, refresh: Any) -> WebhookVerifyOptions:
    return WebhookVerifyOptions(
        jwks_resolver=agent_resolver._BrandJsonStaticJwksResolver({"keys": keys}, jwks_uri=JWKS),
        publisher_pins=pins,
        refresh_publisher_pins=refresh,
        expected_key_origins={"webhook_signing": "https://example.com"},
        clock=lambda: NOW,
        replay_store=None,
    )


def test_pinned_key_absent_from_operator_jwks_fails_and_refreshes() -> None:
    refreshes = []
    pins = {"publisher": [KEY]}
    with pytest.raises(SignatureVerificationError) as exc:
        verify_webhook_signature(
            method="POST",
            url=URL,
            headers=signed_headers(),
            body=b"{}",
            options=options([], pins, lambda: refreshes.append(True) or pins),
        )
    assert exc.value.code == "webhook_signature_key_unknown"
    assert refreshes == [True]


def test_pin_rotation_recovers_after_force_refresh() -> None:
    refreshed = {"publisher": [KEY]}
    result = verify_webhook_signature(
        method="POST",
        url=URL,
        headers=signed_headers(),
        body=b"{}",
        options=options([KEY], {"publisher": [{"kid": KEY["kid"]}]}, lambda: refreshed),
    )
    assert result.key_id == KEY["kid"]


def test_pinned_key_still_requires_matching_key_origin() -> None:
    pins = {"publisher": [KEY]}
    opts = replace(
        options([KEY], pins, lambda: pins),
        expected_key_origins={"webhook_signing": "https://attacker.com"},
    )
    with pytest.raises(SignatureVerificationError) as exc:
        verify_webhook_signature(
            method="POST", url=URL, headers=signed_headers(), body=b"{}", options=opts
        )
    assert exc.value.code == "webhook_signature_key_unknown"


@pytest.mark.parametrize(
    "pin", [{"kid": KEY["kid"]}, {**KEY, "revoked_at": "2020-01-01T00:00:00Z"}]
)
def test_webhook_rejects_invalid_pin_after_refresh(pin: dict[str, Any]) -> None:
    refreshes = []
    pins = {"publisher": [pin]}
    with pytest.raises(SignatureVerificationError) as exc:
        verify_webhook_signature(
            method="POST",
            url=URL,
            headers=signed_headers(),
            body=b"{}",
            options=options([KEY], pins, lambda: refreshes.append(True) or pins),
        )
    assert exc.value.code == "webhook_signature_key_unknown"
    assert refreshes == [True]


def test_governance_canonical_duplicates_fail() -> None:
    record = {
        "agents": [
            {"type": "governance", "url": "https://gov.example.com/tenant"},
            {"type": "governance", "url": "https://GOV.example.com:443/tenant"},
        ]
    }
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_governance_jwks(
            record, issuer="https://gov.example.com/tenant", brand_domain="brand.com"
        )


async def resolve_record(
    monkeypatch: pytest.MonkeyPatch,
    record: Any,
    *,
    record_url: str = RECORD,
    legacy: bool = False,
    key_origins: Any = None,
    responses: Any = None,
) -> Any:
    caps = {"identity": {}} if legacy else {"identity": {"brand_json_url": record_url}}
    if key_origins is not None:
        caps["identity"]["key_origins"] = key_origins
    bodies = {AGENT: caps, record_url: record, **(responses or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        value = bodies.get(str(request.url))
        if request.method == "POST" and value is not None:
            payload = json.loads(request.content)
            if payload["method"] == "initialize":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": payload["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
            if payload["method"] == "notifications/initialized":
                return httpx.Response(202)
            value = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [], "structuredContent": value},
            }
        return httpx.Response(404) if value is None else httpx.Response(200, json=value)

    def factory(_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fetch_keys(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"keys": [KEY]}

    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", fetch_keys)
    return await async_resolve_agent(
        AGENT,
        _capabilities_client_factory=factory,
        _brand_jwks_client_factory=factory,
        allow_legacy_fallback=legacy,
    )


@pytest.mark.asyncio
async def test_exact_operator_record_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    record = {"agents": [entry(id="declared-id")]}
    result = await resolve_record(monkeypatch, record)
    assert result.brand_json == record
    assert result.agent_entry["id"] == "declared-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("house", [True, False])
async def test_only_house_portfolio_delegation_binds_origin(
    monkeypatch: pytest.MonkeyPatch, house: bool
) -> None:
    record = {
        "agents": [entry()],
        "authorized_operators": [
            {"domain": "example.com", "brands": ["unrelated"], "countries": ["AQ"]}
        ],
    }
    if house:
        record["house"] = {"agents": record.pop("agents")}
        assert (
            await resolve_record(monkeypatch, record, record_url="https://operator.org/brand.json")
        ).agent_url == AGENT
    else:
        with pytest.raises(AgentResolverError) as exc:
            await resolve_record(monkeypatch, record, record_url="https://operator.org/brand.json")
        assert exc.value.signature_code == "request_signature_brand_origin_mismatch"


@pytest.mark.asyncio
async def test_all_declared_origins_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentResolverError) as exc:
        await resolve_record(
            monkeypatch,
            {"agents": [entry()]},
            key_origins={
                "webhook_signing": "https://example.com",
                "request_signing": "https://other.org",
            },
        )
    assert exc.value.signature_code == "request_signature_key_origin_mismatch"


@pytest.mark.asyncio
async def test_legacy_fallback_at_agent_host(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await resolve_record(
        monkeypatch,
        {"agents": [entry()]},
        legacy=True,
        record_url="https://seller.example.com/.well-known/brand.json",
    )
    assert result.legacy_discovery


@pytest.mark.asyncio
async def test_legacy_fallback_at_registrable_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await resolve_record(
        monkeypatch,
        {"agents": [entry()]},
        legacy=True,
        record_url="https://example.com/.well-known/brand.json",
    )
    assert result.brand_json_url == "https://example.com/.well-known/brand.json"


def test_governance_selects_only_governed_brand() -> None:
    governance = {"type": "governance", "url": "https://gov.vendor.com/acme"}
    record = {
        "house": {"domain": "house.com", "agents": [governance]},
        "brands": [
            {"url": "https://a.com", "agents": []},
            {"url": "https://b.com", "agents": [governance]},
        ],
    }
    with pytest.raises(ValueError, match="not authorized"):
        resolve_governance_jwks(record, issuer=governance["url"], brand_domain="a.com")
    resolver = resolve_governance_jwks(
        record, issuer="https://GOV.vendor.com:443/acme", brand_domain="b.com"
    )
    assert resolver is not None


@pytest.mark.asyncio
async def test_webhook_discovery_failure_uses_key_unknown_and_logs_cause(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    async def failed(*args: Any, **kwargs: Any) -> Any:
        raise AgentResolverError(
            "brand_json_resolution_failed",
            "ambiguous",
            signature_code="request_signature_brand_json_ambiguous",
        )

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", failed)
    with pytest.raises(SignatureVerificationError) as exc:
        await verify_webhook_from_agent_url(
            method="POST", url=URL, headers=signed_headers(), body=b"{}", agent_url=AGENT
        )
    assert exc.value.code == "webhook_signature_key_unknown"
    assert "request_signature_brand_json_ambiguous" in caplog.text


@pytest.mark.asyncio
async def test_async_webhook_pin_rotation_uses_only_receiver_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adcp import adagents
    from adcp.signing.agent_resolver import AgentResolution

    resolution = AgentResolution(
        agent_url=AGENT,
        brand_json_url=RECORD,
        agent_entry=entry(),
        jwks_uri=JWKS,
        jwks={"keys": [KEY]},
        fetched_at=NOW,
        key_origins={"webhook_signing": "https://example.com"},
    )

    async def resolve(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["signing_purpose"] == "webhook_signing"
        return resolution

    calls = []

    async def pins(domains: Any, agent_url: str) -> Any:
        calls.append((domains, agent_url))
        return (
            {"publisher.com": [{"kid": KEY["kid"]}]}
            if len(calls) == 1
            else {"publisher.com": [KEY]}
        )

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", resolve)
    monkeypatch.setattr(adagents, "fetch_publisher_signing_pins", pins)
    body = b'{"publisher_domains":["attacker.com"]}'
    sender = await verify_webhook_from_agent_url(
        method="POST",
        url=URL,
        headers=signed_headers(body),
        body=body,
        agent_url=AGENT,
        publisher_domains=["publisher.com"],
        clock=lambda: NOW,
    )
    assert sender.sender_url == AGENT
    assert calls == [(("publisher.com",), AGENT), (("publisher.com",), AGENT)]


@pytest.mark.asyncio
async def test_jwks_miss_refresh_does_not_reuse_a_removed_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fetch(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return {"keys": [KEY]}

    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", fetch)
    monkeypatch.setattr(
        agent_resolver, "_JWKS_MISS_REFRESHES", __import__("collections").OrderedDict()
    )
    assert await agent_resolver._refresh_jwks_after_miss(JWKS, allow_private=False) == {
        "keys": [KEY]
    }
    assert await agent_resolver._refresh_jwks_after_miss(JWKS, allow_private=False) is None
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mcp-json", "mcp-sse", "a2a"])
async def test_capabilities_are_a_protocol_call(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A2AAdapter, "_send_and_aggregate", _real_send_and_aggregate)
    methods = []
    caps = {"identity": {"brand_json_url": RECORD}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if mode != "a2a":
                return httpx.Response(405)
            return httpx.Response(
                200,
                json={
                    "name": "test",
                    "description": "Test agent",
                    "version": "1",
                    "url": AGENT,
                    "capabilities": {},
                    "skills": [],
                    "defaultInputModes": ["application/json"],
                    "defaultOutputModes": ["application/json"],
                },
            )
        if request.method == "DELETE":
            assert request.headers["mcp-session-id"] == "session"
            return httpx.Response(200)
        assert request.method == "POST"
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": payload["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "test", "version": "1"},
                    },
                },
                headers={"mcp-session-id": "session"},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        if mode == "a2a":
            result = {
                "kind": "task",
                "id": "capabilities-task",
                "contextId": "ctx",
                "status": {"state": "completed"},
                "artifacts": [{"artifactId": "caps", "parts": [{"kind": "data", "data": caps}]}],
            }
        else:
            assert request.headers["mcp-session-id"] == "session"
            assert payload["params"]["name"] == "get_adcp_capabilities"
            result = {"content": [{"type": "text", "text": json.dumps(caps)}]}
        response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        if mode == "mcp-sse":
            return httpx.Response(
                200,
                content=f"data: {json.dumps(response)}\n\n".encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    def factory(_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    payload = await agent_resolver._fetch_capabilities(
        AGENT,
        allow_private=False,
        max_body_bytes=65536,
        max_redirects=0,
        timeout_seconds=10,
        client_factory=factory,
        protocol="a2a" if mode == "a2a" else "mcp",
    )
    assert payload.body == caps
    assert methods == (
        ["message/send"]
        if mode == "a2a"
        else ["initialize", "notifications/initialized", "tools/call"]
    )


@pytest.mark.asyncio
async def test_capabilities_discovery_against_real_mcp_server() -> None:
    from contextlib import asynccontextmanager

    from adcp.testing import build_test_client
    from tests.test_testing_decisioning import _SalesPlatformWithMethods

    async with build_test_client(
        _SalesPlatformWithMethods(), enable_dns_rebinding_protection=False
    ) as client:

        @asynccontextmanager
        async def factory(_url: str):
            yield client

        result = await agent_resolver._fetch_capabilities(
            "http://test/mcp/",
            allow_private=True,
            max_body_bytes=65536,
            max_redirects=0,
            timeout_seconds=10,
            client_factory=factory,
        )
        assert result.body["supported_protocols"]
