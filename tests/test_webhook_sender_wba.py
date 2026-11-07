"""WebhookSender signs deliveries under ``web-bot-auth`` when a registration asks (#1859).

AdCP 3.3 lets a webhook registration omit ``authentication`` and name a
``signing_profile``; the seller signs under it. These tests cover the
``web-bot-auth`` sender (``from_wba``), a sender holding both profiles
(``with_web_bot_auth``), the key directory that makes its signatures
verifiable, and the outboxes carrying each registration's profile to every
freshly signed attempt.
"""

from __future__ import annotations

import base64
import copy
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.decisioning.capabilities import WebhookSigning
from adcp.decisioning.task_registry import TaskWebhookAuthentication
from adcp.decisioning.types import AdcpError
from adcp.decisioning.webhook_emit import (
    _extract_push_signing_profile,
    validate_webhook_signing_for_capabilities,
)
from adcp.signing import InMemoryReplayStore
from adcp.signing.wba import (
    DIRECTORY_PATH,
    DirectoryResponse,
    DiscoveredSignerKeys,
    public_directory_key,
    verify_directory_response,
    verify_wba_request,
)
from adcp.webhook_sender import (
    PreparedWebhook,
    WebhookDeliveryResult,
    WebhookSender,
    WebhookSigningProfileUnavailableError,
)
from adcp.webhooks import WEBHOOK_SIGNING_PROFILE_ADCP, WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH

ORIGIN = "https://seller.example"
HOOK = "https://buyer.example/webhooks/adcp"
WBA = WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH
ADCP = WEBHOOK_SIGNING_PROFILE_ADCP

_VECTORS = Path(__file__).parent / "conformance" / "vectors" / "request-signing"
_REQUEST_ED25519 = next(
    k
    for k in json.loads((_VECTORS / "keys.json").read_text())["keys"]
    if k["kid"] == "test-ed25519-2026"
)
_WEBHOOK_JWK = {
    **copy.deepcopy(_REQUEST_ED25519),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
    "d": _REQUEST_ED25519["_private_d_for_test_only"],
}


class _Capture(httpx.AsyncBaseTransport):
    """Record every request; answer with the queued statuses, then 200."""

    def __init__(self, *statuses: int) -> None:
        self.requests: list[httpx.Request] = []
        self._statuses = list(statuses)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        status = self._statuses.pop(0) if self._statuses else 200
        return httpx.Response(status, content=b"{}", request=request)


def _key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


def _prepared(profile: str | None = None) -> PreparedWebhook:
    prepared = WebhookSender.prepare_mcp(
        url=HOOK,
        task_id="task_1",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
        operation_id="op_1",
    )
    return replace(prepared, required_signing_profile=profile)


def _tag(request: httpx.Request) -> str:
    return request.headers["signature-input"].split(';tag="', 1)[1].split('"', 1)[0]


async def _verify(request: httpx.Request, directory: DirectoryResponse, replay: Any) -> Any:
    async def fetch(url: str, *, allow_private: bool = False) -> DirectoryResponse:
        assert url == ORIGIN + DIRECTORY_PATH
        return directory

    return await verify_wba_request(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        body=request.content,
        signer_keys=DiscoveredSignerKeys(fetcher=fetch),
        replay_store=replay,
    )


# -- The web-bot-auth sender --------------------------------------------------


def test_from_wba_reports_its_profile_algorithm_and_redacts_the_key() -> None:
    key = _key()
    sender = WebhookSender.from_wba(key, agent_origin="https://Seller.Example:443/")

    assert sender.signing_profile == WBA
    assert sender.signing_profiles == frozenset({WBA})
    assert sender.signing_algorithms == frozenset({"ed25519"})
    assert sender.signs_with_rfc9421 is True
    thumbprint = public_directory_key(key)["kid"]
    assert repr(sender) == (
        f"WebhookSender(auth=WbaSignerStrategy, key_id={thumbprint!r}, profiles=['web-bot-auth'])"
    )


