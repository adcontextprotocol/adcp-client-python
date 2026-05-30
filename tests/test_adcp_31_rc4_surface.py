"""Public surface and validation checks for AdCP 3.1 rc4 schema additions."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

_RC4_EXPORTS = (
    "MediaBuyDeliveryWebhookResult",
    "PricingCurrency",
    "ResponsePayloadJwsEnvelope",
    "SignalDefinitionEnrichment",
    "VerifyBrandClaimPayload",
    "VerifyBrandClaimSignedResponse",
    "VerifyBrandClaimSignedSuccessPayload",
    "VerifyBrandClaimsErrorResponse",
    "VerifyBrandClaimsPayload",
    "VerifyBrandClaimsResponse",
    "VerifyBrandClaimsSignedResponse",
    "VerifyBrandClaimsSignedSuccessPayload",
)

_SIGNED_PAYLOAD_BASE = {
    "typ": "adcp-response-payload+jws",
    "brand_domain": "brand.example",
    "agent_url": "https://brand.example/.well-known/adcp-agent.json",
    "request_hash": "sha256:" + "A" * 43,
    "iat": 1_700_000_000,
    "exp": 1_700_003_600,
}


def test_rc4_symbols_are_publicly_exported() -> None:
    import adcp
    import adcp.types

    for name in _RC4_EXPORTS:
        assert hasattr(adcp, name), f"{name} not exported from adcp"
        assert name in adcp.__all__, f"{name} not declared in adcp.__all__"
        assert hasattr(adcp.types, name), f"{name} not exported from adcp.types"
        assert name in adcp.types.__all__, f"{name} not declared in adcp.types.__all__"


def test_verify_brand_claim_success_requires_signed_response() -> None:
    from adcp.types import VerifyBrandClaimResponse

    with pytest.raises(ValidationError, match="signed_response"):
        TypeAdapter(VerifyBrandClaimResponse).validate_python(
            {
                "claim_type": "property",
                "verification_status": "owned",
            }
        )


def test_verify_brand_claim_signed_response_requires_payload() -> None:
    from adcp.types import VerifyBrandClaimSignedResponse

    with pytest.raises(ValidationError, match="payload"):
        VerifyBrandClaimSignedResponse.model_validate(
            {
                "protected": "abc",
                "signature": "def",
            }
        )


def test_generic_response_payload_jws_requires_typ() -> None:
    from adcp.types import ResponsePayloadJwsEnvelope

    with pytest.raises(ValidationError, match="typ"):
        ResponsePayloadJwsEnvelope.model_validate(
            {
                "protected": "abc",
                "payload": {
                    "task": "verify_brand_claim",
                    "brand_domain": "brand.example",
                    "agent_url": "https://brand.example/.well-known/adcp-agent.json",
                    "request_hash": "sha256:" + "A" * 43,
                    "iat": 1_700_000_000,
                    "exp": 1_700_003_600,
                    "response": {"claim_type": "property", "verification_status": "owned"},
                },
                "signature": "def",
            }
        )


def test_verify_brand_claim_signed_payload_requires_base_metadata() -> None:
    from adcp.types import VerifyBrandClaimSignedResponse

    response = {"claim_type": "property", "verification_status": "owned"}
    with pytest.raises(ValidationError, match="typ"):
        VerifyBrandClaimSignedResponse.model_validate(
            {
                "protected": "abc",
                "payload": {
                    "task": "verify_brand_claim",
                    "response": response,
                },
                "signature": "def",
            }
        )

    missing_task = dict(_SIGNED_PAYLOAD_BASE)
    with pytest.raises(ValidationError, match="task"):
        VerifyBrandClaimSignedResponse.model_validate(
            {
                "protected": "abc",
                "payload": {
                    **missing_task,
                    "response": response,
                },
                "signature": "def",
            }
        )

    signed = VerifyBrandClaimSignedResponse.model_validate(
        {
            "protected": "abc",
            "payload": {
                **_SIGNED_PAYLOAD_BASE,
                "task": "verify_brand_claim",
                "response": response,
            },
            "signature": "def",
        }
    )
    assert signed.payload.brand_domain == "brand.example"
    assert signed.payload.typ == "adcp-response-payload+jws"


def test_verify_brand_claims_success_requires_signed_response_payload() -> None:
    from adcp.types import VerifyBrandClaimsResponseBulk, VerifyBrandClaimsSignedResponse

    result = {"claim_type": "property", "status": "owned"}
    with pytest.raises(ValidationError, match="signed_response"):
        VerifyBrandClaimsResponseBulk.model_validate({"results": [result]})

    with pytest.raises(ValidationError, match="payload"):
        VerifyBrandClaimsSignedResponse.model_validate(
            {
                "protected": "abc",
                "signature": "def",
            }
        )


def test_verify_brand_claims_signed_payload_requires_base_metadata() -> None:
    from adcp.types import VerifyBrandClaimsSignedResponse

    result = {"claim_type": "property", "status": "owned"}
    with pytest.raises(ValidationError, match="typ"):
        VerifyBrandClaimsSignedResponse.model_validate(
            {
                "protected": "abc",
                "payload": {
                    "task": "verify_brand_claims",
                    "response": {"results": [result]},
                },
                "signature": "def",
            }
        )

    missing_task = dict(_SIGNED_PAYLOAD_BASE)
    with pytest.raises(ValidationError, match="task"):
        VerifyBrandClaimsSignedResponse.model_validate(
            {
                "protected": "abc",
                "payload": {
                    **missing_task,
                    "response": {"results": [result]},
                },
                "signature": "def",
            }
        )

    signed = VerifyBrandClaimsSignedResponse.model_validate(
        {
            "protected": "abc",
            "payload": {
                **_SIGNED_PAYLOAD_BASE,
                "task": "verify_brand_claims",
                "response": {"results": [result]},
            },
            "signature": "def",
        }
    )
    assert signed.payload.request_hash == _SIGNED_PAYLOAD_BASE["request_hash"]
    assert signed.payload.typ == "adcp-response-payload+jws"
