# Generated-types delta

## Files added

- `trusted_match/provider_identity_match_response.py` — IdentityMatchResponseProviderRouter
- `trusted_match/publisher_tmpx_config.py` — PublisherTmpxMacroMapping
- `trusted_match/tmpx_chunk.py` — TmpxChunk

## Field changes

- `trusted_match/identity_match_response.py`
  - **classes added**: IdentityMatchResponseRouterPublisher
  - **classes removed**: IdentityMatchResponse
  - `TmpxProviders`: `+chunks` `-macros`
- `trusted_match/provider_registration.py`
  - **classes added**: TmpxSlot
  - `TmpProviderRegistration1`: `+tmpx_slots` `-tmpx_macros`
  - `TmpProviderRegistration2`: `+tmpx_slots` `-tmpx_macros`