@pytest.mark.parametrize(
    "key,origin,match",
    [
        (ec.generate_private_key(ec.SECP256R1()), ORIGIN, "Ed25519 keys only"),
        (_key(), "http://seller.example", "HTTPS"),
        (_key(), "https://user@seller.example", "HTTPS"),
    ],
)
def test_from_wba_rejects_keys_and_origins_the_profile_cannot_use(
    key: Any, origin: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        WebhookSender.from_wba(key, agent_origin=origin)


@pytest.mark.asyncio
async def test_each_attempt_is_signed_afresh_and_verifies_against_the_directory() -> None:
    """A receiver records nonces, including on an attempt it answered 503."""
    capture = _Capture(503)
    sender = WebhookSender.from_wba(
        _key(), agent_origin=ORIGIN, client=httpx.AsyncClient(transport=capture)
    )
    directory = sender.sign_web_bot_auth_directory()
    replay = InMemoryReplayStore()

    first = await sender.send_prepared(_prepared(WBA))
    retry = await sender.resend(first)

    assert (first.status_code, retry.status_code) == (503, 200)
    assert retry.signing_profile == WBA
    one, two = capture.requests
    assert one.content == two.content
    assert one.headers["signature-agent"] == f'sig1="{ORIGIN}"'
    assert _tag(one) == WBA
    assert one.headers["signature-input"].startswith(
        'sig1=("@method" "@target-uri" "@authority" "content-type" '
        '"content-digest" "signature-agent";key="sig1");created='
    )
    assert one.headers["signature"] != two.headers["signature"]
    # Both verify against one replay store: the retry is not a replayed nonce.
    for request in (one, two):
        (signer,) = await _verify(request, directory, replay)
        assert signer.agent_url == ORIGIN


@pytest.mark.asyncio
async def test_web_bot_auth_sender_refuses_registrations_for_the_adcp_profile() -> None:
    capture = _Capture()
    sender = WebhookSender.from_wba(
        _key(), agent_origin=ORIGIN, client=httpx.AsyncClient(transport=capture)
    )
    with pytest.raises(WebhookSigningProfileUnavailableError, match="adcp/webhook-signing/v1"):
        await sender.send_prepared(_prepared(ADCP))
    with pytest.raises(WebhookSigningProfileUnavailableError):
        sender.check_registration({"url": HOOK, "reporting_frequency": "daily"})
    assert (
        sender.check_registration(
            {"url": HOOK, "reporting_frequency": "daily", "signing_profile": WBA}
        )
        == WBA
    )
    assert capture.requests == []


@pytest.mark.asyncio
async def test_extra_headers_cannot_name_a_signature_agent() -> None:
    capture = _Capture()
    for sender in (
        WebhookSender.from_wba(
            _key(), agent_origin=ORIGIN, client=httpx.AsyncClient(transport=capture)
        ),
        WebhookSender.from_jwk(_WEBHOOK_JWK, client=httpx.AsyncClient(transport=capture)),
    ):
        prepared = replace(_prepared(), extra_headers={"Signature-Agent": 'sig1="https://x"'})
        with pytest.raises(ValueError, match="extra_headers may not override"):
            await sender.send_prepared(prepared)
    assert capture.requests == []


# -- A sender holding both profiles -------------------------------------------


@pytest.mark.asyncio
async def test_dual_sender_signs_each_delivery_under_its_registrations_profile() -> None:
    capture = _Capture()
    adcp_only = WebhookSender.from_jwk(_WEBHOOK_JWK, client=httpx.AsyncClient(transport=capture))
    sender = adcp_only.with_web_bot_auth(_key(), agent_origin=ORIGIN)

    assert sender.signing_profile == ADCP
    assert sender.signing_profiles == frozenset({ADCP, WBA})
    assert sender.signing_algorithms == frozenset({"ed25519"})
    assert adcp_only.signing_profiles == frozenset({ADCP})

    await sender.send_prepared(_prepared(WBA))
    await sender.send_prepared(_prepared(ADCP))
    default = await sender.send_prepared(_prepared(None))
    await sender.resend(default)

    wba, adcp, unbound, replayed = capture.requests
    assert _tag(wba) == WBA and "signature-agent" in wba.headers
    for request in (adcp, unbound, replayed):
        assert _tag(request) == ADCP
        assert "signature-agent" not in request.headers


def test_with_web_bot_auth_extends_only_an_adcp_sender_once() -> None:
    dual = WebhookSender.from_jwk(_WEBHOOK_JWK).with_web_bot_auth(_key(), agent_origin=ORIGIN)
    with pytest.raises(ValueError, match="already signs under web-bot-auth"):
        dual.with_web_bot_auth(_key(), agent_origin=ORIGIN)
    with pytest.raises(ValueError, match="already signs under web-bot-auth"):
        WebhookSender.from_wba(_key(), agent_origin=ORIGIN).with_web_bot_auth(
            _key(), agent_origin=ORIGIN
        )
    with pytest.raises(ValueError, match="extends an adcp/webhook-signing/v1 sender"):
        WebhookSender.from_bearer_token("token").with_web_bot_auth(_key(), agent_origin=ORIGIN)


@pytest.mark.asyncio
async def test_resend_keeps_the_profile_the_original_was_signed_under() -> None:
    capture = _Capture()
    sender = WebhookSender.from_jwk(_WEBHOOK_JWK, client=httpx.AsyncClient(transport=capture))
    signed_wba = WebhookDeliveryResult(
        status_code=503,
        idempotency_key="whk_1234567890123456",
        url=HOOK,
        response_headers={},
        response_body=b"",
        sent_body=_prepared().body,
        signing_profile=WBA,
    )
    with pytest.raises(WebhookSigningProfileUnavailableError):
        await sender.resend(signed_wba)
    assert capture.requests == []


# -- Key directory ------------------------------------------------------------


def test_directory_lists_the_sender_key_and_the_origins_other_keys() -> None:
    key, request_key = _key(), _key()
    sender = WebhookSender.from_jwk(_WEBHOOK_JWK).with_web_bot_auth(key, agent_origin=ORIGIN)

    directory = sender.sign_web_bot_auth_directory(other_keys=[request_key])

    keys = verify_directory_response(ORIGIN, directory, now=time.time())
    assert set(keys) == {
        public_directory_key(key)["kid"],
        public_directory_key(request_key)["kid"],
    }


def test_directory_needs_a_web_bot_auth_key() -> None:
    with pytest.raises(ValueError, match="no web-bot-auth key"):
        WebhookSender.from_jwk(_WEBHOOK_JWK).sign_web_bot_auth_directory()


# -- Boot validation against the advertised capability ------------------------


class _Caps:
    def __init__(self, webhook_signing: WebhookSigning) -> None:
        self.webhook_signing = webhook_signing
        self.webhook_signing_managed_externally = False


def _boot(sender: WebhookSender, **signing: Any) -> AdcpError:
    with pytest.raises(AdcpError) as caught:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(
                WebhookSigning(supported=True, delivery_retry_horizon_seconds=86400, **signing)
            ),
            sender=sender,
            supervisor=None,
        )
    return caught.value


