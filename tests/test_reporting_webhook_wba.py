"""Reporting notifications signed under ``web-bot-auth`` when a subscription asks (#1859).

A reporting subscription without legacy ``authentication`` records the
registration's ``signing_profile``. The worker signs each attempt with a fresh
``from_wba`` sender for ``web-bot-auth`` subscriptions, using the Web Bot Auth
key the trusted signing resolver returns, and leaves every subscription and
delivery stored before the field byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.models import ledger_asdict
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox import (
    ReportingLegacyAuthentication,
    ReportingNotificationSubscription,
    ReportingNotificationWorker,
    ReportingSigningMaterial,
    ReportingWebBotAuthKey,
)
from adcp.reporting.outbox.routing import _decode_subscription
from adcp.reporting.production.notifications import (
    ReportingProductionSigning,
    _SignedSubscriptions,
)
from adcp.webhook_sender import ScopePermanentlyUnknown
from adcp.webhooks import WEBHOOK_SIGNING_PROFILE_ADCP, WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH

ORIGIN = "https://seller.example"
WBA = WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH
ADCP = WEBHOOK_SIGNING_PROFILE_ADCP


def _subscription(**changes: Any) -> ReportingNotificationSubscription:
    values: dict[str, Any] = dict(
        account_id="acct_a",
        subscriber_id="buyer",
        principal_id="buyer",
        url="https://receiver.example.test/reporting",
        event_types=("reporting.ledger_changed",),
        configuration_revision="registration-1",
        authorization_ref="authorized-principal-1",
        proof_of_control_ref="account-challenge-1",
        signing_scope_id="seller-signing-scope-1",
        active=True,
        authorized=True,
        proof_valid=True,
    )
    values.update(changes)
    return ReportingNotificationSubscription(**values)


def _material(*, web_bot_auth: bool = True, algorithms: frozenset[str] = frozenset({"ed25519"})):
    return ReportingSigningMaterial(
        ed25519.Ed25519PrivateKey.generate(),
        "https://seller.example/keys#webhook-1",
        "ed25519",
        algorithms,
        web_bot_auth=(
            ReportingWebBotAuthKey(ed25519.Ed25519PrivateKey.generate(), ORIGIN)
            if web_bot_auth
            else None
        ),
    )


# -- Subscriptions -------------------------------------------------------------


def test_a_subscription_without_a_profile_keeps_its_stored_fingerprint() -> None:
    subscription = _subscription()
    pre_field = {k: v for k, v in asdict(subscription).items() if k != "signing_profile"}

    assert "signing_profile" not in ledger_asdict(subscription)
    assert subscription.fingerprint == hashlib.sha256(canonical_json_utf8_v1(pre_field)).hexdigest()
    # Envelopes stored before the field decode exactly as before.
    assert _decode_subscription(json.loads(canonical_json_utf8_v1(pre_field))) == subscription


def test_a_web_bot_auth_subscription_binds_its_profile() -> None:
    subscription = _subscription(signing_profile=WBA)
    stored = json.loads(canonical_json_utf8_v1(ledger_asdict(subscription)))

    assert stored["signing_profile"] == WBA
    assert _decode_subscription(stored) == subscription
    assert subscription.fingerprint != _subscription().fingerprint
    assert subscription.auth_mode == "rfc9421"


@pytest.mark.parametrize(
    "changes",
    [
        {"signing_profile": "custom"},
        {
            "signing_profile": WBA,
            "signing_scope_id": None,
            "authentication": ReportingLegacyAuthentication("Bearer", "x" * 32),
        },
    ],
)
def test_an_invalid_subscription_profile_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ReportingNotificationError, match="invalid_configuration"):
        _subscription(**changes)


# -- Signing material ----------------------------------------------------------


@pytest.mark.parametrize(
    "key,origin",
    [
        (ec.generate_private_key(ec.SECP256R1()), ORIGIN),
        (ed25519.Ed25519PrivateKey.generate(), "http://seller.example"),
    ],
)
def test_web_bot_auth_key_must_be_ed25519_on_an_https_origin(key: Any, origin: str) -> None:
    with pytest.raises(ReportingNotificationError, match="permanent_scope"):
        ReportingWebBotAuthKey(key, origin)


def test_web_bot_auth_material_requires_ed25519_to_be_advertised() -> None:
    p256 = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ReportingNotificationError, match="permanent_scope"):
        ReportingSigningMaterial(
            p256,
            "kid",
            "ecdsa-p256-sha256",
            frozenset({"ecdsa-p256-sha256"}),
            web_bot_auth=ReportingWebBotAuthKey(ed25519.Ed25519PrivateKey.generate(), ORIGIN),
        )


# -- The worker's sender -------------------------------------------------------


async def _sender(subscription: ReportingNotificationSubscription, material: Any) -> Any:
    class Signing:
        async def resolve(self, **_: Any) -> Any:
            return material

    worker = SimpleNamespace(signing=Signing(), lease_seconds=30)
    return await ReportingNotificationWorker._sender(worker, subscription)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_worker_signs_a_web_bot_auth_subscription_with_the_origin_key() -> None:
    material = _material()
    sender = await _sender(_subscription(signing_profile=WBA), material)

    assert sender.signing_profile == WBA
    assert sender.signing_profiles == frozenset({WBA})
    assert sender._allowed_destination_ports == frozenset({443})
    directory = sender.sign_web_bot_auth_directory()
    assert directory.headers["Content-Type"] == "application/http-message-signatures-directory+json"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, ADCP])
async def test_worker_keeps_adcp_subscriptions_on_the_adcp_key(profile: str | None) -> None:
    sender = await _sender(_subscription(signing_profile=profile), _material())
    assert sender.signing_profiles == frozenset({ADCP})


@pytest.mark.asyncio
async def test_worker_quarantines_a_web_bot_auth_subscription_without_a_key() -> None:
    with pytest.raises(ScopePermanentlyUnknown):
        await _sender(_subscription(signing_profile=WBA), _material(web_bot_auth=False))


# -- Production signing contract -----------------------------------------------


class _Resolver:
    def __init__(self, material: ReportingSigningMaterial) -> None:
        self.material = material

    async def resolve(self, **_: Any) -> ReportingSigningMaterial:
        return self.material


def _production(material: ReportingSigningMaterial, **kwargs: Any) -> ReportingProductionSigning:
    return ReportingProductionSigning(
        _Resolver(material),
        ("ed25519",),
        brand_json_url="https://seller.example/brand.json",
        **kwargs,
    )


def test_production_signing_advertises_web_bot_auth_only_when_enabled() -> None:
    assert "profiles" not in _production(_material()).wire()
    assert _production(_material(), web_bot_auth=True).wire()["profiles"] == [ADCP, WBA]


def test_production_web_bot_auth_requires_ed25519() -> None:
    p256 = ec.generate_private_key(ec.SECP256R1())
    material = ReportingSigningMaterial(
        p256, "kid", "ecdsa-p256-sha256", frozenset({"ecdsa-p256-sha256"})
    )
    with pytest.raises(ReportingNotificationError, match="notification_signing_unready"):
        ReportingProductionSigning(
            _Resolver(material),
            ("ecdsa-p256-sha256",),
            brand_json_url="https://seller.example/brand.json",
            web_bot_auth=True,
        )


@pytest.mark.asyncio
async def test_production_web_bot_auth_requires_the_key_in_every_material() -> None:
    signing = _production(_material(web_bot_auth=False), web_bot_auth=True)
    with pytest.raises(ReportingNotificationError, match="notification_signing_unready"):
        await signing.resolve(account_id="acct_a", principal_id="buyer", signing_scope_id="s")
    assert (
        await _production(_material(), web_bot_auth=True).resolve(
            account_id="acct_a", principal_id="buyer", signing_scope_id="s"
        )
    ).web_bot_auth is not None


@pytest.mark.asyncio
async def test_production_skips_only_the_web_bot_auth_subscriptions_it_cannot_sign(
    caplog: pytest.LogCaptureFixture,
) -> None:
    wba = _subscription(subscriber_id="wba_buyer", signing_profile=WBA)
    adcp = _subscription(subscriber_id="adcp_buyer")

    class Subscriptions:
        async def list_active(self, **_: Any) -> tuple[ReportingNotificationSubscription, ...]:
            return (wba, adcp)

        async def get_active(self, **_: Any) -> ReportingNotificationSubscription:
            return wba

    query = {"account_id": "acct_a", "notification_type": "reporting.ledger_changed"}
    without = _SignedSubscriptions(Subscriptions())
    # One unsignable subscriber does not block the account's other subscribers,
    # and the skip is an error-level log naming it, for operators to alert on.
    with caplog.at_level(logging.ERROR, logger="adcp.reporting.production.notifications"):
        assert await without.list_active(**query) == (adcp,)
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert "wba_buyer" in record.getMessage() and "acct_a" in record.getMessage()
    assert await without.get_active(subscriber_id="wba_buyer", **query) is None

    enabled = _SignedSubscriptions(Subscriptions(), web_bot_auth=True)
    assert await enabled.list_active(**query) == (wba, adcp)
    assert await enabled.get_active(subscriber_id="wba_buyer", **query) == wba
