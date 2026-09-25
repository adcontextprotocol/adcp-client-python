# Generated-types delta

## Files added

- `compliance/get_creative_features_completion.py` — GetCreativeFeaturesComplianceCompletion
- `core/async_response_refs/creative/get_creative_features_async_response_submitted.py` — GetCreativeFeaturesSubmitted
- `core/verification_token_claims.py` — AgenticadvertisingOrgVerificationTokenClaims, Role, VerificationTokenGradingProfile, VerificationTokenMode
- `creative/get_creative_features_async_response_submitted.py` — GetCreativeFeaturesSubmitted
- `creative/get_creative_features_terminal_success.py` — GetCreativeFeaturesSuccess
- `enums/request_signing_error_code.py` — RequestSigningErrorCode
- `enums/seller_policy_decline_reason.py` — SellerPolicyDeclineReason
- `error_details/requote_required.py` — EnvelopeField, EnvelopeField1, EnvelopeField1Item, RequoteRequiredDetails

## Field changes

- `a2ui/si_catalog.py`
  - **classes added**: Action23
  - **classes removed**: Action22
- `account/sync_accounts_response.py`
  - `Account`: `+account`
- `bundled/protocol/get_adcp_capabilities_response.py`
  - `MediaBuy`: `+anonymous_discovery`
  - `Signals`: `+anonymous_discovery`
  - `Specialism`: `+sales_exchange`, `+sales_retail_media`, `+sales_streaming_tv`
- `compliance/comply_test_controller_request.py`
  - `Arm`: `+completed`
  - `Operation`: `+advance_past_escalation`, `+advance_past_status_deadline`
  - `Params`: `+evaluation_id`
- `compliance/comply_test_controller_response.py`
  - `ComplyResponseArm`: `+completed`
  - `Forced`: `+evaluation_id`
- `core/collection.py`
  - `Collection`: `+publisher_domain`
- `core/registry_event.py`
  - **classes added**: GradingProfile
  - `Payload8`: `+grading_profile`
  - `Payload9`: `+grading_profile`
- `core/x_entity_types.py`
  - `XEntityTypes`: `+creative_evaluation`
- `creative/get_creative_features_request.py`
  - `GetCreativeFeaturesRequest`: `+idempotency_key`, `+push_notification_config`
- `creative/get_creative_features_response.py`
  - **classes added**: GetCreativeFeaturesResponse3
  - **classes removed**: GetCreativeFeaturesResponse1
  - `GetCreativeFeaturesResponse2`: `+evaluation_id`
- `enums/error_code.py`
  - `ErrorCode`: `+COMMITTED_RESOURCE_PURGED`
- `enums/specialism.py`
  - `AdcpSpecialism`: `+sales_exchange`, `+sales_retail_media`, `+sales_streaming_tv`
- `enums/task_type.py`
  - `TaskType`: `+get_creative_features`
- `error_details/action_not_allowed.py`
  - `ActionNotAllowedDetails`: `+decline_reason`
- `media_buy/accept_proposal_request.py`
  - `AcceptProposalRequest`: `+name`
- `media_buy/accept_proposal_response.py`
  - `AcceptProposalResponse1`: `+name`
- `media_buy/buy_products_request.py`
  - `BuyProductsRequest`: `+name`
- `media_buy/buy_products_response.py`
  - `BuyProductsResponse1`: `+name`
- `media_buy/media_buy_commitment_response.py`
  - `MediaBuyCommitmentResponse1`: `+name`
- `protocol/get_adcp_capabilities_response.py`
  - `MediaBuy`: `+anonymous_discovery`
  - `Signals`: `+anonymous_discovery`
- `protocol/sync_principal_response.py`
  - **classes added**: Action33
  - **classes removed**: Action32