def test_boot_refuses_an_advertised_profile_the_sender_has_no_key_for() -> None:
    error = _boot(WebhookSender.from_jwk(_WEBHOOK_JWK), profiles=[ADCP, WBA])
    assert error.details["missing"] == "webhook_signing_profile_key"
    assert error.details["sender_profiles"] == [ADCP]


def test_boot_refuses_a_web_bot_auth_only_sender_when_profiles_are_omitted() -> None:
    # Omitted profiles mean adcp/webhook-signing/v1, which this sender cannot sign.
    error = _boot(WebhookSender.from_wba(_key(), agent_origin=ORIGIN), algorithms=["ed25519"])
    assert error.details["missing"] == "webhook_signing_profile_key"
    assert error.details["advertised_profiles"] == [ADCP]


def test_boot_accepts_a_web_bot_auth_sender_for_an_advertised_profile() -> None:
    sender = WebhookSender.from_jwk(_WEBHOOK_JWK).with_web_bot_auth(_key(), agent_origin=ORIGIN)
    error = _boot(sender, profiles=[ADCP, WBA], algorithms=["ed25519"])
    # Past every sender check: what remains is the missing durable outbox.
    assert error.details["missing"] == "external_durable_webhook_outbox"


def test_boot_checks_every_algorithm_a_dual_sender_signs_with() -> None:
    es256_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "kid": "es256-webhook",
        "alg": "ES256",
        "adcp_use": "webhook-signing",
    }
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.private_numbers()

    def b64(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    es256_jwk |= {
        "x": b64(numbers.public_numbers.x),
        "y": b64(numbers.public_numbers.y),
        "d": b64(numbers.private_value),
    }
    sender = WebhookSender.from_jwk(es256_jwk).with_web_bot_auth(_key(), agent_origin=ORIGIN)
    assert sender.signing_algorithms == frozenset({"ecdsa-p256-sha256", "ed25519"})
    error = _boot(sender, algorithms=["ecdsa-p256-sha256"])
    assert error.details["missing"] == "webhook_signing_algorithm_alignment"
    assert error.details["sender_alg"] == "ed25519"


# -- Task push registrations ---------------------------------------------------


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"url": HOOK}, None),
        ({"url": HOOK, "signing_profile": WBA}, WBA),
        ({"url": HOOK, "signing_profile": ADCP}, ADCP),
    ],
)
def test_push_signing_profile_is_read_from_the_registration(
    config: dict[str, Any], expected: str | None
) -> None:
    assert _extract_push_signing_profile({"push_notification_config": config}) == expected


