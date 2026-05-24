from __future__ import annotations

import copy
import hashlib
import hmac
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from adcp import NotificationConfig
from adcp.webhooks import (
    WebhookChallengeError,
    WebhookDestinationPolicy,
    WebhookSender,
    challenge_webhook_destination,
    create_webhook_challenge_payload,
    validate_webhook_challenge_response,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

VECTORS_DIR = Path(__file__).parent / "conformance" / "vectors" / "request-signing"
_KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
_REQUEST_ED25519 = next(k for k in _KEYS if k["kid"] == "test-ed25519-2026")
_WEBHOOK_JWK = {
    **copy.deepcopy(_REQUEST_ED25519),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}


def _rfc9421_sender(*, timeout_seconds: float = 10.0) -> WebhookSender:
    return WebhookSender.from_jwk(
        {**_WEBHOOK_JWK, "d": _WEBHOOK_JWK["_private_d_for_test_only"]},
        timeout_seconds=timeout_seconds,
    )


def _public_dns(monkeypatch: pytest.MonkeyPatch, host: str = "buyer.example") -> None:
    def fake_getaddrinfo(query_host: str, port: object) -> list[tuple[object, ...]]:
        assert query_host == host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("adcp.signing.jwks.socket.getaddrinfo", fake_getaddrinfo)


def _challenge_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"challenge": body["challenge"]})

    return httpx.MockTransport(handler)


def test_create_webhook_challenge_payload_shape() -> None:
    payload = create_webhook_challenge_payload(
        account_id="acct_1",
        subscriber_id="buyer-primary",
        challenge="opaque-random-value",
    )

    assert payload == {
        "type": "webhook.challenge",
        "challenge": "opaque-random-value",
        "account_id": "acct_1",
        "subscriber_id": "buyer-primary",
    }


def test_create_webhook_challenge_payload_rejects_empty_challenge() -> None:
    with pytest.raises(ValueError, match="challenge"):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            challenge="",
        )


@pytest.mark.parametrize("body", [b'{"challenge":"abc"}', {"token": "abc"}])
def test_validate_webhook_challenge_response_accepts_challenge_or_token(
    body: bytes | dict[str, str],
) -> None:
    assert validate_webhook_challenge_response(body, challenge="abc") in {"challenge", "token"}


def test_validate_webhook_challenge_response_rejects_mismatch() -> None:
    with pytest.raises(WebhookChallengeError) as exc:
        validate_webhook_challenge_response(b'{"challenge":"wrong"}', challenge="expected")

    assert exc.value.reason == "challenge_mismatch"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_with_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    sender = _rfc9421_sender()

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=_challenge_transport(captured),
    ):
        result = await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=sender,
            challenge="challenge_123",
            field="accounts[0].notification_configs[0].url",
        )

    assert result.ok
    assert result.echoed_field == "challenge"
    assert result.challenge == "challenge_123"
    assert result.destination.effective_url == "https://buyer.example/webhooks/catalog"
    assert "signature" in captured[0].headers
    assert "signature-input" in captured[0].headers
    assert "content-digest" in captured[0].headers
    assert "authorization" not in captured[0].headers
    sent = json.loads(captured[0].content)
    assert sent == {
        "type": "webhook.challenge",
        "challenge": "challenge_123",
        "account_id": "acct_1",
        "subscriber_id": "buyer-primary",
    }


