"""Unit tests for the signer — sign a request, then verify it round-trips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adcp.signing import (
    DEFAULT_TAG,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    private_key_from_jwk,
    sign_request,
    verify_request_signature,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")
ES256_KEY = next(k for k in KEYS if k["kid"] == "test-es256-2026")


def _verify_options(keys: list[dict]) -> VerifyOptions:
    return VerifyOptions(
        now=float(int(__import__("time").time())),
        capability=VerifierCapability(
            covers_content_digest="either",
            required_for=frozenset({"create_media_buy"}),
        ),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": keys}),
    )


def test_sign_then_verify_ed25519() -> None:
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")

    signed = sign_request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        signing_profile_version="3.2",
    )

    headers = {"Content-Type": "application/json", **signed.as_dict()}
    result = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=_verify_options([ED25519_KEY]),
    )
    assert result.key_id == "test-ed25519-2026"
    assert result.alg == "ed25519"


def test_sign_then_verify_es256() -> None:
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ES256_KEY, d_field="_private_d_for_test_only")

    signed = sign_request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id="test-es256-2026",
        alg="ecdsa-p256-sha256",
        signing_profile_version="3.2",
    )

    headers = {"Content-Type": "application/json", **signed.as_dict()}
    result = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=_verify_options([ES256_KEY]),
    )
    assert result.alg == "ecdsa-p256-sha256"


def test_sign_with_content_digest_then_verify() -> None:
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")

    signed = sign_request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        cover_content_digest=True,
        signing_profile_version="3.2",
    )
    assert signed.content_digest is not None

    headers = {"Content-Type": "application/json", **signed.as_dict()}
    options = VerifyOptions(
        now=float(int(__import__("time").time())),
        capability=VerifierCapability(
            covers_content_digest="required",
            required_for=frozenset({"create_media_buy"}),
        ),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
    )
    verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=options,
    )


def test_signed_input_uses_adcp_tag() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    signed = sign_request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        body=b"{}",
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        signing_profile_version="3.2",
    )
    assert f'tag="{DEFAULT_TAG}"' in signed.signature_input
    assert signed.signature.startswith("sig1=:") and signed.signature.endswith(":")


def test_low_level_signer_requires_profile() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(TypeError, match="signing_profile_version"):
        sign_request(  # type: ignore[call-arg]
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="test-ed25519-2026",
            alg="ed25519",
        )


def test_low_level_signer_rejects_unknown_profile() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="signing_profile_version"):
        sign_request(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="test-ed25519-2026",
            alg="ed25519",
            signing_profile_version="3.3",  # type: ignore[arg-type]
        )


def test_32_nonempty_body_covers_digest_by_default() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    signed = sign_request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={},
        body=b"{}",
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        signing_profile_version="3.2",
    )
    assert signed.content_digest is not None
    assert '"content-digest"' in signed.signature_input


def test_32_nonempty_body_rejects_disabled_digest() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="must cover content-digest"):
        sign_request(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="test-ed25519-2026",
            alg="ed25519",
            cover_content_digest=False,
            signing_profile_version="3.2",
        )