@pytest.mark.parametrize(
    "config,match",
    [
        ({"url": HOOK, "signing_profile": "custom"}, "unknown"),
        (
            {
                "url": HOOK,
                "signing_profile": WBA,
                "authentication": {"schemes": ["Bearer"], "credentials": "x" * 32},
            },
            "must not combine",
        ),
    ],
)
def test_malformed_push_signing_profile_is_refused(config: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _extract_push_signing_profile({"push_notification_config": config})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config,refused",
    [
        ({"url": HOOK, "signing_profile": WBA}, True),
        ({"url": HOOK}, False),
        ({"url": HOOK, "signing_profile": "custom"}, True),
    ],
)
async def test_dispatch_refuses_a_profile_the_outbox_cannot_sign(
    config: dict[str, Any], refused: bool
) -> None:
    from adcp.decisioning.dispatch import _push_signing_profile

    registry = MagicMock()
    registry.task_webhook_outbox.supports_signing_profile = lambda profile: profile == ADCP
    on_failure = AsyncMock()
    params = {"push_notification_config": config}
    if refused:
        with pytest.raises(AdcpError) as caught:
            await _push_signing_profile(
                params, registry, method_name="create_media_buy", on_failure=on_failure
            )
        assert caught.value.code == "INVALID_REQUEST"
        assert caught.value.field == "push_notification_config.signing_profile"
        on_failure.assert_awaited_once()
    else:
        assert (
            await _push_signing_profile(
                params, registry, method_name="create_media_buy", on_failure=on_failure
            )
            is None
        )
        on_failure.assert_not_awaited()


# -- Task webhook outbox -------------------------------------------------------


def _task_outbox(sender: Any) -> Any:
    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

    with patch("adcp.decisioning.pg.task_webhook_outbox.PG_AVAILABLE", True):
        return PgTaskWebhookOutbox(
            pool=MagicMock(),
            sender=sender,
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=86_400,
        )


def test_task_outbox_accepts_a_web_bot_auth_sender_and_reports_its_profiles() -> None:
    outbox = _task_outbox(WebhookSender.from_wba(_key(), agent_origin=ORIGIN))
    assert outbox.supports_signing_profile(WBA) is True
    assert outbox.supports_signing_profile(ADCP) is False


