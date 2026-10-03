"""Security boundaries for webhook discovery, refreshes, and publisher pins."""

from __future__ import annotations

import asyncio
import gc
import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from adcp import adagents
from adcp.exceptions import AdagentsValidationError
from adcp.signing import agent_resolver, ip_pinned_transport, jwks
from adcp.signing.agent_resolver import AgentResolution
from adcp.signing.crypto import private_key_from_jwk
from adcp.signing.errors import SignatureVerificationError
from adcp.signing.revocation import RevocationList
from adcp.signing.webhook_signer import sign_webhook
from adcp.signing.webhook_verifier import (
    WebhookVerifyOptions,
    verify_webhook_from_agent_url,
    verify_webhook_signature,
)

AGENT = "https://seller.example.com/mcp"
JWKS = "https://example.com/keys.json"
URL = "https://buyer.example.com/webhooks"
NOW = 1776520800
KEY = json.loads(
    (Path(__file__).parent / "conformance/vectors/request-signing/keys.json").read_text()
)["keys"][0]
KEY = {**KEY, "adcp_use": "request-signing"}


def signed_headers(*, created: int = NOW) -> dict[str, str]:
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers={"content-type": "application/json"},
        body=b"{}",
        private_key=private_key_from_jwk(KEY, d_field="_private_d_for_test_only"),
        key_id=KEY["kid"],
        alg="ed25519",
        created=created,
    )
    return {"content-type": "application/json", **signed.as_dict()}


def patch_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    resolution = AgentResolution(
        agent_url=AGENT,
        brand_json_url="https://example.com/operator/brand.json",
        agent_entry={"type": "sales", "url": AGENT, "jwks_uri": JWKS},
        jwks_uri=JWKS,
        jwks={"keys": [KEY]},
        fetched_at=NOW,
        key_origins={"webhook_signing": "https://example.com"},
    )

    async def resolve(*args: Any, **kwargs: Any) -> AgentResolution:
        return resolution

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", resolve)


@pytest.mark.asyncio
async def test_newer_discovery_cannot_reuse_pending_old_jwks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_resolver, "_JWKS_MISS_REFRESHES", OrderedDict())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_fetch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"keys": [KEY]}

    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", old_fetch)
    earlier = asyncio.create_task(
        agent_resolver._refresh_jwks_after_miss(JWKS, allow_private=False)
    )
    await entered.wait()
    try:
        # A later discovery has observed an empty JWKS. Its miss must not
        # recover the key from the still-pending earlier response.
        assert (
            await asyncio.wait_for(
                agent_resolver._refresh_jwks_after_miss(JWKS, allow_private=False), timeout=1
            )
            is None
        )
    finally:
        release.set()
        assert await earlier == {"keys": [KEY]}


