"""Tests for #384 — webhook_signing.supported boot validator.

Covers the two acceptance criteria the issue tracks here:

* AC4 — outbound webhooks delivered by an RFC 9421 sender carry the
  ``Signature`` and ``Signature-Input`` headers conformant verifiers
  gate on.
* AC5 — server boot fails when ``capabilities.webhook_signing.supported``
  is ``True`` but no JWK-signing sender is wired.

Other auth-mode senders (bearer, AdCP-legacy HMAC, Standard-Webhooks
HMAC) MUST trip the same boot gate — capabilities advertise RFC 9421,
the wired sender does not produce ``Signature`` / ``Signature-Input``,
buyers enforcing RFC 9421 see silent blackout.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from adcp.decisioning.capabilities import WebhookSigning
from adcp.decisioning.types import AdcpError
from adcp.decisioning.webhook_emit import validate_webhook_signing_for_capabilities
from adcp.webhook_sender import WebhookSender
from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor

VECTORS_DIR = Path(__file__).parent / "conformance" / "vectors" / "request-signing"
_KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
_REQUEST_ED25519 = next(k for k in _KEYS if k["kid"] == "test-ed25519-2026")

# Clone the request-signing fixture into a webhook-signing JWK. The
# from_jwk constructor rejects keys whose adcp_use is not exactly
# "webhook-signing" — separation of webhook-signing and request-signing
# key material is part of the AdCP security posture.
_WEBHOOK_JWK = {
    **copy.deepcopy(_REQUEST_ED25519),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}


def _jwk_with_private() -> dict:
    return {**_WEBHOOK_JWK, "d": _WEBHOOK_JWK["_private_d_for_test_only"]}


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Records the request the sender would have emitted, returns 200."""

    def __init__(self) -> None:
        self.captured: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.captured = request
        return httpx.Response(200, content=b"{}", request=request)


# ----- AC4: outbound webhooks carry RFC 9421 headers -----


@pytest.mark.asyncio
async def test_outbound_webhook_carries_rfc9421_signature_headers() -> None:
    """A JWK-signing sender MUST attach ``Signature`` and ``Signature-Input``
    on every outbound POST. Without these headers, any buyer running an
    RFC 9421 verifier (the AdCP-conformant posture) rejects the delivery.
    """
    transport = _CapturingTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_jwk(_jwk_with_private(), client=client)

    async with sender:
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_ac4",
            task_type="create_media_buy",
            status="completed",
            result={"media_buy_id": "mb_1"},
        )

    assert result.status_code == 200
    assert transport.captured is not None
    headers = transport.captured.headers
    assert "signature" in headers, (
        "RFC 9421 Signature header missing from outbound webhook — "
        "buyers enforcing webhook-signing would reject every delivery"
    )
    assert "signature-input" in headers, (
        "RFC 9421 Signature-Input header missing — verifiers cannot "
        "validate without the covered-components metadata"
    )
    # Content-Digest is bound into the signature per the AdCP profile;
    # without it, the receiver cannot verify body integrity.
    assert "content-digest" in headers
    # Profile pinning: Signature-Input MUST carry ``tag="adcp/webhook-signing/v1"``
    # and ``keyid=`` so receivers can statically validate the declared
    # profile and look up the signing key. Without these parameters,
    # the on-wire signature is not adcp/webhook-signing/v1-conformant
    # regardless of cryptographic validity.
    signature_input = headers["signature-input"]
    assert (
        'tag="adcp/webhook-signing/v1"' in signature_input
    ), f"Signature-Input missing profile tag: {signature_input!r}"
    assert (
        'keyid="test-webhook-ed25519-2026"' in signature_input
    ), f"Signature-Input missing keyid: {signature_input!r}"


@pytest.mark.asyncio
async def test_bearer_sender_does_not_emit_rfc9421_headers() -> None:
    """A bearer-token sender MUST NOT emit RFC 9421 headers — the
    ``signs_with_rfc9421`` property is the validator's only reliable
    signal, so this test pins the contract at the sender level.
    """
    transport = _CapturingTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_bearer_token("test-token", client=client)
    assert sender.signs_with_rfc9421 is False

    async with sender:
        await sender.send_raw(
            url="http://test/webhooks/adcp",
            idempotency_key="whk_test",
            payload={"x": 1},
        )

    assert transport.captured is not None
    assert "signature" not in transport.captured.headers
    assert "signature-input" not in transport.captured.headers
    assert transport.captured.headers["authorization"] == "Bearer test-token"


def test_jwk_sender_reports_signs_with_rfc9421() -> None:
    """The validator reads ``signs_with_rfc9421`` to gate boot — the
    JWK constructor must set it ``True`` so the gate accepts.
    """
    sender = WebhookSender.from_jwk(_jwk_with_private())
    assert sender.signs_with_rfc9421 is True