def test_task_registration_round_trips_its_signing_profile() -> None:
    outbox = _task_outbox(WebhookSender.from_jwk(_WEBHOOK_JWK))
    encrypted, nonce = outbox.protect_registration(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        url=HOOK,
        operation_id="op_1",
        token=None,
        signing_profile=WBA,
    )
    opened = outbox._open_registration_with_scope(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        encrypted_registration=encrypted,
        nonce=nonce,
    )
    assert opened == (HOOK, "op_1", None, None, None, WBA)


@pytest.mark.parametrize(
    "authentication,profile,match",
    [
        (TaskWebhookAuthentication(scheme="Bearer", credentials="x" * 32), WBA, "must not carry"),
        (None, "custom", "unknown webhook signing_profile"),
    ],
)
def test_task_registration_refuses_an_invalid_signing_profile(
    authentication: Any, profile: str, match: str
) -> None:
    outbox = _task_outbox(WebhookSender.from_jwk(_WEBHOOK_JWK))
    with pytest.raises(ValueError, match=match):
        outbox.validate_registration(HOOK, authentication, signing_profile=profile)


@pytest.mark.parametrize(
    "stored_profile,required",
    [(WBA, WBA), (ADCP, ADCP), (None, ADCP)],
)
def test_task_delivery_envelope_carries_the_profile_without_signatures(
    stored_profile: str | None, required: str
) -> None:
    outbox = _task_outbox(WebhookSender.from_jwk(_WEBHOOK_JWK))
    body = _prepared().body
    protected = outbox._protect_delivery_body(body, None, stored_profile)
    if stored_profile != WBA:
        # The default, named or not, stays a bare body an earlier SDK can send.
        assert protected == body
    opened_body, authentication, opened_profile = outbox._open_delivery_body(protected)
    assert (opened_body, authentication) == (body, None)
    assert opened_profile == (WBA if stored_profile == WBA else None)
    assert b"signature" not in protected.lower()
    assert (opened_profile or ADCP) == required


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_profile,expected", [(WBA, WBA), (None, ADCP)])
async def test_task_outbox_sends_each_attempt_under_the_stored_profile(
    stored_profile: str | None, expected: str
) -> None:
    sender = MagicMock(spec=WebhookSender)
    sender.signs_with_rfc9421 = True
    sender._owns_client = True
    sender._allow_private_destinations = False
    sender._timeout = 10.0
    sender.signing_algorithms = frozenset({"ed25519"})
    sender.send_prepared = AsyncMock(
        return_value=WebhookDeliveryResult(
            status_code=200,
            idempotency_key="k",
            url=HOOK,
            response_headers={},
            response_body=b"",
        )
    )
    outbox = _task_outbox(sender)
    prepared = _prepared()
    nonce = b"n" * 12
    encrypted = outbox._cipher.encrypt(
        nonce,
        outbox._protect_delivery_body(prepared.body, None, stored_profile),
        outbox._envelope_aad(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            status="completed",
            url=HOOK,
            operation_id="op_1",
            idempotency_key=prepared.idempotency_key,
        ),
    )
    row = (
        7,
        "acct_1",
        "task_1",
        "create_media_buy",
        "completed",
        HOOK,
        "op_1",
        prepared.idempotency_key,
        None,
        encrypted,
        nonce,
        1,
    )

    def connection(fetch: Any = None) -> Any:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone = AsyncMock(return_value=fetch)
        conn.execute = AsyncMock(return_value=cursor)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    contexts = iter([connection(row), connection()])
    outbox._pool = MagicMock()
    outbox._pool.connection = lambda: next(contexts)

    assert await outbox.process_one() is True
    sent = sender.send_prepared.await_args.args[0]
    assert sent.required_signing_profile == expected
    assert sent.body == prepared.body
    assert sent.extra_headers == {}


# -- Notification outbox -------------------------------------------------------


