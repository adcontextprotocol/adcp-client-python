# Generated-types delta

## Files added

- `core/event_surface.py` — Category, EventSurface

## Field changes

- `bundled/core/tasks_get_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/creative/get_creative_delivery_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/creative/get_creative_features_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/creative/list_creatives_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/creative/preview_creative_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/creative/preview_creative_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/creative/sync_creatives_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/creative/validate_input_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/build_creative_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/build_creative_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/create_media_buy_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/create_media_buy_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/get_media_buy_delivery_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/get_products_request.py`
  - `ConversionEvent`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/get_products_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/log_event_request.py`
  - **classes added**: Category, Surface
  - `CustomData`: `+progress_percent`, `+progress_seconds`
  - `Event`: `+surface`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/package_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/sync_catalogs_request.py`
  - `ConversionEvent`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/sync_event_sources_request.py`
  - **classes added**: ActionSource, Category, Surface
  - `EventSource`: `+action_source`, `+surface`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/update_media_buy_request.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/media_buy/update_media_buy_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `bundled/protocol/get_adcp_capabilities_response.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `core/event.py`
  - `Event`: `+surface`
- `core/event_custom_data.py`
  - `EventCustomData`: `+progress_percent`, `+progress_seconds`
- `enums/event_type.py`
  - `EventType`: `+content_view`, `+follow`, `+watch_milestone`
- `media_buy/sync_event_sources_request.py`
  - `EventSource`: `+action_source`, `+surface`
