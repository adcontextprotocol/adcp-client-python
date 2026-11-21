"""adcp 9.6 webhook challenge call shapes keep working, with deprecation warnings.

AdCP 3.2 challenges carry ``seller_agent_url``, ``delivery_auth`` and
``event_types`` and are always signed with RFC 9421. adcp 9.x keeps the
calls that worked on 9.6.2 (no new arguments, or no RFC 9421 sender) working
and warns that 10.0 requires the new shape. Delete this module in 10.0.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import socket
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from adcp.webhooks import (
    WebhookChallengeError,
    WebhookSender,
    challenge_webhook_destination,
    create_webhook_challenge_payload,
)

# Tests that assert on warnings capture them explicitly; leftovers are expected.
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
_BUYER_URL = "https://buyer.example/webhooks/catalog"

_FIELDS_WARNING = r"called without seller_agent_url and event_types.*required in adcp 10\.0"
_NO_RFC9421_WARNING = r"legacy Bearer or HMAC-SHA256.*adcp 10\.0"


def _rfc9421_sender(**kwargs: Any) -> WebhookSender:
    return WebhookSender.from_jwk(
        {**_WEBHOOK_JWK, "d": _WEBHOOK_JWK["_private_d_for_test_only"]},
        **kwargs,
    )


def _public_dns(monkeypatch: pytest.MonkeyPatch, host: str = "buyer.example") -> None:
    def fake_getaddrinfo(query_host: str, port: object) -> list[tuple[object, ...]]:
        assert query_host == host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("adcp.signing.jwks.socket.getaddrinfo", fake_getaddrinfo)


def _echo_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"challenge": body["challenge"]})

    return httpx.MockTransport(handler)


@contextmanager
def _pinned_transport(captured: list[httpx.Request]) -> Iterator[None]:
    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=_echo_transport(captured),
    ):
        yield


@contextmanager
def _recorded_deprecations() -> Iterator[list[warnings.WarningMessage]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught


def _messages(caught: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]


def _legacy_body(challenge: str, subscriber_id: str = "buyer-primary") -> dict[str, str]:
    return {
        "type": "webhook.challenge",
        "challenge": challenge,
        "account_id": "acct_1",
        "subscriber_id": subscriber_id,
    }


# --- create_webhook_challenge_payload ---------------------------------------


def test_payload_without_new_fields_returns_9_6_shape_and_warns() -> None:
    with pytest.warns(DeprecationWarning, match=_FIELDS_WARNING) as record:
        payload = create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            # 9.6 accepted challenges the 3.2 schema rejects as too short.
            challenge="opaque-random-value",
        )

    assert payload == _legacy_body("opaque-random-value")
    assert record[0].filename == __file__


def test_payload_without_new_fields_generates_a_challenge() -> None:
    with pytest.warns(DeprecationWarning, match=_FIELDS_WARNING):
        payload = create_webhook_challenge_payload(account_id="acct_1", subscriber_id="sub")

    assert set(payload) == {"type", "challenge", "account_id", "subscriber_id"}
    assert payload["challenge"].startswith("wch_")


def test_payload_without_new_fields_keeps_9_6_validation() -> None:
    with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match="challenge"):
        create_webhook_challenge_payload(account_id="acct_1", subscriber_id="sub", challenge="")


def test_payload_with_new_fields_does_not_warn() -> None:
    with _recorded_deprecations() as caught:
        payload = create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            challenge=_VALID_CHALLENGE,
        )

    assert payload["delivery_auth"] == {"mode": "rfc9421"}
    assert _messages(caught) == []


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        ({"seller_agent_url": _SELLER_AGENT_URL}, "event_types"),
        ({"event_types": _EVENT_TYPES}, "seller_agent_url"),
    ],
)
def test_payload_rejects_only_one_new_field(kwargs: dict[str, Any], missing: str) -> None:
    with pytest.raises(TypeError, match=f"together; {missing} is missing"):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            challenge=_VALID_CHALLENGE,
            **kwargs,
        )


def test_payload_rejects_authentication_without_new_fields() -> None:
    with pytest.raises(TypeError, match="seller_agent_url and event_types"):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )


# --- WebhookSender.send_webhook_challenge -----------------------------------


@pytest.mark.asyncio
async def test_sender_challenge_without_new_fields_sends_9_6_payload() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_echo_transport(captured)) as client:
        sender = _rfc9421_sender(client=client)
        with _recorded_deprecations() as caught:
            result = await sender.send_webhook_challenge(
                url=_BUYER_URL,
                account_id="acct_1",
                subscriber_id="buyer-primary",
                challenge="challenge_123",
            )

    assert result.ok
    assert json.loads(captured[0].content) == _legacy_body("challenge_123")
    assert "signature" in captured[0].headers
    messages = _messages(caught)
    assert len(messages) == 1
    assert "WebhookSender.send_webhook_challenge() called without seller_agent_url" in messages[0]
    assert caught[0].filename == __file__


@pytest.mark.asyncio
async def test_bearer_sender_challenge_falls_back_to_legacy_auth() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_echo_transport(captured)) as client:
        sender = WebhookSender.from_bearer_token("t" * 40, client=client)
        with _recorded_deprecations() as caught:
            result = await sender.send_webhook_challenge(
                url=_BUYER_URL,
                account_id="acct_1",
                subscriber_id="buyer-primary",
                challenge="challenge_123",
            )

    assert result.ok
    assert captured[0].headers["authorization"] == f"Bearer {'t' * 40}"
    assert "signature" not in captured[0].headers
    assert json.loads(captured[0].content) == _legacy_body("challenge_123")
    messages = _messages(caught)
    assert any("without seller_agent_url and event_types" in m for m in messages)
    fallback = [w for w in caught if "does not sign with RFC 9421" in str(w.message)]
    assert len(fallback) == 1
    assert fallback[0].filename == __file__


@pytest.mark.asyncio
async def test_hmac_sender_challenge_falls_back_to_legacy_auth() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_echo_transport(captured)) as client:
        sender = WebhookSender.from_adcp_legacy_hmac(b"s" * 40, key_id="hmac-1", client=client)
        with pytest.warns(DeprecationWarning, match="does not sign with RFC 9421"):
            result = await sender.send_webhook_challenge(
                url=_BUYER_URL,
                account_id="acct_1",
                subscriber_id="buyer-primary",
                challenge="challenge_123",
            )

    assert result.ok
    assert captured[0].headers["x-adcp-signature"].startswith("sha256=")
    assert json.loads(captured[0].content) == _legacy_body("challenge_123")


@pytest.mark.asyncio
async def test_bearer_sender_with_new_fields_still_requires_rfc9421() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_echo_transport(captured)) as client:
        sender = WebhookSender.from_bearer_token("t" * 40, client=client)
        with pytest.raises(ValueError, match="RFC 9421"):
            await sender.send_webhook_challenge(
                url=_BUYER_URL,
                account_id="acct_1",
                subscriber_id="buyer-primary",
                seller_agent_url=_SELLER_AGENT_URL,
                event_types=_EVENT_TYPES,
                challenge=_VALID_CHALLENGE,
            )

    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"seller_agent_url": _SELLER_AGENT_URL}, "event_types is missing"),
        ({"event_types": _EVENT_TYPES}, "seller_agent_url is missing"),
        (
            {"authentication": {"schemes": ["Bearer"], "credentials": "b" * 40}},
            "seller_agent_url and event_types too",
        ),
    ],
)
async def test_sender_challenge_rejects_mixed_shapes_before_io(
    kwargs: dict[str, Any], error: str
) -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_echo_transport(captured)) as client:
        sender = _rfc9421_sender(client=client)
        with pytest.raises(TypeError, match=error):
            await sender.send_webhook_challenge(
                url=_BUYER_URL,
                account_id="acct_1",
                subscriber_id="buyer-primary",
                challenge=_VALID_CHALLENGE,
                **kwargs,
            )

    assert captured == []


# --- challenge_webhook_destination ------------------------------------------


@pytest.mark.asyncio
async def test_destination_challenge_with_sender_only_sends_9_6_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    with _pinned_transport(captured), _recorded_deprecations() as caught:
        result = await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=_rfc9421_sender(),
            challenge="challenge_123",
        )

    assert result.ok
    assert result.challenge == "challenge_123"
    assert json.loads(captured[0].content) == _legacy_body("challenge_123")
    assert "signature" in captured[0].headers
    assert "authorization" not in captured[0].headers
    messages = _messages(caught)
    assert len(messages) == 1
    assert "challenge_webhook_destination() called without seller_agent_url" in messages[0]
    assert "sender=WebhookSender.from_jwk" in messages[0]
    assert caught[0].filename == __file__


@pytest.mark.asyncio
async def test_destination_challenge_with_bearer_authentication_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    with _pinned_transport(captured), _recorded_deprecations() as caught:
        result = await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="audit-bus",
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
            challenge="legacy_challenge_123",
        )

    assert result.ok
    assert captured[0].headers["authorization"] == f"Bearer {'b' * 40}"
    assert "signature" not in captured[0].headers
    assert json.loads(captured[0].content) == _legacy_body("legacy_challenge_123", "audit-bus")
    messages = _messages(caught)
    assert any("without seller_agent_url and event_types" in m for m in messages)
    fallback = [w for w in caught if "authentication= and no sender=" in str(w.message)]
    assert len(fallback) == 1
    assert fallback[0].filename == __file__
    assert all(w.filename == __file__ for w in caught if "seller_agent_url" in str(w.message))


@pytest.mark.asyncio
async def test_destination_challenge_with_hmac_authentication_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    secret = "s" * 40
    with _pinned_transport(captured), pytest.warns(DeprecationWarning, match=_NO_RFC9421_WARNING):
        await challenge_webhook_destination(
            url=_BUYER_URL,
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
    assert json.loads(sent.content) == _legacy_body("hmac_challenge_123", "audit-bus")


@pytest.mark.asyncio
async def test_destination_challenge_legacy_shape_keeps_9_6_rejections() -> None:
    with pytest.warns(DeprecationWarning), pytest.raises(WebhookChallengeError) as both:
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=_rfc9421_sender(),
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )
    assert both.value.reason == "ambiguous_auth_mode"
    assert both.value.suggestion is not None
    assert "seller_agent_url and event_types" in both.value.suggestion

    with pytest.warns(DeprecationWarning), pytest.raises(WebhookChallengeError) as neither:
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="buyer-primary",
        )
    assert neither.value.reason == "sender_required"

    with pytest.warns(DeprecationWarning), pytest.raises(WebhookChallengeError) as unsigned:
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=WebhookSender.from_bearer_token("t" * 40),
        )
    assert unsigned.value.reason == "sender_auth_mode_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        ({"seller_agent_url": _SELLER_AGENT_URL}, "event_types"),
        ({"event_types": _EVENT_TYPES}, "seller_agent_url"),
    ],
)
async def test_destination_challenge_rejects_only_one_new_field(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], missing: str
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    with (
        _pinned_transport(captured),
        pytest.raises(TypeError, match=f"together; {missing} is missing"),
    ):
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="buyer-primary",
            sender=_rfc9421_sender(),
            **kwargs,
        )

    assert captured == []


# --- explicit None follows the 3.2 path, as on main --------------------------
#
# Only omitting both new arguments selects the deprecated 9.6 shape. A caller
# who passes them, even as None (say, from empty config), gets the 3.2 contract
# and its failures, never a silent four-field challenge.


def test_payload_with_explicit_none_seller_agent_url_fails_schema_validation() -> None:
    with (
        _recorded_deprecations() as caught,
        pytest.raises(ValueError, match="webhook challenge payload failed schema validation"),
    ):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=None,  # type: ignore[arg-type]
            event_types=_EVENT_TYPES,
            challenge=_VALID_CHALLENGE,
        )

    assert not any("seller_agent_url and event_types" in m for m in _messages(caught))


def test_payload_with_both_new_fields_none_does_not_build_the_9_6_payload() -> None:
    with _recorded_deprecations() as caught, pytest.raises((TypeError, ValueError)):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=None,  # type: ignore[arg-type]
            event_types=None,  # type: ignore[arg-type]
            challenge=_VALID_CHALLENGE,
        )

    assert not any("seller_agent_url and event_types" in m for m in _messages(caught))


def test_payload_with_one_none_and_one_omitted_reports_the_missing_argument() -> None:
    with pytest.raises(TypeError, match="together; event_types is missing"):
        create_webhook_challenge_payload(
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_destination_challenge_with_explicit_none_is_a_challenge_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    with _pinned_transport(captured), pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=None,  # type: ignore[arg-type]
            event_types=_EVENT_TYPES,
            sender=_rfc9421_sender(),
            challenge=_VALID_CHALLENGE,
        )

    assert exc.value.reason == "invalid_configuration"
    assert captured == []


@pytest.mark.asyncio
async def test_destination_challenge_with_none_fields_and_no_sender_requires_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    with _pinned_transport(captured), pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="audit-bus",
            seller_agent_url=None,  # type: ignore[arg-type]
            event_types=None,  # type: ignore[arg-type]
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )

    assert exc.value.reason == "sender_required"
    assert captured == []


@pytest.mark.asyncio
async def test_bearer_sender_challenge_with_none_fields_still_requires_rfc9421() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_echo_transport(captured)) as client:
        sender = WebhookSender.from_bearer_token("t" * 40, client=client)
        with pytest.raises(ValueError, match="RFC 9421"):
            await sender.send_webhook_challenge(
                url=_BUYER_URL,
                account_id="acct_1",
                subscriber_id="buyer-primary",
                seller_agent_url=None,  # type: ignore[arg-type]
                event_types=None,  # type: ignore[arg-type]
                challenge=_VALID_CHALLENGE,
            )

    assert captured == []


@pytest.mark.asyncio
async def test_destination_challenge_with_new_fields_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    captured: list[httpx.Request] = []
    with _pinned_transport(captured), _recorded_deprecations() as caught:
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="buyer-primary",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            sender=_rfc9421_sender(),
            challenge=_VALID_CHALLENGE,
        )

    assert json.loads(captured[0].content)["seller_agent_url"] == _SELLER_AGENT_URL
    assert _messages(caught) == []


@pytest.mark.asyncio
async def test_destination_challenge_with_new_fields_still_requires_a_sender() -> None:
    """Passing the 3.2 fields opts into the 3.2 contract, sender included."""

    with pytest.raises(WebhookChallengeError) as exc:
        await challenge_webhook_destination(
            url=_BUYER_URL,
            account_id="acct_1",
            subscriber_id="audit-bus",
            seller_agent_url=_SELLER_AGENT_URL,
            event_types=_EVENT_TYPES,
            authentication={"schemes": ["Bearer"], "credentials": "b" * 40},
        )

    assert exc.value.reason == "sender_required"
