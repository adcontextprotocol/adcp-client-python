# Generated-types delta

## Files added

- `core/match_key.py` — MatchKey
- `enums/match_key_use.py` — MatchKeyUse

## Field changes

- `bundled/protocol/get_adcp_capabilities_response.py`
  - **classes added**: BrandKitOverride11, PropertyType, SupportedMatchKey, Use, Vendor9
  - **classes removed**: Surface
  - `MediaBuy`: `+supported_match_keys`
- `core/product_identity.py`
  - `ProductIdentity`: `+match_keys`
- `protocol/get_adcp_capabilities_response.py`
  - `MediaBuy`: `+supported_match_keys`
- `trusted_match/provider_registration.py`
  - **classes added**: MatchKey, Use
  - `TmpProviderRegistration1`: `+match_keys`
  - `TmpProviderRegistration2`: `+match_keys`