@pytest.mark.asyncio
async def test_challenge_webhook_destination_with_legacy_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        return httpx.Response(202, json={"token": body["challenge"]})

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(handler),
    ):
        result = await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="audit-bus",
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
            challenge="legacy_challenge_123",
        )

    assert result.echoed_field == "token"
    assert captured[0].headers["authorization"] == f"Bearer {'b' * 40}"
    assert json.loads(captured[0].content)["type"] == "webhook.challenge"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_accepts_notification_config_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    config = NotificationConfig.model_validate(
        {
            "subscriber_id": "buyer-primary",
            "url": "https://buyer.example/webhooks/catalog",
            "event_types": ["product.updated"],
            "authentication": {"schemes": ["Bearer"], "credentials": "b" * 40},
        }
    )

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=_challenge_transport(captured),
    ):
        result = await challenge_webhook_destination(
            url=config.url,
            account_id="acct_1",
            subscriber_id=config.subscriber_id,
            authentication=config.authentication,
            challenge="typed_url_challenge_123",
        )

    assert result.ok
    assert str(captured[0].url) == "https://buyer.example/webhooks/catalog"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_with_legacy_hmac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    secret = "s" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"challenge": body["challenge"]})

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(handler),
    ):
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="audit-bus",
            authentication={"schemes": ["HMAC-SHA256"], "credentials": secret},
            challenge="hmac_challenge_123",
        )

    sent = captured[0]
    timestamp = sent.headers["x-adcp-timestamp"]
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode() + sent.content,
        hashlib.sha256,
    ).hexdigest()
    assert sent.headers["x-adcp-signature"] == f"sha256={expected}"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_rejects_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "nope"})

    sender = _rfc9421_sender()

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(handler),
    ):
        with pytest.raises(WebhookChallengeError) as exc:
            await challenge_webhook_destination(
                url="https://buyer.example/webhooks/catalog",
                account_id="acct_1",
                subscriber_id="buyer-primary",
                sender=sender,
                challenge="challenge_123",
            )

    assert exc.value.reason == "http_status"
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_challenge_webhook_destination_local_development_policy() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        body = json.loads(request.content)
        return httpx.Response(200, json={"challenge": body["challenge"]})

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(handler),
    ):
        result = await challenge_webhook_destination(
            url="http://localhost/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
            challenge="local_challenge_123",
            policy=WebhookDestinationPolicy.local_development(),
        )

    assert result.ok
    assert captured["url"] == "http://localhost/webhooks/catalog"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_rejects_sender_with_custom_client() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        sender = WebhookSender.from_bearer_token("sender-token", client=client)
        with pytest.raises(WebhookChallengeError) as exc:
            await challenge_webhook_destination(
                url="https://buyer.example/webhooks/catalog",
                account_id="acct_1",
                subscriber_id="buyer-primary",
                sender=sender,
            )

    assert exc.value.reason == "unsafe_sender_client"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_rejects_non_rfc9421_sender() -> None:
    sender = WebhookSender.from_bearer_token("sender-token")

    with pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=sender,
        )

    assert exc.value.reason == "sender_auth_mode_mismatch"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_rejects_sender_transport_hooks() -> None:
    class RewriteHook:
        def rewrite_url(self, url: str) -> str:
            return url.replace("buyer.example", "other.example")

    sender = WebhookSender.from_jwk(
        {**_WEBHOOK_JWK, "d": _WEBHOOK_JWK["_private_d_for_test_only"]},
        transport_hooks=(RewriteHook(),),
    )

    with pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=sender,
        )

    assert exc.value.reason == "unsupported_sender_hooks"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_rejects_sender_host_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    sender = _rfc9421_sender()

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=_challenge_transport([]),
    ):
        with pytest.raises(WebhookChallengeError) as exc:
            await challenge_webhook_destination(
                url="https://buyer.example/webhooks/catalog",
                account_id="acct_1",
                subscriber_id="buyer-primary",
                sender=sender,
                extra_headers={"Host": "evil.internal"},
            )

    assert exc.value.reason == "invalid_configuration"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_wraps_preflight_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url="http://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )

    assert exc.value.reason == "https_required"

    _public_dns(monkeypatch)
    with pytest.raises(WebhookChallengeError) as empty_id:
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="",
            subscriber_id="buyer-primary",
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )

    assert empty_id.value.reason == "invalid_configuration"

    with pytest.raises(WebhookChallengeError) as empty_challenge:
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
            challenge="",
        )

    assert empty_challenge.value.reason == "invalid_configuration"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_sender_uses_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
        assert host == "localhost"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr("adcp.signing.jwks.socket.getaddrinfo", fake_getaddrinfo)
    sender = _rfc9421_sender()

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=_challenge_transport(captured),
    ) as build_transport:
        result = await challenge_webhook_destination(
            url="http://localhost/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=sender,
            challenge="local_sender_challenge_123",
            policy=WebhookDestinationPolicy.local_development(),
        )

    assert result.ok
    assert str(captured[0].url) == "http://localhost/webhooks/catalog"
    assert build_transport.call_args.kwargs["allow_private"] is True


@pytest.mark.asyncio
async def test_challenge_webhook_destination_sender_uses_sender_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    sender = _rfc9421_sender(timeout_seconds=42.0)

    with (
        patch(
            "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
            return_value=_challenge_transport([]),
        ),
        patch("adcp.webhooks.httpx.AsyncClient") as async_client,
    ):
        async_client.return_value.__aenter__.return_value.post.return_value = httpx.Response(
            200,
            json={"challenge": "timeout_challenge_123"},
        )
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=sender,
            challenge="timeout_challenge_123",
        )

    assert async_client.call_args.kwargs["timeout"] == 42.0
