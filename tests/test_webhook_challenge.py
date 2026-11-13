from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from adcp import NotificationConfig
from adcp.validation.schema_loader import get_named_validator
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
_SELLER_AGENT_URL = "https://seller.example/adcp"
_EVENT_TYPES = ["creative.status_changed"]
_VALID_CHALLENGE = "challenge_123_00000000000000000000"


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
        seller_agent_url="https://seller.example/adcp",
        event_types=["creative.status_changed", "creative.purged"],
        challenge="opaque-random-value-0000000000000",
    )

    assert payload == {
        "type": "webhook.challenge",
        "challenge": "opaque-random-value-0000000000000",
        "account_id": "acct_1",
        "subscriber_id": "buyer-primary",
        "seller_agent_url": "https://seller.example/adcp",
        "delivery_auth": {"mode": "rfc9421"},
        "event_types": ["creative.status_changed", "creative.purged"],
    }


@pytest.mark.parametrize(
    ("scheme", "credentials", "fingerprint"),
    [
        (
            "Bearer",
            "b" * 40,
            "e26d2da3ab585c9840b157a4a9b5639fda905c2ad33d04d2c598be8883b51236",
        ),
        (
            "HMAC-SHA256",
            "s" * 40,
            "3923fb8348df77529b04f6f536b8204856d0501d031063de795c57ad693b288f",
        ),
    ],
)
def test_create_webhook_challenge_payload_binds_legacy_delivery_auth(
    scheme: str,
    credentials: str,
    fingerprint: str,
) -> None:
    payload = create_webhook_challenge_payload(
        account_id="acct_1",
        subscriber_id="buyer-primary",
        seller_agent_url="https://seller.example/adcp",
        event_types=["creative.status_changed"],
        authentication={"schemes": [scheme], "credentials": credentials},
        challenge="opaque-random-value-0000000000000",
    )

    assert payload["delivery_auth"] == {
        "mode": scheme,
        "credential_fingerprint": fingerprint,
    }
    assert credentials not in json.dumps(payload)


@pytest.mark.parametrize(
    "authentication",
    [
        {"schemes": [], "credentials": "b" * 40},
        {"schemes": ["Basic"], "credentials": "b" * 40},
        {"schemes": ["Bearer"]},
        {"schemes": ["HMAC-SHA256"], "credentials": "too-short"},
    ],
)
def test_create_webhook_challenge_payload_rejects_invalid_legacy_delivery_auth(
    authentication: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="authentication"):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url="https://seller.example/adcp",
            event_types=["creative.status_changed"],
            authentication=authentication,
            challenge="opaque-random-value-0000000000000",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subscriber_id", "buyer primary"),
        ("seller_agent_url", "not a uri"),
        ("event_types", []),
        ("event_types", ["not.a.notification"]),
        ("event_types", ["creative.purged", "creative.purged"]),
        ("challenge", "too-short"),
    ],
)
def test_create_webhook_challenge_payload_rejects_schema_invalid_required_fields(
    field: str,
    value: object,
) -> None:
    arguments: dict[str, Any] = {
        "account_id": "acct_1",
        "subscriber_id": "buyer-primary",
        "seller_agent_url": "https://seller.example/adcp",
        "event_types": ["creative.status_changed"],
        "challenge": "opaque-random-value-0000000000000",
    }
    arguments[field] = value

    with pytest.raises(ValueError, match="webhook challenge payload"):
        create_webhook_challenge_payload(**arguments)


def test_create_webhook_challenge_payload_validates_against_cached_3_2_schema() -> None:
    payload = create_webhook_challenge_payload(
        account_id="acct_1",
        subscriber_id="buyer-primary",
        seller_agent_url="https://seller.example/adcp",
        event_types=["creative.status_changed", "creative.purged"],
        challenge="opaque-random-value-0000000000000",
    )
    validator = get_named_validator("core/webhook-challenge.json", version="3.2")

    assert validator is not None
    assert list(validator.iter_errors(payload)) == []


def test_create_webhook_challenge_payload_rejects_empty_challenge() -> None:
    with pytest.raises(ValueError, match="challenge"):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url="https://seller.example/adcp",
            event_types=["creative.status_changed"],
            challenge="",
        )


@pytest.mark.asyncio
async def test_webhook_sender_sends_complete_challenge_payload() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_challenge_transport(captured)) as client:
        sender = WebhookSender.from_jwk(
            {**_WEBHOOK_JWK, "d": _WEBHOOK_JWK["_private_d_for_test_only"]},
            client=client,
        )
        result = await sender.send_webhook_challenge(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url="https://seller.example/adcp",
            event_types=["creative.status_changed"],
            challenge="challenge_123_00000000000000000000",
        )

    assert result.ok
    assert json.loads(captured[0].content) == {
        "type": "webhook.challenge",
        "challenge": "challenge_123_00000000000000000000",
        "account_id": "acct_1",
        "subscriber_id": "buyer-primary",
        "seller_agent_url": "https://seller.example/adcp",
        "delivery_auth": {"mode": "rfc9421"},
        "event_types": ["creative.status_changed"],
    }


@pytest.mark.asyncio
async def test_webhook_sender_accepts_current_request_signing_jwk() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_challenge_transport(captured)) as client:
        sender = WebhookSender.from_jwk(
            {**_REQUEST_ED25519, "d": _REQUEST_ED25519["_private_d_for_test_only"]},
            client=client,
        )
        result = await sender.send_webhook_challenge(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            challenge=_VALID_CHALLENGE,
        )

    assert result.ok
    assert captured[0].headers["Signature-Input"]
    assert captured[0].headers["Signature"]