# ----- AC5: boot fails when capabilities advertise signing but no key -----


class _Caps:
    """Minimal capabilities stub — the validator only reads the
    ``webhook_signing`` attribute.
    """

    def __init__(
        self,
        webhook_signing: WebhookSigning | None,
        *,
        webhook_signing_managed_externally: bool = False,
    ) -> None:
        self.webhook_signing = webhook_signing
        self.webhook_signing_managed_externally = webhook_signing_managed_externally


def test_boot_passes_when_capabilities_omit_webhook_signing() -> None:
    """No advertisement, no obligation — validator returns silently."""
    validate_webhook_signing_for_capabilities(
        capabilities=_Caps(webhook_signing=None),
        sender=None,
        supervisor=None,
    )


def test_boot_passes_when_supported_false() -> None:
    """Capabilities present but ``supported=False`` is still a non-advertisement."""
    validate_webhook_signing_for_capabilities(
        capabilities=_Caps(webhook_signing=WebhookSigning(supported=False)),
        sender=None,
        supervisor=None,
    )


def test_boot_fails_when_signing_advertised_but_no_sender() -> None:
    """The headline #384 failure mode: capabilities advertise signing,
    nothing is wired, buyers enforcing RFC 9421 see silent blackout.
    """
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(webhook_signing=WebhookSigning(supported=True)),
            sender=None,
            supervisor=None,
        )
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["missing"] == "webhook_sender_with_rfc9421_key"
    assert exc_info.value.details["capabilities_webhook_signing_supported"] is True


def test_boot_passes_when_signing_is_adopter_managed_without_sdk_sender() -> None:
    """Adopters with external webhook delivery/signing can advertise the
    capability without wiring the SDK sender stack."""
    validate_webhook_signing_for_capabilities(
        capabilities=_Caps(
            webhook_signing=WebhookSigning(
                supported=True,
                profile="adcp/webhook-signing/v1",
                algorithms=["ed25519"],
            ),
            webhook_signing_managed_externally=True,
        ),
        sender=None,
        supervisor=None,
    )


def test_adopter_managed_flag_rejects_non_bool_values() -> None:
    """Fail closed on mistyped config instead of truthiness-bypassing validation."""
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(
                webhook_signing=WebhookSigning(supported=True),
                webhook_signing_managed_externally="false",  # type: ignore[arg-type]
            ),
            sender=None,
            supervisor=None,
        )
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["field"] == "webhook_signing_managed_externally"
    assert exc_info.value.details["value_type"] == "str"


def test_boot_fails_when_signing_advertised_with_bearer_sender() -> None:
    """A non-JWK sender (bearer / HMAC) advertised as RFC 9421 trips the
    same gate — buyers see the capability but receive unsignable bytes.
    """
    sender = WebhookSender.from_bearer_token("test-token")
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(webhook_signing=WebhookSigning(supported=True)),
            sender=sender,
            supervisor=None,
        )
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["sender_auth_mode"] == "BearerTokenStrategy"


def test_adopter_managed_flag_does_not_bypass_wired_sender_validation() -> None:
    """If the SDK sender is wired, validate the actual bytes it will emit."""
    sender = WebhookSender.from_bearer_token("test-token")
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(
                webhook_signing=WebhookSigning(supported=True),
                webhook_signing_managed_externally=True,
            ),
            sender=sender,
            supervisor=None,
        )
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["sender_auth_mode"] == "BearerTokenStrategy"


def test_boot_fails_when_signing_advertised_with_legacy_hmac_sender() -> None:
    """3.x's ``legacy_hmac_fallback`` is delivery-axis only — a seller
    advertising ``webhook_signing.supported=True`` still owes RFC 9421
    headers. HMAC-only senders trip the gate.
    """
    sender = WebhookSender.from_adcp_legacy_hmac(b"secret", key_id="hmac-1")
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(webhook_signing=WebhookSigning(supported=True)),
            sender=sender,
            supervisor=None,
        )
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["sender_auth_mode"] == "AdcpLegacyHmacStrategy"


def test_boot_passes_when_jwk_sender_wired() -> None:
    """The happy path: capabilities advertise signing, a JWK sender is
    wired, boot proceeds.
    """
    sender = WebhookSender.from_jwk(_jwk_with_private())
    validate_webhook_signing_for_capabilities(
        capabilities=_Caps(webhook_signing=WebhookSigning(supported=True)),
        sender=sender,
        supervisor=None,
    )


def test_boot_passes_when_supervisor_wraps_jwk_sender() -> None:
    """The realistic adopter wiring: the supervisor owns the sender, the
    framework reads it back via the convention-private ``_sender``.
    """
    sender = WebhookSender.from_jwk(_jwk_with_private())
    supervisor = InMemoryWebhookDeliverySupervisor(sender=sender)
    validate_webhook_signing_for_capabilities(
        capabilities=_Caps(webhook_signing=WebhookSigning(supported=True)),
        sender=None,
        supervisor=supervisor,
    )


