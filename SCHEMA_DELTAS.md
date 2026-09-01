# Generated-types delta

## Field changes

- `bundled/protocol/get_adcp_capabilities_response.py`
  - **classes added**: ChangeFeed3, EventType4, EventType5, Idempotency3, IdentityUpdates3, Mode16, Mode17, Notifications5, Notifications6, Notifications7, Operation4, SupportedTarget3, Tasks10, Tasks12, Tasks13, Tasks14, Tasks15, Tasks16, Tasks17, Tasks18, Tasks19, Type9
  - **classes removed**: ChangeFeed1, EventType2, EventType3, Idempotency1, IdentityUpdates1, Mode11, Mode12, Notifications1, Notifications2, Notifications3, Operation3, SupportedTarget1, Tasks1, Tasks2, Tasks3, Tasks4, Tasks5, Tasks6, Tasks7, Tasks8, Tasks9, Type6
  - `Brand`: `+available_uses`, `+description`, `+generation_providers`, `+right_types`, `+rights` `-brand_id`, `-brand_kit_override`, `-countries`, `-data_subject_contestation`, `-domain`, `-industries`
  - `Brand1`: `+brand_id`, `+brand_kit_override`, `+countries`, `+data_subject_contestation`, `+domain`, `+industries` `-available_uses`, `-description`, `-generation_providers`, `-right_types`, `-rights`
  - `Features`: `+catalog_signals` `-bidding_policy`, `-catalog_item_availability_updates`, `-catalog_management`, `-committed_metrics_supported`, `-inline_creative_management`, `-property_list_filtering`, `-seller_optimized_budget`
  - `Features1`: `+bidding_policy`, `+catalog_item_availability_updates`, `+catalog_management`, `+committed_metrics_supported`, `+inline_creative_management`, `+property_list_filtering`, `+seller_optimized_budget` `-catalog_signals`
  - `Tasks`: `+modes`, `+task` `-root`
- `core/reporting_webhook.py`
  - `ReportingWebhook`: `-operation_id`
- `protocol/get_adcp_capabilities_response.py`
  - **classes added**: ChangeFeed1, EventType3, Idempotency1, IdentityUpdates1, Notifications1, Notifications2, Notifications3, SupportedTarget1, Tasks1, Tasks2, Tasks3, Tasks4, Tasks5, Tasks6, Tasks7, Tasks8, Tasks9, Type6
  - **classes removed**: ChangeFeed3, EventType5, Idempotency3, IdentityUpdates3, Notifications5, Notifications6, Notifications7, SupportedTarget3, Tasks10, Tasks12, Tasks13, Tasks14, Tasks15, Tasks16, Tasks17, Tasks18, Tasks19, Type9
  - `Tasks`: `+root` `-modes`, `-task`
