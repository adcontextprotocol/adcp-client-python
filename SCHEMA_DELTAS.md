# Generated-types delta

## Files added

- `core/placement_presentation.py` — BoxDecoration, Canvas, Color, CreativeSlot, Fit, ImageDecoration, ImageRef, Layer, PlacementPresentationDocument, Rectangle, TextDecoration
- `core/presentation_ref.py` — PlacementPresentationReference
- `core/preview_provider.py` — PublisherDesignatedPreviewProvider, Route
- `core/preview_renderer_metadata.py` — PreviewRendererMetadata, RenderingOrigin
- `core/reference_renderer.py` — Provenance, ReferenceRenderer

## Field changes

- `adagents.py`
  - `AdcpAgentsAuthorization210`: `+catalog_role`
  - `AdcpAgentsAuthorization211`: `+catalog_role`
  - `AdcpAgentsAuthorization212`: `+catalog_role`
  - `AdcpAgentsAuthorization213`: `+catalog_role`
  - `AdcpAgentsAuthorization27`: `+catalog_role`
  - `AdcpAgentsAuthorization28`: `+catalog_role`
  - `AdcpAgentsAuthorization29`: `+catalog_role`
- `brand_discovery.py`
  - `ImageAsset`: `+file_size_bytes`
- `bundled/protocol/get_adcp_capabilities_response.py`
  - **classes added**: Preview, RenderingOrigin, Route
  - `Creative`: `+preview`
  - `Logo`: `+file_size_bytes`
  - `Params7`: `+max_file_size_mb`
- `compliance/comply_test_controller_request.py`
  - `Operation`: `+expire_proposal`, `+prepare`
  - `Params`: `+proposal_id`
- `core/assets/asset_union.py`
  - `ImageAsset`: `+file_size_bytes`
- `core/assets/image_asset.py`
  - `ImageAsset`: `+file_size_bytes`
- `core/placement_definition.py`
  - `PlacementDefinition`: `+presentation_ref`, `+preview_provider`
- `creative/preview_render.py`
  - `PreviewRender1`: `+renderer`
  - `PreviewRender2`: `+renderer`
  - `PreviewRender3`: `+renderer`
- `enums/error_code.py`
  - `ErrorCode`: `+CONFLICTING_SELECTORS`
- `formats/canonical/audio_hosted.py`
  - `CanonicalFormatHostedAudio`: `+max_file_size_mb`
- `media_buy/accept_proposal_request.py`
  - `AcceptProposalRequest`: `+adcp_major_version`
- `media_buy/buy_products_request.py`
  - `BuyProductsRequest`: `+adcp_major_version`
- `media_buy/control_media_buy_request.py`
  - `ControlMediaBuyRequest`: `+adcp_major_version`
- `protocol/get_adcp_capabilities_response.py`
  - **classes added**: Preview, RenderingOrigin, Route
  - `Creative`: `+preview`