@pytest.mark.asyncio
async def test_abandoned_refresh_failure_is_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_resolver, "_JWKS_MISS_REFRESHES", OrderedDict())
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def failed_fetch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        finished.set()
        raise ValueError("refresh failed after caller disconnected")

    monkeypatch.setattr(agent_resolver, "async_default_jwks_fetcher", failed_fetch)
    caller = asyncio.create_task(agent_resolver._refresh_jwks_after_miss(JWKS, allow_private=False))
    try:
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await caller
        release.set()
        await finished.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        agent_resolver._JWKS_MISS_REFRESHES.clear()
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize(
    "body",
    [
        (
            b'{"authorized_agents":[{"url":"https://seller.example.com/mcp",'
            + b'"signing_keys":[],"signing_keys":[{"kid":"key"}]}]}'
        ),
        (
            b'{"authorized_agents":[{"url":"https://seller.example.com/mcp",'
            + b'"signing_keys":[{"revoked_at":"2020-01-01T00:00:00Z",'
            + b'"revoked_at":null}]}]}'
        ),
    ],
)
@pytest.mark.asyncio
async def test_fetched_publisher_pin_document_rejects_duplicate_members(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))

    @asynccontextmanager
    async def client(*args: Any, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(transport=transport) as http:
            yield http

    monkeypatch.setattr(adagents, "_owned_pinned_client", client)
    with pytest.raises(AdagentsValidationError, match="duplicate_key"):
        await adagents.fetch_publisher_signing_pins(("example.com",), AGENT)


@pytest.mark.parametrize("body", [b'{"keys":[],"keys":[]}', b'{"keys":[{"x":"a","x":"b"}]}'])
def test_sync_jwks_fetch_rejects_duplicate_members(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(ip_pinned_transport, "build_ip_pinned_transport", lambda *a, **k: transport)
    with pytest.raises(ValueError, match="duplicate_key"):
        jwks.default_jwks_fetcher(JWKS)


@pytest.mark.parametrize("body", [b'{"keys":[],"keys":[]}', b'{"keys":[{"x":"a","x":"b"}]}'])
@pytest.mark.asyncio
async def test_async_jwks_fetch_rejects_duplicate_members(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(
        ip_pinned_transport, "build_async_ip_pinned_transport", lambda *a, **k: transport
    )
    with pytest.raises(ValueError, match="duplicate_key"):
        await jwks.async_default_jwks_fetcher(JWKS)


@pytest.mark.parametrize("status", ["revoked", "stale", "fresh"])
@pytest.mark.asyncio
async def test_async_webhook_factory_enforces_revocation_hooks(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    patch_discovery(monkeypatch)
    revocations = RevocationList(
        issuer=AGENT,
        updated="2026-04-18T13:00:00Z",
        next_update="2020-01-01T00:00:00Z" if status == "stale" else "2030-01-01T00:00:00Z",
        revoked_kids=frozenset({KEY["kid"]}) if status == "revoked" else frozenset(),
    )
    kwargs = dict(
        method="POST",
        url=URL,
        headers=signed_headers(),
        body=b"{}",
        agent_url=AGENT,
        revocation_checker=revocations.is_revoked,
        revocation_list=revocations,
        clock=lambda: NOW,
    )
    if status == "fresh":
        assert (await verify_webhook_from_agent_url(**kwargs)).key_id == KEY["kid"]
    else:
        with pytest.raises(SignatureVerificationError) as exc:
            await verify_webhook_from_agent_url(**kwargs)
        assert exc.value.code == (
            "webhook_signature_key_revoked"
            if status == "revoked"
            else "webhook_signature_revocation_stale"
        )


@pytest.mark.parametrize("created_offset", [-1, 0, 1])
@pytest.mark.parametrize("async_discovery", [False, True])
@pytest.mark.asyncio
async def test_pin_revocation_applies_at_signature_creation(
    monkeypatch: pytest.MonkeyPatch, created_offset: int, async_discovery: bool
) -> None:
    patch_discovery(monkeypatch)
    revoked_at = datetime.fromtimestamp(NOW, tz=timezone.utc).isoformat()
    pins = {"publisher.com": [{**KEY, "revoked_at": revoked_at}]}

    async def fetch_pins(*args: Any, **kwargs: Any) -> Any:
        return pins

    monkeypatch.setattr(adagents, "fetch_publisher_signing_pins", fetch_pins)
    headers = signed_headers(created=NOW + created_offset)

    async def verify() -> Any:
        if async_discovery:
            return await verify_webhook_from_agent_url(
                method="POST",
                url=URL,
                headers=headers,
                body=b"{}",
                agent_url=AGENT,
                publisher_domains=("publisher.com",),
                clock=lambda: NOW + 2,
            )
        return verify_webhook_signature(
            method="POST",
            url=URL,
            headers=headers,
            body=b"{}",
            options=WebhookVerifyOptions(
                jwks_resolver=agent_resolver._BrandJsonStaticJwksResolver(
                    {"keys": [KEY]}, jwks_uri=JWKS
                ),
                publisher_pins=pins,
                refresh_publisher_pins=lambda: pins,
                expected_key_origins={"webhook_signing": "https://example.com"},
                replay_store=None,
                clock=lambda: NOW + 2,
            ),
        )

    if created_offset < 0:
        assert (await verify()).key_id == KEY["kid"]
    else:
        with pytest.raises(SignatureVerificationError) as exc:
            await verify()
        assert exc.value.code == "webhook_signature_key_unknown"
