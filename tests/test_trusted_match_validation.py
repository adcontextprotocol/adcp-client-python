import pytest
from pydantic import ValidationError

from adcp.types.generated_poc.trusted_match.identity_match_response import (
    IdentityMatchResponse,
)
from adcp.types.generated_poc.trusted_match.provider_registration import (
    TmpProviderRegistration,
)


def test_provider_registration_requires_identity_dimensions_when_identity_match_enabled():
    with pytest.raises(ValidationError, match="countries is required"):
        TmpProviderRegistration.model_validate(
            {
                "provider_id": "provider_1",
                "endpoint": "https://example.com",
                "identity_match": True,
            }
        )


def test_provider_registration_rejects_non_https_endpoint():
    with pytest.raises(ValidationError, match="endpoint must use https"):
        TmpProviderRegistration.model_validate(
            {
                "provider_id": "provider_1",
                "endpoint": "http://example.com",
                "context_match": True,
            }
        )


def test_provider_registration_accepts_valid_identity_registration():
    registration = TmpProviderRegistration.model_validate(
        {
            "provider_id": "provider_1",
            "endpoint": "https://example.com",
            "identity_match": True,
            "countries": ["US"],
            "uid_types": ["uid2"],
        }
    )

    assert registration.provider_id == "provider_1"


def test_identity_match_response_rejects_invalid_tmpx_provider_ids():
    with pytest.raises(ValidationError, match="tmpx_providers keys"):
        IdentityMatchResponse.model_validate(
            {
                "request_id": "request_1",
                "eligible_package_ids": [],
                "serve_window_sec": 60,
                "tmpx_providers": {
                    "bad provider!": {"macros": [{"name": "PIN_TMPX_1", "value": "abc"}]}
                },
            }
        )


def test_identity_match_response_accepts_valid_tmpx_provider_ids():
    response = IdentityMatchResponse.model_validate(
        {
            "request_id": "request_1",
            "eligible_package_ids": [],
            "serve_window_sec": 60,
            "tmpx_providers": {"provider_1": {"macros": [{"name": "PIN_TMPX_1", "value": "abc"}]}},
        }
    )

    assert response.tmpx_providers is not None
    assert "provider_1" in response.tmpx_providers
