# Generated-types delta

## Field changes

- `bundled/protocol/get_adcp_capabilities_response.py`
  - **classes added**: AssignmentOperation
  - `Change`: `+clickthrough` `-click_through`
  - `Creative`: `+assignment_operations`
- `core/registry_event.py`
  - `Tracks`: `+passed`
- `core/signal_definition.py`
  - `MatchKey`: `+full_name`
- `core/signal_definition_enrichment.py`
  - `MatchKey`: `+full_name`
- `enums/binary_verdict.py`
  - `BinaryVerdict`: `+passed`
- `enums/creative_change_kind.py`
  - `CreativeChangeKind`: `+clickthrough` `-click_through`
- `enums/delivery_projection_end_state.py`
  - `DeliveryProjectionEndState`: `+under_delivery` `-underdelivery`
- `enums/distribution_identifier_type.py`
  - `DistributionIdentifierType`: `+facebook_page_handle`, `+facebook_page_id`, `+instagram_account_id`, `+instagram_handle`, `+snapchat_handle`, `+snapchat_profile_id`, `+x_handle`, `+x_user_id`
- `error_details/rate_limited.py`
  - `Scope`: `+all`
- `formats/canonical/coordinated_placements.py`
  - `Params4`: `+cta_values`
- `formats/canonical/image_carousel.py`
  - `CanonicalFormatImageCarousel`: `+cta_values`
- `protocol/get_adcp_capabilities_response.py`
  - **classes added**: AssignmentOperation
  - `Creative`: `+assignment_operations`
- `signals/get_signals_response.py`
  - `MatchKey`: `+full_name`
