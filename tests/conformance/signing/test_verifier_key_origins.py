"""Verifier integration of the ``identity.key_origins`` consistency check.

Behavior under test (matches ADCP request-signing spec #3690 step 7):

* Brand-json-sourced resolver + matching declaration → success.
* Brand-json-sourced resolver + mismatched declaration → raises
  ``request_signature_key_origin_mismatch``.
* Brand-json-sourced resolver + missing declaration → raises
  ``request_signature_key_origin_missing``.
* Publisher-pin-sourced resolver (and resolvers without a
  ``jwks_source`` attribute) skip the check entirely — a mismatched
  declaration does NOT raise.
* Pre-existing verifier codes (``key_unknown``, etc.) still surface
  unchanged when the prior step rejects.

The check is wired only when ``VerifyOptions.expected_key_origins`` is
supplied; adopters who don't yet plumb capabilities through the
verifier see no behavior change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest

from adcp.signing import (
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING,
    REQUEST_SIGNATURE_KEY_UNKNOWN,
    SignatureVerificationError,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    sign_request,
    verify_request_signature,
)
from adcp.signing.crypto import private_key_from_jwk

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")


class _BrandJsonStaticResolver:
    """Test double: a JWKS resolver that advertises brand-json sourcing.

    Combines :class:`StaticJwksResolver`'s in-memory keys with the
    ``jwks_source``/``jwks_uri`` discriminant the verifier reads to
    decide whether to engage the key-origin consistency check. Lets
    these tests exercise the verifier-integration seam without
    standing up a real brand.json walk.
    """

    jwks_source: ClassVar[Literal["brand_json"]] = "brand_json"

    def __init__(self, jwks: dict[str, Any], *, jwks_uri: str) -> None:
        self._inner = StaticJwksResolver(jwks)
        self.jwks_uri = jwks_uri

    def __call__(self, keyid: str) -> dict[str, Any] | None:
        return self._inner(keyid)


class _PublisherPinStaticResolver:
    """Test double: a JWKS resolver that advertises publisher-pin sourcing.

    The publisher-pin source skips the consistency check per spec —
    the JWKS origin is the publisher's domain by design, not the
    operator's.
    """

    jwks_source: ClassVar[Literal["publisher_pin"]] = "publisher_pin"

    def __init__(self, jwks: dict[str, Any], *, jwks_uri: str) -> None:
        self._inner = StaticJwksResolver(jwks)
        self.jwks_uri = jwks_uri

    def __call__(self, keyid: str) -> dict[str, Any] | None:
        return self._inner(keyid)


def _sign_basic(
    *,
    method: str = "POST",
    url: str = "https://seller.example.com/adcp/create_media_buy",
    body: bytes = b"{}",
    created: int = 1776520800,
) -> tuple[dict[str, str], bytes]:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    headers = {"Content-Type": "application/json"}
    signed = sign_request(
        method=method,
        url=url,
        headers=headers,
        body=body,
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        created=created,
        signing_profile_version="3.2",
    )
    return {**headers, **signed.as_dict()}, body


def _options_with(
    resolver: Any,
    *,
    expected_key_origins: dict[str, str] | None = None,
    signing_purpose: str = "request_signing",
    posture: str | None = None,
) -> VerifyOptions:
    return VerifyOptions(
        now=1776520800.0,
        capability=VerifierCapability(covers_content_digest="either"),
        operation="create_media_buy",
        jwks_resolver=resolver,
        expected_key_origins=expected_key_origins,
        signing_purpose=signing_purpose,
        posture=posture,
    )


# ----- brand-json source: check engages -----


def test_brand_json_source_matching_origin_passes() -> None:
    """Resolver advertises brand-json source AND declaration matches
    resolved jwks_uri host → verifier passes (no raise from the
    key-origin check)."""
    headers, body = _sign_basic()
    resolver = _BrandJsonStaticResolver(
        {"keys": [ED25519_KEY]},
        jwks_uri="https://keys.brand.example/.well-known/jwks.json",
    )
    options = _options_with(
        resolver,
        expected_key_origins={"request_signing": "https://keys.brand.example"},
    )
    # Verification succeeds — both the signature and the key-origin
    # consistency check pass.
    verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=options,
    )


def test_brand_json_source_mismatched_origin_raises_mismatch_code() -> None:
    """Resolver advertises brand-json source AND declaration disagrees
    with resolved jwks_uri host → verifier raises
    ``request_signature_key_origin_mismatch``."""
    headers, body = _sign_basic()
    resolver = _BrandJsonStaticResolver(
        {"keys": [ED25519_KEY]},
        # JWKS resolved at attacker-controlled host.
        jwks_uri="https://attacker.example.org/.well-known/jwks.json",
    )
    options = _options_with(
        resolver,
        # Capabilities declare the legitimate host.
        expected_key_origins={"request_signing": "https://keys.brand.example"},
    )
    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=body,
            options=options,
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH
    detail = exc_info.value.detail
    assert detail is not None
    assert detail["purpose"] == "request_signing"
    assert detail["actual_origin"] == "attacker.example.org"
    assert detail["expected_origin"] == "keys.brand.example"


def test_brand_json_source_missing_declaration_raises_missing_code() -> None:
    """Resolver advertises brand-json source AND
    ``identity.key_origins`` carries no entry for the purpose →
    verifier raises ``request_signature_key_origin_missing``."""
    headers, body = _sign_basic()
    resolver = _BrandJsonStaticResolver(
        {"keys": [ED25519_KEY]},
        jwks_uri="https://keys.brand.example/.well-known/jwks.json",
    )
    options = _options_with(
        resolver,
        # Capabilities map doesn't carry ``request_signing``.
        expected_key_origins={"webhook_signing": "https://keys.brand.example"},
        posture="required",
    )
    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=body,
            options=options,
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISSING
    detail = exc_info.value.detail
    assert detail is not None
    assert detail["purpose"] == "request_signing"
    assert detail["posture"] == "required"


# ----- publisher-pin source: check skips -----


def test_publisher_pin_source_mismatched_origin_does_not_raise() -> None:
    """Resolver advertises publisher-pin source → verifier skips the
    key-origin check entirely. A mismatched declaration is NOT
    grounds for rejection because publisher-pinned JWKS hosts are
    expected to differ from the operator origin per spec.
    """
    headers, body = _sign_basic()
    resolver = _PublisherPinStaticResolver(
        {"keys": [ED25519_KEY]},
        jwks_uri="https://publisher.example.com/.well-known/jwks.json",
    )
    options = _options_with(
        resolver,
        # Operator-side declaration deliberately disagrees — the check
        # must skip and not raise.
        expected_key_origins={"request_signing": "https://keys.brand.example"},
    )
    verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=options,
    )


def test_publisher_pin_source_missing_declaration_does_not_raise() -> None:
    """Publisher-pin source + absent declaration → no raise. The
    skip is unconditional regardless of declaration presence."""
    headers, body = _sign_basic()
    resolver = _PublisherPinStaticResolver(
        {"keys": [ED25519_KEY]},
        jwks_uri="https://publisher.example.com/.well-known/jwks.json",
    )
    options = _options_with(
        resolver,
        expected_key_origins={"webhook_signing": "https://keys.brand.example"},
    )
    verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=options,
    )


# ----- legacy resolvers (no jwks_source attribute) -----


def test_resolver_without_source_attribute_skips_check() -> None:
    """Adopter-supplied :class:`JwksResolver` that predates the
    ``jwks_source`` attribute → verifier skips the check (treats
    absence as ``publisher_pin``-equivalent). This preserves
    backwards compatibility with the Protocol — adopters who haven't
    rebuilt their resolvers against the new contract keep working.
    """
    headers, body = _sign_basic()
    # Plain StaticJwksResolver carries no ``jwks_source`` attribute.
    resolver = StaticJwksResolver({"keys": [ED25519_KEY]})
    options = _options_with(
        resolver,
        # Declaration is provided but resolver doesn't advertise
        # brand-json sourcing — check skips.
        expected_key_origins={"request_signing": "https://keys.brand.example"},
    )
    verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=options,
    )


# ----- check does not fire without expected_key_origins -----


def test_brand_json_source_skips_when_no_capabilities_map_supplied() -> None:
    headers, body = _sign_basic()
    resolver = _BrandJsonStaticResolver(
        # Even with a mismatched jwks_uri, the check skips when the
        # capabilities-derived expected_key_origins is None.
        {"keys": [ED25519_KEY]},
        jwks_uri="https://different.example/.well-known/jwks.json",
    )
    options = _options_with(resolver, expected_key_origins=None)
    signer = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=options,
    )
    assert signer.key_id


# ----- earlier failure codes still surface -----


def test_key_unknown_still_surfaces_before_origin_check() -> None:
    """A keyid the resolver can't resolve raises ``key_unknown`` —
    the verifier short-circuits before the key-origin check, so the
    pre-existing code is preserved. Important: the key-origin wiring
    must NOT change the order of operations in the checklist; the
    spec mandates ``key_unknown`` is the rejection for a missing
    JWK, even when a key-origin declaration is configured.
    """
    headers, body = _sign_basic()
    # Brand-json-sourced resolver with EMPTY key set — every kid
    # returns None.
    resolver = _BrandJsonStaticResolver(
        {"keys": []},
        jwks_uri="https://keys.brand.example/.well-known/jwks.json",
    )
    options = _options_with(
        resolver,
        expected_key_origins={"request_signing": "https://keys.brand.example"},
    )
    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=body,
            options=options,
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_UNKNOWN


# ----- BrandJsonJwksResolver advertises the discriminant -----


def test_brand_json_jwks_resolver_advertises_source() -> None:
    """The production :class:`BrandJsonJwksResolver` advertises
    ``jwks_source = "brand_json"`` at the class level — instances
    surface it without needing a refresh cycle to populate it.
    """
    from adcp.signing.brand_jwks import BrandJsonJwksResolver

    assert BrandJsonJwksResolver.jwks_source == "brand_json"