@pytest.mark.asyncio
async def test_webhook_sender_rejects_unsigned_challenge_sender() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_challenge_transport(captured)) as client:
        sender = WebhookSender.from_bearer_token("sender-token", client=client)
        with pytest.raises(ValueError, match="RFC 9421"):
            await sender.send_webhook_challenge(
                url="https://buyer.example/webhooks/catalog",
                account_id="acct_1",
                subscriber_id="buyer-primary",
                seller_agent_url=_SELLER_AGENT_URL,
                event_types=_EVENT_TYPES,
                challenge=_VALID_CHALLENGE,
            )

    assert captured == []


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
            seller_agent_url="https://seller.example/adcp",
            event_types=["creative.status_changed", "creative.purged"],
            sender=sender,
            challenge="challenge_123_00000000000000000000",
            field="accounts[0].notification_configs[0].url",
        )

    assert result.ok
    assert result.echoed_field == "challenge"
    assert result.challenge == "challenge_123_00000000000000000000"
    assert result.destination.effective_url == "https://buyer.example/webhooks/catalog"
    assert "signature" in captured[0].headers
    assert "signature-input" in captured[0].headers
    assert "content-digest" in captured[0].headers
    assert "authorization" not in captured[0].headers
    sent = json.loads(captured[0].content)
    assert sent == {
        "type": "webhook.challenge",
        "challenge": "challenge_123_00000000000000000000",
        "account_id": "acct_1",
        "subscriber_id": "buyer-primary",
        "seller_agent_url": "https://seller.example/adcp",
        "delivery_auth": {"mode": "rfc9421"},
        "event_types": ["creative.status_changed", "creative.purged"],
    }


@pytest.mark.asyncio
async def test_challenge_webhook_destination_with_legacy_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    sender = _rfc9421_sender()

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
            seller_agent_url="https://seller.example/adcp",
            event_types=["creative.purged"],
            sender=sender,
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
            challenge="legacy_challenge_123_000000000000",
        )

    assert result.echoed_field == "token"
    assert "authorization" not in captured[0].headers
    assert "signature" in captured[0].headers
    assert json.loads(captured[0].content)["delivery_auth"] == {
        "mode": "Bearer",
        "credential_fingerprint": (
            "e26d2da3ab585c9840b157a4a9b5639fda905c2ad33d04d2c598be8883b51236"
        ),
    }


@pytest.mark.asyncio
async def test_challenge_webhook_destination_accepts_notification_config_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    sender = _rfc9421_sender()
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
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=config.event_types,
            sender=sender,
            authentication=config.authentication,
            challenge=_VALID_CHALLENGE,
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
    sender = _rfc9421_sender()

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
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=["creative.purged"],
            sender=sender,
            authentication={"schemes": ["HMAC-SHA256"], "credentials": secret},
            challenge=_VALID_CHALLENGE,
        )

    sent = captured[0]
    assert "signature" in sent.headers
    assert "x-adcp-signature" not in sent.headers
    assert json.loads(sent.content)["delivery_auth"] == {
        "mode": "HMAC-SHA256",
        "credential_fingerprint": (
            "3923fb8348df77529b04f6f536b8204856d0501d031063de795c57ad693b288f"
        ),
    }


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
                seller_agent_url=_SELLER_AGENT_URL,
                event_types=_EVENT_TYPES,
                sender=sender,
                challenge=_VALID_CHALLENGE,
            )

    assert exc.value.reason == "http_status"
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_challenge_webhook_destination_local_development_policy() -> None:
    captured: dict[str, Any] = {}
    sender = _rfc9421_sender()

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
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
            challenge=_VALID_CHALLENGE,
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
                seller_agent_url=_SELLER_AGENT_URL,
                event_types=_EVENT_TYPES,
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
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
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
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
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
                seller_agent_url=_SELLER_AGENT_URL,
                event_types=_EVENT_TYPES,
                sender=sender,
                extra_headers={"Host": "evil.internal"},
            )

    assert exc.value.reason == "invalid_configuration"


@pytest.mark.asyncio
async def test_challenge_webhook_destination_wraps_preflight_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _rfc9421_sender()
    with pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url="http://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )

    assert exc.value.reason == "https_required"

    _public_dns(monkeypatch)
    with pytest.raises(WebhookChallengeError) as empty_id:
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )

    assert empty_id.value.reason == "invalid_configuration"

    with pytest.raises(WebhookChallengeError) as empty_challenge:
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
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
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
            challenge=_VALID_CHALLENGE,
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
            json={"challenge": _VALID_CHALLENGE},
        )
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
            challenge=_VALID_CHALLENGE,
        )

    assert async_client.call_args.kwargs["timeout"] == 42.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,tag",
    [
        (None, "adcp/webhook-signing/v1"),
        ("adcp/webhook-signing/v1", "adcp/webhook-signing/v1"),
        ("web-bot-auth", "web-bot-auth"),
    ],
)
async def test_challenge_is_signed_under_the_configs_signing_profile(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, tag: str
) -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    sender = _rfc9421_sender().with_web_bot_auth(
        ed25519.Ed25519PrivateKey.generate(), agent_origin="https://seller.example"
    )

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=_challenge_transport(captured),
    ):
        result = await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
            signing_profile=profile,
        )

    assert result.ok
    assert f';tag="{tag}"' in captured[0].headers["signature-input"]
    assert ("signature-agent" in captured[0].headers) is (tag == "web-bot-auth")


@pytest.mark.asyncio
async def test_challenge_refuses_a_profile_the_sender_cannot_sign() -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    sender = WebhookSender.from_wba(
        ed25519.Ed25519PrivateKey.generate(), agent_origin="https://seller.example"
    )
    # An omitted signing_profile means adcp/webhook-signing/v1.
    with pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url="https://buyer.example/webhooks/catalog",
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=sender,
        )
    assert exc.value.reason == "signing_profile_unavailable"