def test_notification_outbox_stores_the_required_profile_and_reserves_signature_agent() -> None:
    pytest.importorskip("psycopg_pool", reason="constructor checks require adcp[pg]")
    from adcp import PgNotificationOutbox

    outbox = PgNotificationOutbox(
        pool=MagicMock(),
        sender=WebhookSender.from_wba(_key(), agent_origin=ORIGIN),
        encryption_key=b"x" * 32,
        delivery_retry_horizon_seconds=86400,
    )
    raw = WebhookSender.prepare_raw(url=HOOK, payload={"notification_type": "extension.changed"})
    prepared = replace(raw, required_signing_profile=WBA)
    params, _scope = outbox._prepare_insert(
        prepared,
        notification_type="extension.changed",
        caller_scope_id="caller",
        account_id=None,
        signing_scope_id=None,
    )
    kind, caller, account, url, key, signing_scope, key_id, scope, encrypted, nonce = params[:10]
    row = (1, account, caller, kind, url, key, signing_scope, key_id, scope, encrypted, nonce)
    assert outbox._open(row) == prepared
    # Version 2 makes an earlier SDK quarantine the row instead of ignoring
    # the profile; rows without one keep version 1.
    assert json.loads(PgNotificationOutbox._protected(prepared))["version"] == 2
    assert json.loads(PgNotificationOutbox._protected(raw))["version"] == 1
    # The default profile stays readable to an earlier SDK, which signs only it.
    default = replace(raw, required_signing_profile=ADCP)
    assert json.loads(PgNotificationOutbox._protected(default))["version"] == 1
    params, _scope = outbox._prepare_insert(
        default,
        notification_type="extension.changed",
        caller_scope_id="caller",
        account_id=None,
        signing_scope_id=None,
    )
    kind, caller, account, url, key, signing_scope, key_id, scope, encrypted, nonce = params[:10]
    row = (1, account, caller, kind, url, key, signing_scope, key_id, scope, encrypted, nonce)
    assert outbox._open(row) == default

    with pytest.raises(ValueError):
        PgNotificationOutbox._protected(
            replace(raw, extra_headers={"Signature-Agent": 'sig1="https://x"'})
        )


# -- Advertised profiles, checked for every outbox ------------------------------


@pytest.mark.parametrize(
    "webhook_signing,expected",
    [
        (None, None),
        (WebhookSigning(supported=False), None),
        (WebhookSigning(supported=True), frozenset({ADCP})),
        (WebhookSigning(supported=True, profiles=[ADCP, WBA]), frozenset({ADCP, WBA})),
    ],
)
def test_advertised_profiles_default_to_the_adcp_profile(
    webhook_signing: WebhookSigning | None, expected: frozenset[str] | None
) -> None:
    from adcp.decisioning.webhook_emit import advertised_webhook_signing_profiles

    caps = SimpleNamespace(webhook_signing=webhook_signing)
    assert advertised_webhook_signing_profiles(caps) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config,advertised,refused",
    [
        ({"url": HOOK, "signing_profile": WBA}, frozenset({ADCP}), True),
        ({"url": HOOK, "signing_profile": WBA}, frozenset({ADCP, WBA}), False),
        # The implicit default must be advertised too.
        ({"url": HOOK}, frozenset({WBA}), True),
        # Legacy authentication selects no signing profile.
        (
            {"url": HOOK, "authentication": {"schemes": ["Bearer"], "credentials": "x" * 32}},
            frozenset({WBA}),
            False,
        ),
        ({"url": HOOK, "signing_profile": WBA}, None, False),
    ],
)
async def test_resolver_outbox_registrations_are_checked_against_advertised_profiles(
    config: dict[str, Any], advertised: frozenset[str] | None, refused: bool
) -> None:
    """A sender_resolver outbox cannot vouch for a profile, so the capabilities decide."""
    from adcp.decisioning.dispatch import _push_signing_profile

    registry = MagicMock()
    registry.task_webhook_outbox.supports_signing_profile = lambda profile: None
    params = {"push_notification_config": config}
    call = _push_signing_profile(
        params,
        registry,
        method_name="create_media_buy",
        on_failure=None,
        advertised_profiles=advertised,
    )
    if refused:
        with pytest.raises(AdcpError) as caught:
            await call
        assert caught.value.field == "push_notification_config.signing_profile"
    else:
        await call
