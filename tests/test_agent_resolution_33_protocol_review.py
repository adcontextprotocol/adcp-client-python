"""Protocol discovery errors and the legacy document-indirection boundary."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
import pytest

from adcp.signing import agent_resolver
from adcp.signing.agent_resolver import AgentResolution, AgentResolverError, async_resolve_agent
from adcp.signing.crypto import private_key_from_jwk
from adcp.signing.errors import SignatureVerificationError
from adcp.signing.middleware import VERIFIED_SIGNER_SCOPE_KEY
from adcp.signing.replay import InMemoryReplayStore
from adcp.signing.signer import sign_request

AGENT = "https://seller.example.com/mcp"
HOST_RECORD = "https://seller.example.com/.well-known/brand.json"
DOMAIN_RECORD = "https://example.com/.well-known/brand.json"


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"identity": {}},
        {"identity": {"brand_json_url": None}},
        {"identity": {"brand_json_url": ""}},
        {"identity": {"brand_json_url": "http://example.com/brand.json"}},
        {"identity": {"brand_json_url": "https://[broken/brand.json"}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_advertised_record_has_discovery_error_code(
    capabilities: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fetch_capabilities(*args: Any, **kwargs: Any):
        return agent_resolver._CapabilitiesPayload(body=capabilities, final_url=AGENT)

    monkeypatch.setattr(agent_resolver, "_fetch_capabilities", fetch_capabilities)
    with pytest.raises(SignatureVerificationError) as exc:
        await agent_resolver.verify_from_agent_url(object(), AGENT, operation="get_products")
    assert exc.value.code == "request_signature_brand_json_url_missing"


@pytest.mark.parametrize("value", [None, "", "http://example.com/brand.json"])
@pytest.mark.asyncio
async def test_present_invalid_record_never_uses_legacy_fallback(
    value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fetch_capabilities(*args: Any, **kwargs: Any):
        return agent_resolver._CapabilitiesPayload(
            body={"identity": {"brand_json_url": value}}, final_url=AGENT
        )

    def unexpected_fetch(_url: str):
        raise AssertionError("an invalid advertised record must not trigger a fallback fetch")

    monkeypatch.setattr(agent_resolver, "_fetch_capabilities", fetch_capabilities)
    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            AGENT,
            allow_legacy_fallback=True,
            _brand_jwks_client_factory=unexpected_fetch,
        )
    assert exc.value.signature_code == "request_signature_brand_json_url_missing"


@pytest.mark.parametrize("domain_fallback", [False, True])
@pytest.mark.parametrize("indirections", [1, 2])
@pytest.mark.asyncio
async def test_legacy_discovery_allows_one_document_indirection(
    domain_fallback: bool, indirections: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_url = DOMAIN_RECORD if domain_fallback else HOST_RECORD
    middle_url = "https://example.com/first.json"
    terminal_url = "https://example.com/second.json"
    terminal = {"agents": [{"url": AGENT, "type": "sales"}]}
    documents = {
        start_url: {"authoritative_location": middle_url},
        middle_url: terminal if indirections == 1 else {"authoritative_location": terminal_url},
        terminal_url: terminal,
    }
    visited: list[str] = []

    async def fetch_capabilities(*args: Any, **kwargs: Any):
        return agent_resolver._CapabilitiesPayload(body={"identity": {}}, final_url=AGENT)

    async def fetch_jwks(*args: Any, **kwargs: Any):
        return {"keys": []}

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        record = documents.get(str(request.url))
        return httpx.Response(404) if record is None else httpx.Response(200, json=record)

    def factory(_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(agent_resolver, "_fetch_capabilities", fetch_capabilities)
    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", fetch_jwks)
    if indirections == 1:
        resolution = await async_resolve_agent(
            AGENT, allow_legacy_fallback=True, _brand_jwks_client_factory=factory
        )
        assert resolution.legacy_discovery
        assert resolution.brand_json == terminal
    else:
        with pytest.raises(AgentResolverError, match="redirect depth exceeded"):
            await async_resolve_agent(
                AGENT, allow_legacy_fallback=True, _brand_jwks_client_factory=factory
            )
        assert terminal_url not in visited


@pytest.mark.asyncio
async def test_missing_indirection_target_does_not_trigger_domain_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_target = "https://seller.example.com/missing.json"
    documents = {
        HOST_RECORD: {"authoritative_location": missing_target},
        DOMAIN_RECORD: {"agents": [{"url": AGENT, "type": "sales"}]},
    }
    visited: list[str] = []

    async def fetch_capabilities(*args: Any, **kwargs: Any):
        return agent_resolver._CapabilitiesPayload(body={"identity": {}}, final_url=AGENT)

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        record = documents.get(str(request.url))
        return httpx.Response(404) if record is None else httpx.Response(200, json=record)

    def factory(_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(agent_resolver, "_fetch_capabilities", fetch_capabilities)
    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            AGENT, allow_legacy_fallback=True, _brand_jwks_client_factory=factory
        )
    assert exc.value.signature_code == "request_signature_brand_json_unreachable"
    assert visited == [HOST_RECORD, missing_target]
    assert exc.value.__cause__.url == missing_target
    assert exc.value.__cause__.status_code == 404


class _BodyOnceRequest:
    method = "POST"
    url = "https://buyer.example.com/mcp"

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.scope: dict[str, Any] = {}
        self.reads = 0

    async def body(self) -> bytes:
        self.reads += 1
        assert self.reads == 1, "verification retries must reuse the buffered body"
        return b"{}"


@pytest.fixture
def request_rotation(monkeypatch: pytest.MonkeyPatch):
    key = json.loads(
        (Path(__file__).parent / "conformance/vectors/request-signing/keys.json").read_text()
    )["keys"][0]
    key = {**key, "adcp_use": "request-signing"}
    now = 1776520800
    jwks_uri = "https://example.com/request-rotation.json"
    discovered: list[dict[str, Any]] = []
    fetched: list[str] = []
    published = [key]
    failure: list[Exception] = []

    async def resolve(*args: Any, **kwargs: Any) -> AgentResolution:
        return AgentResolution(
            agent_url=AGENT,
            agent_entry={"type": "sales", "url": AGENT},
            brand_json_url=HOST_RECORD,
            brand_json={"agents": [{"type": "sales", "url": AGENT}]},
            jwks_uri=jwks_uri,
            jwks={"keys": list(discovered)},
            key_origins={"request_signing": "https://example.com"},
            fetched_at=now,
        )

    async def fetch(uri: str, **kwargs: Any) -> dict[str, Any]:
        assert not kwargs["allow_private"]
        fetched.append(uri)
        if failure:
            raise failure[0]
        return {"keys": list(published)}

    signed = sign_request(
        method=_BodyOnceRequest.method,
        url=_BodyOnceRequest.url,
        headers={"content-type": "application/json"},
        body=b"{}",
        private_key=private_key_from_jwk(key, d_field="_private_d_for_test_only"),
        key_id=key["kid"],
        alg="ed25519",
        created=now,
        signing_profile_version="3.1",
    )
    headers = {"content-type": "application/json", **signed.as_dict()}
    monkeypatch.setattr(agent_resolver, "async_resolve_agent", resolve)
    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", fetch)
    monkeypatch.setattr(agent_resolver, "_JWKS_MISS_REFRESHES", OrderedDict())
    return now, key, headers, discovered, fetched, published, failure


@pytest.mark.asyncio
async def test_request_factory_refreshes_rotated_key_and_preserves_replay(request_rotation) -> None:
    now, key, headers, discovered, fetched, _, _ = request_rotation
    replay_store = InMemoryReplayStore()
    request = _BodyOnceRequest(headers)
    signer = await agent_resolver.verify_from_agent_url(
        request, AGENT, operation="get_products", now=now, replay_store=replay_store
    )
    assert signer.key_id == key["kid"]
    assert signer.operator_brand_json["agents"][0]["url"] == AGENT
    assert request.reads == 1
    assert request.scope[VERIFIED_SIGNER_SCOPE_KEY] is signer
    assert len(fetched) == 1

    discovered.append(key)
    with pytest.raises(SignatureVerificationError) as exc:
        await agent_resolver.verify_from_agent_url(
            _BodyOnceRequest(headers),
            AGENT,
            operation="get_products",
            now=now,
            replay_store=replay_store,
        )
    assert exc.value.code == "request_signature_replayed"
    assert len(fetched) == 1


@pytest.mark.asyncio
async def test_request_factory_respects_jwks_refresh_cooldown(request_rotation) -> None:
    now, _, headers, _, fetched, published, _ = request_rotation
    published.clear()
    for _ in range(2):
        request = _BodyOnceRequest(headers)
        with pytest.raises(SignatureVerificationError) as exc:
            await agent_resolver.verify_from_agent_url(
                request, AGENT, operation="get_products", now=now, replay_store=None
            )
        assert exc.value.code == "request_signature_key_unknown"
        assert request.reads == 1
    assert len(fetched) == 1

    cache_key, (attempted_at, task) = next(iter(agent_resolver._JWKS_MISS_REFRESHES.items()))
    agent_resolver._JWKS_MISS_REFRESHES[cache_key] = (attempted_at - 31, task)
    with pytest.raises(SignatureVerificationError) as exc:
        await agent_resolver.verify_from_agent_url(
            _BodyOnceRequest(headers), AGENT, operation="get_products", now=now, replay_store=None
        )
    assert exc.value.code == "request_signature_key_unknown"
    assert len(fetched) == 2


@pytest.mark.asyncio
async def test_request_factory_does_not_refresh_malformed_signature(request_rotation) -> None:
    now, _, headers, _, fetched, _, _ = request_rotation
    malformed_headers = {name.lower(): value for name, value in headers.items()}
    malformed_headers["signature-input"] = "malformed"
    request = _BodyOnceRequest(malformed_headers)
    with pytest.raises(SignatureVerificationError) as exc:
        await agent_resolver.verify_from_agent_url(
            request, AGENT, operation="get_products", now=now, replay_store=None
        )
    assert exc.value.code == "request_signature_header_malformed"
    assert request.reads == 1
    assert not fetched


@pytest.mark.asyncio
async def test_request_factory_maps_jwks_refresh_failure(request_rotation) -> None:
    now, _, headers, _, fetched, _, failure = request_rotation
    failure.append(OSError("refresh unavailable"))
    with pytest.raises(SignatureVerificationError) as exc:
        await agent_resolver.verify_from_agent_url(
            _BodyOnceRequest(headers), AGENT, operation="get_products", now=now, replay_store=None
        )
    assert exc.value.code == "request_signature_jwks_unavailable"
    assert isinstance(exc.value.__cause__, OSError)
    assert len(fetched) == 1
