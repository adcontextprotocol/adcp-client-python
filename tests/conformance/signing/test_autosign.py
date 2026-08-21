"""Unit tests for the autosign capability classifier + SigningConfig.

These test the client-side orchestration layer — no wire calls, no
verifier interaction. They establish the precedence rules documented in
the AdCP security profile (required_for > warn_for > supported_for) and
the invariants on SigningConfig.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.signing import SigningConfig, operation_needs_signing
from adcp.signing.autosign import signing_profile_for_adcp_version
from adcp.signing.crypto import ALG_ED25519, ALG_ES256
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    CoversContentDigest,
    RequestSigning,
)

# -- helpers ------------------------------------------------------------


def _cap(
    *,
    supported: bool = True,
    required: list[str] | None = None,
    warn: list[str] | None = None,
    supported_for: list[str] | None = None,
    covers: CoversContentDigest = CoversContentDigest.either,
) -> RequestSigning:
    return RequestSigning(
        supported=supported,
        covers_content_digest=covers,
        required_for=required or [],
        warn_for=warn or [],
        supported_for=supported_for,
    )


# -- operation_needs_signing --------------------------------------------


def test_skip_when_capability_absent() -> None:
    assert operation_needs_signing(None, "create_media_buy") == "skip"


def test_skip_when_capability_unsupported() -> None:
    cap = _cap(supported=False, required=["create_media_buy"])
    assert operation_needs_signing(cap, "create_media_buy") == "skip"


def test_required_when_op_in_required_for() -> None:
    cap = _cap(required=["create_media_buy"])
    assert operation_needs_signing(cap, "create_media_buy") == "required"


def test_optional_when_op_in_warn_for_only() -> None:
    cap = _cap(warn=["sync_creatives"])
    assert operation_needs_signing(cap, "sync_creatives") == "optional"


def test_optional_when_op_in_supported_for_only() -> None:
    cap = _cap(supported_for=["get_products"])
    assert operation_needs_signing(cap, "get_products") == "optional"


def test_skip_when_op_in_no_list() -> None:
    cap = _cap(required=["create_media_buy"])
    assert operation_needs_signing(cap, "get_products") == "skip"


def test_required_wins_over_warn() -> None:
    # required_for takes precedence even when the op also appears in warn_for.
    cap = _cap(required=["create_media_buy"], warn=["create_media_buy"])
    assert operation_needs_signing(cap, "create_media_buy") == "required"


def test_required_wins_over_supported() -> None:
    cap = _cap(
        required=["create_media_buy"],
        supported_for=["create_media_buy", "get_products"],
    )
    assert operation_needs_signing(cap, "create_media_buy") == "required"


def test_warn_and_supported_both_optional() -> None:
    # Spec allows supported_for to be a superset of warn_for.
    cap = _cap(warn=["sync_creatives"], supported_for=["sync_creatives"])
    assert operation_needs_signing(cap, "sync_creatives") == "optional"


def test_empty_lists_and_none_lists_equivalent() -> None:
    cap_empty = _cap(required=[], warn=[], supported_for=[])
    cap_none = RequestSigning(
        supported=True,
        covers_content_digest=CoversContentDigest.either,
        required_for=None,
        warn_for=None,
        supported_for=None,
    )
    assert operation_needs_signing(cap_empty, "anything") == "skip"
    assert operation_needs_signing(cap_none, "anything") == "skip"


# -- SigningConfig ------------------------------------------------------


def test_signing_config_accepts_ed25519_key() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    cfg = SigningConfig(private_key=key, key_id="buyer-1")
    assert cfg.alg == ALG_ED25519
    assert cfg.key_id == "buyer-1"
    assert cfg.private_key is key
    assert cfg.signing_profile_version is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [("3.0", "3.0"), ("3.1.15", "3.1"), ("3.2-beta.4", "3.2")],
)
def test_signing_profile_follows_release_line(version: str, expected: str) -> None:
    assert signing_profile_for_adcp_version(version) == expected


def test_signing_profile_rejects_unknown_release() -> None:
    with pytest.raises(ValueError, match="no supported request-signing profile"):
        signing_profile_for_adcp_version("4.0")


def test_signing_config_accepts_es256_key() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    cfg = SigningConfig(private_key=key, key_id="buyer-2", alg=ALG_ES256)
    assert cfg.alg == ALG_ES256
    assert cfg.key_id == "buyer-2"


def test_signing_config_rejects_empty_key_id() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    with pytest.raises(ValueError, match="key_id"):
        SigningConfig(private_key=key, key_id="")


def test_signing_config_rejects_unknown_alg() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    with pytest.raises(ValueError, match="alg"):
        SigningConfig(private_key=key, key_id="buyer-1", alg="hs256")


def test_signing_config_is_frozen() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    cfg = SigningConfig(private_key=key, key_id="buyer-1")
    with pytest.raises((AttributeError, Exception)):
        cfg.key_id = "mutated"  # type: ignore[misc]
