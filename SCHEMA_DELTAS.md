# Generated-types delta

## Files added

- `core/verification_token_claims.py` — AgenticadvertisingOrgVerificationTokenClaims, Role, VerificationTokenGradingProfile, VerificationTokenMode

## Field changes

- `a2ui/si_catalog.py`
  - **classes added**: Action23
  - **classes removed**: Action22
- `account/sync_accounts_response.py`
  - `Account`: `+account`
- `bundled/protocol/get_adcp_capabilities_response.py`
  - `MediaBuy`: `+anonymous_discovery`
  - `Signals`: `+anonymous_discovery`
- `compliance/comply_test_controller_request.py`
  - `Operation`: `+advance_past_escalation`, `+advance_past_status_deadline`
- `core/collection.py`
  - `Collection`: `+publisher_domain`
- `core/registry_event.py`
  - **classes added**: GradingProfile
  - `Payload8`: `+grading_profile`
  - `Payload9`: `+grading_profile`
- `protocol/get_adcp_capabilities_response.py`
  - `MediaBuy`: `+anonymous_discovery`
  - `Signals`: `+anonymous_discovery`
- `protocol/sync_principal_response.py`
  - **classes added**: Action33
  - **classes removed**: Action32
