import pytest
from pydantic import TypeAdapter, ValidationError

from adcp.types.domains.trusted_match.identity_match_response import (
    IdentityMatchResponse,
)
from adcp.types.domains.trusted_match.provider_identity_match_response import (
    IdentityMatchResponseProviderRouter,
)
from adcp.types.domains.trusted_match.provider_registration import (
    TmpProviderRegistration,
    TmpProviderRegistration1,
    TmpProviderRegistration2,
)
from adcp.types.domains.trusted_match.publisher_tmpx_config import (
    PublisherTmpxMacroMapping,
)


def test_provider_registration_requires_identity_dimensions_when_identity_match_enabled():
    with pytest.raises(ValidationError, match="countries is required"):
        TypeAdapter(TmpProviderRegistration).validate_python(
            {
                "provider_id": "provider_1",
                "endpoint": "https://example.com",
                "identity_match": True,
            }
        )


def test_provider_registration_rejects_non_https_endpoint():
    with pytest.raises(ValidationError, match="endpoint must use https"):
        TypeAdapter(TmpProviderRegistration).validate_python(
            {
                "provider_id": "provider_1",
                "endpoint": "http://example.com",
                "context_match": True,
            }
        )


@pytest.mark.parametrize("arm", [TmpProviderRegistration1, TmpProviderRegistration2])
def test_each_provider_registration_arm_carries_its_own_validators(arm):
    # 3.3.0-beta.5 generates a helper class (MatchKey3) between the two arms;
    # the post-generate fixer must still attach arm 1's validators to arm 1.
    decorators = arm.__pydantic_decorators__
    assert "_require_https_endpoint" in decorators.field_validators
    assert "_require_identity_match_dimensions" in decorators.model_validators


def test_provider_registration_accepts_valid_identity_registration():
    registration = TypeAdapter(TmpProviderRegistration).validate_python(
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
    with pytest.raises(ValidationError, match="String should match pattern"):
        IdentityMatchResponse.model_validate(
            {
                "request_id": "request_1",
                "eligible_package_ids": [],
                "serve_window_sec": 60,
                "tmpx_providers": {
                    "bad provider!": {"chunks": [{"slot_id": "primary", "value": "abc"}]}
                },
            }
        )


def test_identity_match_response_accepts_valid_tmpx_provider_ids():
    response = IdentityMatchResponse.model_validate(
        {
            "request_id": "request_1",
            "eligible_package_ids": [],
            "serve_window_sec": 60,
            "tmpx_providers": {"provider_1": {"chunks": [{"slot_id": "primary", "value": "abc"}]}},
        }
    )

    assert response.tmpx_providers is not None
    assert "provider_1" in response.tmpx_providers


def test_provider_identity_match_response_accepts_tmpx_chunks():
    response = IdentityMatchResponseProviderRouter.model_validate(
        {
            "request_id": "request_1",
            "eligible_package_ids": [],
            "serve_window_sec": 60,
            "tmpx_chunks": [{"slot_id": "primary", "value": "abc"}],
        }
    )

    assert response.tmpx_chunks is not None
    assert response.tmpx_chunks[0].slot_id == "primary"


def test_publisher_tmpx_mapping_validates_provider_and_slot_keys():
    mapping = PublisherTmpxMacroMapping.model_validate(
        {"tmpx_macro_mapping": {"provider_1": {"primary": "PIN_TMPX_1"}}}
    )
    assert mapping.tmpx_macro_mapping["provider_1"]["primary"] == "PIN_TMPX_1"

    with pytest.raises(ValidationError, match="String should match pattern"):
        PublisherTmpxMacroMapping.model_validate(
            {"tmpx_macro_mapping": {"bad provider!": {"primary": "PIN_TMPX_1"}}}
        )

    with pytest.raises(ValidationError, match="String should match pattern"):
        PublisherTmpxMacroMapping.model_validate(
            {"tmpx_macro_mapping": {"provider_1": {"bad slot!": "PIN_TMPX_1"}}}
        )
