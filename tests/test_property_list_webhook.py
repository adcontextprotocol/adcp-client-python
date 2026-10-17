from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from adcp.signing import StaticJwksResolver
from adcp.webhooks import WebhookSender, WebhookVerifyOptions, verify_webhook_signature

KEYS = json.loads(
    (Path(__file__).parent / "conformance/vectors/request-signing/keys.json").read_text()
)["keys"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_signature", [None, "old-marker"])
async def test_property_list_marker_and_computed_http_signature(monkeypatch, legacy_signature):
    key = next(key for key in KEYS if key["kid"] == "test-ed25519-2026")
    webhook_key = {**key, "kid": "property-list-webhook", "adcp_use": "webhook-signing"}
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    seen = []

    async def handler(request):
        seen.append(request)
        assert json.loads(request.content)["signature"] == "rfc9421"
        verified = verify_webhook_signature(
            method="POST",
            url=str(request.url),
            headers=dict(request.headers),
            body=request.content,
            options=WebhookVerifyOptions(jwks_resolver=StaticJwksResolver({"keys": [webhook_key]})),
        )
        assert verified.key_id == "property-list-webhook"
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with WebhookSender.from_jwk(
            {**webhook_key, "d": key["_private_d_for_test_only"]},
            client=http,
        ) as sender:
            result = await sender.send_property_list_changed(
                url="https://hooks.example.com/events",
                list_id="approved",
                resolved_at="2026-10-17T00:00:00Z",
                **({"signature": legacy_signature} if legacy_signature is not None else {}),
            )
    assert result.ok
    assert len(seen) == 1
