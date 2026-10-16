# Generated-types delta

## Field changes

- `account/list_account_changes_request.py`
  - `ListAccountChangesRequest`: `+adcp_major_version`, `+adcp_version`
- `bundled/protocol/get_adcp_capabilities_response.py`
  - **classes added**: C2paAction, MediaType, Method3, Persistence, Position
  - **classes removed**: C2PAWatermarkAction, Disclosure3, Disclosure4, Disclosure5, Disclosure6, DisclosurePersistence, DisclosurePosition, EmbeddedProvenanceItem2, EmbeddedProvenanceItem3, EmbeddedProvenanceItem4, EmbeddedProvenanceItem5, EmbeddedProvenanceMethod, Logo1, Logo2, Logo3, Logo4, Provenance2, Provenance3, Provenance4, Provenance5, Watermark2, Watermark3, Watermark4, Watermark5, WatermarkMediaType
  - `Method`: `+manifest_wrapper`, `+provenance_markers` `-access_mode`, `-destination_modes`, `-format`, `-orchestration`, `-pattern`, `-producer_identity`, `-provider`, `-reader_compatibility`, `-transport`
- `core/provenance.py`
  - **classes added**: VerifyAgent10
  - **classes removed**: VerifyAgent18
- `core/reporting_status_issue.py`
  - `Code`: `+REVISION_UNREADABLE`
  - `ReportingStatusIssue`: `+reporting_revision_id`
- `media_buy/accept_proposal_request.py`
  - `AcceptProposalRequest`: `+adcp_major_version`, `+adcp_version`
- `media_buy/buy_products_request.py`
  - `BuyProductsRequest`: `+adcp_major_version`, `+adcp_version`
- `media_buy/control_media_buy_request.py`
  - `ControlMediaBuyRequest`: `+adcp_major_version`, `+adcp_version`
- `media_buy/decline_proposals_request.py`
  - `DeclineProposalsRequest`: `+adcp_major_version`, `+adcp_version`
- `media_buy/list_products_request.py`
  - `ListProductsRequest`: `+adcp_major_version`, `+adcp_version`
- `media_buy/refine_proposals_request.py`
  - `RefineProposalsRequest`: `+adcp_major_version`, `+adcp_version`
- `media_buy/request_proposals_request.py`
  - `RequestProposalsRequest`: `+adcp_major_version`, `+adcp_version`
- `media_buy/sync_reporting_status_request.py`
  - `SyncReportingStatusRequest`: `+adcp_major_version`, `+adcp_version`
- `protocol/get_principal_response.py`
  - **classes added**: Result2, Result3, Result5
  - **classes removed**: Result6, Result7, Result9
- `protocol/sync_principal_response.py`
  - **classes added**: Result13, Result15
  - **classes removed**: Result17, Result19
