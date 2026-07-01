# Generated-types delta

## Files added

- `trusted_match/available_package.py` — AvailablePackage
- `trusted_match/context_match_request.py` — ArtifactRef, ContextMatchRequest, ContextSignals, Geo, Keyword, Metro, Sentiment, Type
- `trusted_match/context_match_response.py` — ContextMatchResponse, Signals, TargetingKv
- `trusted_match/error.py` — Code, TmpError
- `trusted_match/identity_match_request.py` — Attestation, Consent, Identity, IdentityMatchRequest, SealedCredential, VerificationLevel
- `trusted_match/identity_match_response.py` — IdentityMatchResponse, TmpxMacro, TmpxProviders
- `trusted_match/offer.py` — Offer
- `trusted_match/offer_price.py` — Model, OfferPrice
- `trusted_match/provider_registration.py` — Country, Status, TmpProviderRegistration, TmpProviderRegistration1, TmpProviderRegistration2, TmpxMacro

## Files removed

- `tmp/available_package.py` — AvailablePackage
- `tmp/context_match_request.py` — ArtifactRef, ContextMatchRequest, ContextSignals, Geo, Keyword, Metro, Sentiment, Type
- `tmp/context_match_response.py` — ContextMatchResponse, Signals, TargetingKv
- `tmp/error.py` — Code, TmpError
- `tmp/identity_match_request.py` — Attestation, Consent, Identity, IdentityMatchRequest, SealedCredential, VerificationLevel
- `tmp/identity_match_response.py` — IdentityMatchResponse
- `tmp/offer.py` — Offer
- `tmp/offer_price.py` — Model, OfferPrice
- `tmp/provider_registration.py` — Country, Status, TmpProviderRegistration, TmpProviderRegistration1, TmpProviderRegistration2

## Field changes

- `bundled/protocol/get_adcp_capabilities_response.py`
  - `MediaBuy`: `+governance_aware`
- `core/registry_feed_response.py`
  - **classes added**: Freshness
  - `RegistryFeedResponse`: `+freshness`
- `manifest_schema.py`
  - `Protocol`: `+trusted_match` `-tmp`
- `protocol/get_adcp_capabilities_response.py`
  - `MediaBuy`: `+governance_aware`
