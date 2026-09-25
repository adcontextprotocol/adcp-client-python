"""Webhook-v1 emission stays distinct from request-signing profile 3.2."""

import base64
import hashlib
import json
import re
from importlib.resources import files

import pytest

from adcp.signing import private_key_from_jwk, sign_request
from adcp.webhooks import WebhookVerifyOptions, sign_webhook, verify_webhook_signature

KEYS = json.loads(
    files("adcp")
    .joinpath("_compliance/3.2.0-rc.6/test-vectors/webhook-signing/keys.json")
    .read_text()
)["keys"]
ALGORITHMS = [
    ("ed25519", "test-ed25519-webhook-2026"),
    ("ecdsa-p256-sha256", "test-es256-webhook-2026"),
]
BODY = '{\n "task_id": "raw-body", "text": "é", "status": "completed"\n}\n'.encode()
URL = "https://buyer.example.test/webhook"
NOW = 1776520800


@pytest.mark.parametrize("alg,kid", ALGORITHMS)
@pytest.mark.parametrize("label", ["sig1", "callback"])
def test_webhook_emits_unpadded_base64url_with_unchanged_digest(alg, kid, label):
    key = next(row for row in KEYS if row["kid"] == kid)
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers={"Content-Type": "application/json"},
        body=BODY,
        private_key=private_key_from_jwk(key, d_field="_private_d_for_test_only"),
        key_id=kid,
        alg=alg,
        created=NOW,
        nonce="emission-regression",
        label=label,
    )
    assert re.fullmatch(rf"{label}=:[A-Za-z0-9_-]+:", signed.signature)
    assert signed.content_digest == (
        "sha-256=:" + base64.b64encode(hashlib.sha256(BODY).digest()).decode() + ":"
    )
    public = {k: v for k, v in key.items() if not k.startswith("_")}
    verified = verify_webhook_signature(
        method="POST",
        url=URL,
        headers={"Content-Type": "application/json", **signed.as_dict()},
        body=BODY,
        options=WebhookVerifyOptions(
            jwks_resolver={kid: public}.get, clock=lambda: NOW, label=label
        ),
    )
    assert (verified.key_id, verified.alg, verified.label) == (kid, alg, label)


@pytest.mark.parametrize("alg,kid", ALGORITHMS)
@pytest.mark.parametrize("profile", ["3.0", "3.1", "3.2"])
def test_request_profile_encoding_is_independent(alg, kid, profile):
    key = next(row for row in KEYS if row["kid"] == kid)
    signed = sign_request(
        method="POST",
        url=URL,
        headers={"Content-Type": "application/json"},
        body=BODY,
        private_key=private_key_from_jwk(key, d_field="_private_d_for_test_only"),
        key_id=kid,
        alg=alg,
        created=NOW,
        nonce="request-emission-regression",
        signing_profile_version=profile,
        cover_content_digest=True,
    )
    pattern = r"sig1=:[A-Za-z0-9+/]+==:" if profile == "3.2" else r"sig1=:[A-Za-z0-9_-]+:"
    assert re.fullmatch(pattern, signed.signature)
    assert signed.content_digest == (
        "sha-256=:" + base64.b64encode(hashlib.sha256(BODY).digest()).decode() + ":"
    )