def test_boot_fails_when_supervisor_wraps_bearer_sender() -> None:
    """Sender introspection through the supervisor must surface non-RFC-9421
    senders too — the supervisor wrapper does not change the auth-mode
    contract on the wire.
    """
    sender = WebhookSender.from_bearer_token("test-token")
    supervisor = InMemoryWebhookDeliverySupervisor(sender=sender)
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(webhook_signing=WebhookSigning(supported=True)),
            sender=None,
            supervisor=supervisor,
        )
    assert exc_info.value.details["sender_auth_mode"] == "BearerTokenStrategy"


def test_boot_skips_validation_for_protocol_only_supervisor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A custom supervisor without an introspectable ``_sender`` (e.g., a
    Celery / Kafka queue-only impl) is the adopter's contract to honor.
    The validator skips the check but logs a WARNING so the gap is
    observable in boot logs — silent skip would mask the same
    silent-blackout failure mode the gate exists to close.
    """
    custom_supervisor = MagicMock(spec=[])  # no ``_sender`` attr
    with caplog.at_level("WARNING", logger="adcp.decisioning.webhook_emit"):
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(webhook_signing=WebhookSigning(supported=True)),
            sender=None,
            supervisor=custom_supervisor,
        )
    assert any(
        "no introspectable _sender attribute" in rec.message for rec in caplog.records
    ), f"expected WARNING about protocol-only supervisor; got {caplog.records!r}"


# ----- legacy_hmac_fallback does not relax the wired-sender requirement -----


def test_boot_fails_with_hmac_sender_even_when_legacy_hmac_fallback_advertised() -> None:
    """``legacy_hmac_fallback`` is the per-receiver downgrade switch
    (HMAC for receivers that have not adopted RFC 9421) — NOT a
    substitute for the seller's RFC 9421 capability. A seller declaring
    ``supported=True, legacy_hmac_fallback=True`` still owes RFC 9421
    headers for receivers that DO support them. HMAC-only senders trip
    the gate regardless of the fallback flag.
    """
    sender = WebhookSender.from_adcp_legacy_hmac(b"secret", key_id="hmac-1")
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(
                webhook_signing=WebhookSigning(
                    supported=True,
                    legacy_hmac_fallback=True,
                ),
            ),
            sender=sender,
            supervisor=None,
        )
    assert exc_info.value.details["sender_auth_mode"] == "AdcpLegacyHmacStrategy"


# ----- algorithm cross-check -----


def test_boot_fails_when_sender_alg_not_in_advertised_algorithms() -> None:
    """Advertised ``algorithms=["ecdsa-p256-sha256"]`` + ed25519 sender
    is the silent-blackout case one axis deeper than the supported
    check: buyers pinning their verifier to the advertised set reject
    every delivery whose ``Signature-Input alg=`` is outside the set.
    """
    sender = WebhookSender.from_jwk(_jwk_with_private())  # ed25519
    with pytest.raises(AdcpError) as exc_info:
        validate_webhook_signing_for_capabilities(
            capabilities=_Caps(
                webhook_signing=WebhookSigning(
                    supported=True,
                    algorithms=["ecdsa-p256-sha256"],
                ),
            ),
            sender=sender,
            supervisor=None,
        )
    assert exc_info.value.details["missing"] == "webhook_signing_algorithm_alignment"
    assert exc_info.value.details["advertised_algorithms"] == ["ecdsa-p256-sha256"]
    assert exc_info.value.details["sender_alg"] == "ed25519"


def test_boot_passes_when_sender_alg_in_advertised_algorithms() -> None:
    """The happy path: advertised set includes the sender's alg."""
    sender = WebhookSender.from_jwk(_jwk_with_private())  # ed25519
    validate_webhook_signing_for_capabilities(
        capabilities=_Caps(
            webhook_signing=WebhookSigning(
                supported=True,
                algorithms=["ed25519", "ecdsa-p256-sha256"],
            ),
        ),
        sender=sender,
        supervisor=None,
    )


def test_boot_skips_alg_check_when_algorithms_omitted() -> None:
    """``algorithms`` is optional on the wire — omission means the seller
    is not pinning verifiers to a specific set, so any RFC 9421 sender
    is acceptable. Cross-check skipped.
    """
    sender = WebhookSender.from_jwk(_jwk_with_private())  # ed25519
    validate_webhook_signing_for_capabilities(
        capabilities=_Caps(
            webhook_signing=WebhookSigning(supported=True, algorithms=None),
        ),
        sender=sender,
        supervisor=None,
    )
