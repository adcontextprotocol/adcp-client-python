# AdCP 3.3.0-beta.0: SDK-relevant changes

Inventory of every AdCP 3.3.0-beta.0 change that touches this SDK, for the
SDK 10 workstreams. Sources: the signed `3.3.0-beta.0` bundle (source commit
`a2d7eac350f0325331ae544bc071301afe01d2f2`, pinned in
`schemas/releases/3.3.0-beta.0.json`), its `CHANGELOG.md` section, the
regeneration delta of `src/adcp/types/domains/` against 3.2.4
(`SCHEMA_DELTAS.md` at the adoption commit), the protocol's
[what's new in 3.3](https://docs.adcontextprotocol.org/docs/reference/whats-new-in-3-3)
and the [3.2 to 3.3 migration guide](https://docs.adcontextprotocol.org/docs/reference/migration/3-2-to-3-3).

3.3 is a minor release: it adds optional fields, enum values and capability
declarations. The only removal is the experimental
`build_creative.evaluator.evaluator_id` source form. Beta.0 publishes the
protocol artifacts before SDK support; beta.1 is the SDK-backed cut.

The "SDK status" column records what the foundation workstream (1) did when
it adopted the bundle, and what is left to the codegen/wire-fidelity (2) and
3.3 feature-parity (3) workstreams.

## 1. Version negotiation and wire pin

| Item | Detail | SDK status |
| --- | --- | --- |
| Wire pin | `src/adcp/ADCP_VERSION` is `3.3.0-beta.0`; bundle key `3.3.0-beta.0` (prereleases keep their full identifier, `resolve_bundle_key`). The 3.2 cache stays synced from the signed 3.2.4 release as a preview bundle (`PREVIEW_VERSIONS`), beside 3.1.27. | done (1) |
| Advertised versions | `COMPATIBLE_ADCP_VERSIONS = ("3.0", "3.1", "3.2", "3.3")`; `get_supported_adcp_versions()` advertises the exact prerelease `3.3-beta.0`, never a stable `3.3`, so buyers opt into the beta they validated against (DR-0024). Full set: `3.0, 3.1, 3.2, 3.3-beta.0, 3.2-rc.7`. | done (1) |
| Negotiation | `negotiate_version` matches prereleases exactly. The client's outgoing conversion treats the SDK's served prerelease as its line (`is_convertible_version`), so a 3.3-beta.0 buyer still negotiates down to a 3.1 or 3.2 seller. | done (1) |
| Translation | `convert_payload` steps 3.3-beta.0 ↔ 3.2 ↔ 3.1 ↔ 3.0 through the bundled schemas; the 3.3 step uses the `3.3.0-beta.0` bundle. Downgrades go through `_require_target_valid`, so a converted payload the older schema refuses fails as `ADCPVersionConversionError` (#1621 / #1630). | done (1); per-field rules below are (3) |
| Restricted servers | A server that lists `supported_versions` without `adcp_version` now pins to the newest listed release (`version_policy.default_adcp_version`), instead of refusing because the SDK default (3.3-beta.0) is outside the list. | done (1) |
| Handler gate | `PlatformHandler.get_adcp_capabilities` used `startswith("3.2")` for the proposal-manager projection; it is now "3.2 or later". Audit every other `"3.2"` literal (`accounts.py`, `dispatch.py`, `signing/*`, `server/principal.py accepted_async_adcp_versions`) for 3.3 inclusion. | (3) |
| `adcp_version` on `list_products` responses | `ListProductsResponse1/2` gained `adcp_version`. | generated |
| `search_brands` | `SearchBrandsRequest` gained `adcp_major_version`. | generated |

## 2. New tasks

| Task | Schemas | Notes | SDK status |
| --- | --- | --- | --- |
| `list_collections` | `media-buy/list-collections-request.json`, `-response.json` | Experimental collection cards: filters (`platform`, `genres_include`, `talent`), `cache_scope`, `outcome`. Lifecycle tool under `media_buy.collection_lists`. | types generated; client method, server tool registration, A2A skill, MCP tool list: (3) |
| `list_proposals` | `media-buy/list-proposals-request.json`, `-response.json` | Experimental `media_buy.open_opportunities`: `Opportunity`, `Proposal`, `Origin`, `CloseReason`, `Status`; filters by `media_buy_id`, `opportunity_id`, `proposal_id`. Added to `LifecycleTool` and to `account-change.json` `Task`. | types generated; client/server surface: (3) |
| `get_property_list` over MCP | existing schema | Property list references resolve through AdCP tasks at `agent_url`, `auth_token` as Bearer, every page fetched and validated before a cached snapshot is replaced. | `property_lists` resolution review: (3) |
| `proposal.created` webhook | `core/proposal-created-webhook.json`; `NotificationType` / `EventType` `proposal_created` | Seller-initiated proposals under `open_opportunities`. | types generated; webhook dispatch and `notification_configs` event validation: (3) |
| `refine_proposals` submitted response | `core/async_response_refs/media_buy/refine_proposals_async_response_submitted.json` | New async `submitted` envelope for refinement with `expected_confirmation_at`. | types generated; async response dispatch table: (3) |

## 3. Changed tasks and request/response fields

### Media buy lifecycle

| Field | Where | Rule | SDK status |
| --- | --- | --- | --- |
| `expected_confirmation_at` | `submitted` envelopes of `create_media_buy`, `buy_products`, `accept_proposal`, `refine_proposals`; `MediaBuy` on `get_media_buys` | Estimate. Send on `accept_proposal`, `buy_products`, `refine_proposals` only to 3.3 peers (safe on `create_media_buy`). | generated; downgrade drop rule: (3) |
| `predecessor_media_buy_id`, `lineage_reason` | `CreateMediaBuyRequest`, `BuyProductsRequest`, `AcceptProposalRequest`, `MediaBuy`, `GetMediaBuysRequest` filter; enum `media-buy-lineage-reason` | Experimental lineage. | generated |
| `commercial_terms.definition_pins[]` | `media-buy/commercial-terms.json` via `core/definition-pin.json`; enum `definition-pin-ref-kind` | Required for callers negotiating 3.3. Drift returns `unable` with `definition_changed` and `drifted_pins[]` (`refine_proposals` `Results*`), or `REFERENCE_DEFINITION_CHANGED` before `accept_proposal` mutates. Never sent to callers below 3.3. | generated; pin minting, drift detection, downgrade stripping: (3) |
| `criteria.outcome_target.goal` `kind: "vendor_metric"` | `media-buy/outcome-target.json` `Goal2`; `vendor_metric_values[]`; capability `VendorMetricOptimization.supported_metrics` | Buyer sends only to 3.3 peers. | generated; gate: (3) |
| `criteria.product_ids_exclusive`, `terms_only` finalize, `committed` proposals | `product-discovery-criteria.json`; capability `media_buy.supports_exclusive_product_ids` | Experimental. | generated |
| `hold_policy`, `extend_hold`, `holds`, `hold_rank`, `hold_placed_at`, `proposal_initiator` | `core/hold-policy.json`; `Product`, `CanonicalProduct`, `Proposal` (`refine_proposals`), `ProposalRefinement.Action.extend_hold`; capabilities `proposal_refinement.hold_extension`, `hold_semantics` | Experimental holds. `hold_extension` has a schema default `false`, so the generated capability model emits it. | generated |
| `include_collections` / `collections[]` | `get_products`, `list_products`, `request_proposals`, `refine_proposals` requests and responses; `GetBrandIdentityResponse.collections`; `Collection` gains `images`, `sample_content`, `url`, `publisher_domain`; `CollectionDistribution.followers` | Experimental collection cards. `include_collections` defaults to `false` and is materialized on dump; the 3.3 → 3.2 downgrade drops it with a recorded loss. | generated |
| `later_proposals` | `request_proposals` responses, `OpportunityContext`, `core/later-proposals-state.json`, enum `later-proposal-reason` | Experimental `open_opportunities`. | generated |
| `mutation_outcome` | `core/error.json` (`not_applied`, `unknown`, `applied`) | Every error can state whether the mutation applied. | generated; populate from the dispatcher on mutation errors: (3) |
| `operator_fee` | enum `adjustment-kind` | New price adjustment kind; send only to 3.3 peers. | generated |
| `change_cutoff` | `core/installment-deadlines.json` | Installment change cutoff. | generated |
| `budget.periods[]`, `budget_period_id` | `governance/sync-plans-request.json` `Period`; `check_governance` request/response; capability `governance.supports_budget_periods`; `x-entity-types` `governance_budget_period` | Experimental budget periods; buyer sends `periods[]` only to 3.3 peers. | generated; governance conversion: (3) |
| `history[].origin` | `get_media_buys` `HistoryItem.origin` (`seller_operator`, `seller_system`, `connected_platform`, `unknown`) | Seller `revision` advances on every committed change (DR-0031); buyers pass the latest revision and expect `CONFLICT`. | generated |
| `creative_assignments_as_of`, `approval_status_as_of` | `get_media_buys` `Package`, `CreativeApproval`; `list_creatives` `AssignedPackage` | Timestamps. | generated |
| `landing_page_url`, `trackers` on assignments | `core/creative-assignment.json`, `core/assignment-tracker.json`; `sync_creatives` `Assignment` / `AssignmentOperations1`; `list_creatives` `AssignedPackage`; `get_media_buys` `CreativeApproval`; capabilities `creative.supports_assignment_landing_page`, `creative.supports_assignment_trackers` | Per-assignment overrides. | generated |
| `sync_creatives` `merges[]`, `AssignmentOperations4` | `sync_creatives` response `Merge`; error `CREATIVE_MERGE_REJECTED` with `error-details/creative-merge-rejected.json`; enum `creative-merge-rejection-reason`; capability `creative.supports_assignment_merge` | Creative merge. | generated; error-details narrowing: (3) |
| `list_creatives` `platform_id`, `provenance`, `seller_revision`, `status_as_of`; request filter `provenance` | `creative/list-creatives-response.json`; `x-entity-types` `creative_seller_revision`; capability `creative.stable_creative_ids` | `platform_id` is native in 3.3 (adcp#8175). There was no forward-compat patch for it in this SDK; nothing to retire. | generated |
| `validate_input` `Warning.field` | `creative/validate-input-result.json` | `field` is now declared as a string (#8212). | generated |
| `build_creative` `errors` | `BuildCreativeResponse1/3` | Errors array on build responses. | generated |
| `pixel_tracker` events | enum `pixel-tracking-event`: `start`, `first_quartile`, `midpoint`, `third_quartile`, `complete` | Send only to 3.3 peers. Interim form for 3.2 peers: `event: "custom"` with `custom_event_name`; the 3.3 → 3.2 downgrade may rewrite to that form. Downgrade to 3.0 `tracker_pixel` drops them with `PIXEL_TRACKER_LOSSY_DOWNGRADE`, never as `impression_tracker`. | generated; downgrade rewrite: (3) |
| `daypart_targets[].start_time` / `end_time`, `time_granularity` | `core/daypart-target.json`; `targeting-overlay-requirements.json` `DaypartRequirement1`; `targeting-overlay-support.json` `DaypartSupport1`; enum `daypart-time-granularity`; capability `media_buy.daypart_granularity` | Minute-level dayparts; `start_hour` / `end_hour` become optional, one complete pair per entry. Send only to 3.3 peers. | generated; overlay validation of "one complete pair": (3) |
| `collection_lists`, `collection_lists_exclude` | `TargetingOverlay`, `TargetingOverlayInput`, `TargetingOverlayRequirements`, `TargetingOverlaySupport` (`CollectionListsSupport`) | Experimental. | generated; `_forward_compat._patch_targeting_overlay` still bridges rc.3 inputs: review (2) |
| `dooh_placement_attributes.location`, `dooh_inventory_summary.venue_counts[]` | `core/placement.json`, `placement-definition.json`, `canonical-placement.json` (`*DoohAddress`, `*DoohLocation`); `core/dooh-inventory-summary.json` on `Product` / `CanonicalProduct` | Structured DOOH; strict (`lon`, not `lng`). Seller sends `dooh_inventory_summary` only to 3.3 peers. | generated; canonical product projection: (3) |
| `Talent.brand_ref`, `Brand.prohibited_terms` (`ProhibitedTerm`), `AssetContentType.video_brief`, `core/assets/video-brief-asset.json` | brand discovery, asset unions | New brand and asset shapes; `video_brief` joins the open asset union vocabulary (`scripts/post_generate_fixes.py` `_OPEN_ASSET_UNIONS`). | generated; verify `asset_type` vocabulary derivation picked up `video_brief`: (2) |
| `DisplayTagAsset`, `HtmlAsset`, `ZipAsset` `width`, `height`, `sizes[]` (`Size`) | `core/assets/*` | Dimensions on tag/HTML/zip assets. | generated |
| `FormatKind` `coordinated_placements`, `seller_rendered_stateful_display`; `formats/canonical/coordinated_placements.json` `Container1`, `Loudness`, `Standard` | canonical formats | New canonical format kinds; `is_canonical_format_kind` vocabulary. | generated; canonical format registry: (3) |
| `rights_constraint.channels`, `excluded_channels`; error `RIGHTS_CHANNEL_VIOLATION` | `core/rights-constraint.json`, `enums/error-code.json` | Channel-scoped rights. | generated |
| `x-entity-types` | `+creative_seller_revision`, `+governance_budget_period`, `-evaluator` | Entity vocabulary. | generated |

### Reporting and delivery

| Field | Where | Rule | SDK status |
| --- | --- | --- | --- |
| `impressions` optional | every delivery-metrics row (`DeliveryMetrics` siblings: `by_*`, `CatalogItem`, `Collection*`, `Creative`, `Geo`, `Installment*`, `Keyword`, `Placement*`, `Property`), `get_media_buy_delivery` `By*Item` | Absence means not measured, never zero. `aggregated_totals` still requires it. Receivers must not treat absence as zero. | generated; `adcp.reporting` readers and the storage package's row codecs: (3) |
| `by_spot[]` `spot_status`, `scheduled_at`, `replaces_spot_id`, `break_position` | `get_media_buy_delivery` `BySpotItem`; enum `spot-status`; `SpotReportingCapability.available_statuses`, `supports_break_position` | Experimental spot lifecycle; `aired_at` required only on `aired` / `makegood` rows and rows without `spot_status`. Send `scheduled` / `preempted` rows only to 3.3 peers. | generated; downgrade rule: (3) |
| `print_metrics` | `core/delivery-metrics.json` `PrintMetrics`, `CirculationBasis` (`audited`, `seller_modeled`), tearsheet `evidence`; `AvailableMetric.print_metrics`, `.publication` | On `by_installment` rows. | generated |
| `installment_status`, `publication` | `InstallmentDeliveryMetrics`; `core/installment-publication.json`; `ReportingCapabilities.available_installment_statuses` | Experimental. | generated |
| `projection` | `by_package[]` `DeliveryProjection`; enums `delivery-projection-basis`, `delivery-projection-end-state` | Experimental. | generated |
| `by_property`, `by_installment_property` | `MediaBuyFeatures.supports_property_breakdown`, `supports_installment_property_breakdown` | Property-grain rollups. | generated; storage package rollups: (3) |
| Reliable Reporting `source.custody` | `core/reporting-report-definition.json` `Source.custody`; enum `reporting-custody` (`system_of_record`, `upstream_derived`) | | generated; `adcp.reporting` ledger: (3) |
| `reporting_webhook.operation_id` | `core/reporting-webhook.json` (`minLength` 1, `maxLength` 255, `^[A-Za-z0-9_.:-]{1,255}$`) | Native in 3.3 (adcp#7895); echoed verbatim; nothing is sent to a registration without it. The 3.2 forward-compat runtime patch in `_forward_compat.py` is retired. | done (1) |
| `reporting_webhook.authentication`, `artifact_webhook.authentication` optional | `core/reporting-webhook.json` `required` is `url`, `reporting_frequency`; `create_media_buy` `ArtifactWebhook` | Required only on requests served below 3.3; buyers pinned to 3.2 keep sending them. The 3.3 → 3.2 downgrade of a request without `authentication` must fail structured (#1621). | generated; downgrade rule: (3) |
| `signing_profile` | `ReportingWebhook`, `PushNotificationConfig`, `NotificationConfig`, `AgentNotificationConfig`, `ArtifactWebhook`; enum `webhook-signing-profile` | Per-registration webhook signing profile (Web Bot Auth or RFC 9421). | generated; webhook sender profile selection: (3) |
| Multi-qualifier metrics omit flat totals | `totals` on buys, windows and webhook per-buy | Receivers ignore `aggregated_totals` in delivery webhooks. | (3) |
| `sync_audiences` `audience_size`, `targetable_size`, `match_rate_basis`, `platform_id` | `core/audience-size.json`; enums `audience-size-precision`, `audience-match-rate-basis` | Ranges; `matched_count` / `effective_match_rate` may be absent on `ready` audiences. | generated |
| `sync_event_sources` `platform_id`, `archived` | request/response `EventSource`; capability `media_buy.event_source_archive` | `platform_id` dual-emitted with deprecated `seller_id`. | generated |
| Response size default 4 MiB | client receipt bound (was 1 MiB); SSE per event and cumulative; discovery surfaces small (`tools/list` < 128 KiB, `get_adcp_capabilities` < 64 KiB) | `ADCPClient` default `max_response_bytes` (#1504 capped responses). | raise default to 4 MiB: (3) |
| Inbound body bound | webhook and A2A push receivers reject over-limit bodies with HTTP 413 before signature verification; senders do not retry 413 | | (3) |

### Signals

| Field | Where | SDK status |
| --- | --- | --- |
| `signal_status`, `source_audience_id`, `age_determination_bases` | `get_signals` request `Field1` and response `Signal`; `SignalDefinition`, `SignalDefinitionEnrichment`, `SignalListing`; enum `signal-status`; capability `signals.features.signal_lifecycle` | generated; interim `ext.adcp.age_determination_bases` for 3.2 peers (top-level wins): (3) |
| `signals.data_provider_domains_complete`, `media_buy.portfolio.publisher_domains_complete` | capabilities; byte budgets 8,192 per field / 16,384 combined, 100 entries, 253 chars each; absence is never a denial | generated; bound enforcement in `property_lists` / portfolio readers: (3) |

### Accounts and identity

| Field | Where | Rule | SDK status |
| --- | --- | --- | --- |
| `account.stable_account_id`, `account_id` from `sync_accounts` | capabilities `Account.stable_account_id` | Natural-key references after provisioning are deprecated. | generated; `adcp.accounts` reference resolution: (3) |
| Discovery never provisions | `get_products`, `list_products`, `get_signals`, `request_proposals`, `refine_proposals`, `decline_proposals` MUST NOT create or activate an account; unresolved `account` → `ACCOUNT_NOT_FOUND`; `list_accounts` treats `account` as a filter | Storyboard `media_buy_seller/unprovisioned_account_reference` (advisory until runner 14.1.0). | decisioning account resolution: (3) |
| `account-change.json` `idempotency_key`; `changed_paths` required for `adcp` / `seller_operator` origins | `AccountChange.idempotency_key` | Echoed to the sending principal only. | generated; account change feed: (3) |
| `account.agent_onboarding`, `get_principal` `relationship`, `identity.trust_json`, `identity-relying-party` | `core/agent-onboarding.json`, `-tier.json`, `-requirement.json`, `core/buyer-onboarding-posture.json`, `core/principal-relationship.json`, `trust/v1/trust.json`, `trust/v1/trust-acknowledgements.json`; enums `agent-onboarding-mode`, `agent-onboarding-requirement-kind` (x-extensible), `principal-relationship-status`; `PrincipalChangedWebhook.Reason` `relationship_changed`, `relationship_expiring`; `get_principal` `Result.errors` | Experimental. The identity preview (`_identity_preview.py`, vendored `_identity_schemas/`, `generate_identity_preview.py`) is retired: `adcp.types.experimental_identity` binds the generated 3.3 types, `AgentOnboardingRequirement.kind` stays open (`enum | str`), and `adcp.signing.trust` validates against the bundle through `get_named_validator`. | done (1) |
| `adcp.implementation.sdks[]` | capabilities `Adcp.implementation` (`Implementation`, `Sdk`, `Component`) | Vendors move data out of `ext.adcp`. | generated; advertise this SDK: (3) |
| `security_profiles` | `GetAdcpCapabilitiesResponse.security_profiles` (`adcp-prod-security-3.3`, experimental) | | generated |
| `request_signing.profiles`, `operation_sources`; `webhook_signing.profiles` | capabilities `RequestSigning.profiles` (`adcp/request-signing/v1`, `web-bot-auth`), `operation_sources` (`mcp_tools_call`, `a2a_invocation_skill`) | `required_for` enforced over A2A with `operation_sources` as the advertisement. | generated; `adcp.signing.negotiated_profiles` and A2A enforcement: (3) |
| `ext.adcp` reserved | `extensions/adcp.json` `AdcpOwnedExtensions` | Reserved namespace for the working group. | generated |
| `media_buy.features.catalog_ingestion` | `core/catalog-ingestion-capability.json` (`IngestionMode`, `ItemStatusReporting`); requires `catalog_management: true` | An object: fails 3.2 capability schemas (booleans only); the 3.3 → 3.2 downgrade must coarsen or drop it. `media_buy.features` is no longer boolean-only. | generated; downgrade rule: (3) |
| `error-details/unsupported-targeting-dimension.json` | under `UNSUPPORTED_FEATURE`; adapters minting `UNSUPPORTED_TARGETING_DIMENSION` / `DEPRECATED_TARGETING_DIMENSION` should migrate | | generated; decisioning error minting: (3) |
| `error-details/agent-permission-denied.json` `Reason` | new nested enum | | generated |
| `brand.json` fetch cap 2 MiB | all fetch paths; `MAX_JWKS_BYTES`, `MAX_CAPABILITIES_BYTES` unchanged | 3.2 verifiers may reject files between 256 KiB and 2 MiB. | `adcp.signing.brand_jwks` cap: (3) |
| Property `domain` / `subdomain` lowercase ASCII hostnames | property schemas; invalid values → `INVALID_REQUEST` | | validation: (3) |
| One agent-resolution rule | request signing, webhook signing, governance JWS `iss`, response signing, rights attestations; pinned `signing_keys` published in JWKS; RFC 7638 thumbprints; `key_origins`; `agent_url_match: canonical` | | `adcp.signing` audit: (3) |
| RFC 9421 profile deprecation | `adcp/request-signing/v1`, `adcp/webhook-signing/v1` deprecated (removal no earlier than 4.0) in favour of Web Bot Auth | `rfc9421_deprecation_active()` keys on a 3.3 line in `COMPATIBLE_ADCP_VERSIONS`, so SDK 10 warns by default on legacy `SigningConfig` / `RequestSignatureVerification` construction. | done (1) |

## 4. Capabilities (`get_adcp_capabilities`)

Every new declaration is optional; 3.2 behaviour stands when undeclared.

| Path | Added | SDK status |
| --- | --- | --- |
| `adcp.implementation` | `Implementation` (`sdks[]` of `Sdk` / `Component`) | generated |
| `security_profiles` | `SecurityProfile[]` | generated |
| `account` | `agent_onboarding`, `stable_account_id` | generated |
| `creative` | `stable_creative_ids`, `supports_assignment_landing_page`, `supports_assignment_merge`, `supports_assignment_trackers`, `broadcast_loudness` | generated |
| `governance` | `supports_budget_periods` | generated |
| `media_buy` | `event_source_archive`, `open_opportunities` (`OpenOpportunities`, `LaterProposals`), `supports_exclusive_product_ids`, `collection_lists`, `delegated_composition`, `daypart_granularity`, `features.catalog_ingestion`, `features.supports_property_breakdown`, `features.supports_installment_property_breakdown`, `lifecycle_tools` `list_proposals` | generated |
| `media_buy.proposal_refinement` | `hold_extension` (default `false`), `hold_semantics` (`HoldSemantics`) | generated; the default is materialized on dump (#1613 class) |
| `media_buy.portfolio` | `publisher_domains_complete` | generated |
| `signals` | `data_provider_domains_complete`, `features.signal_lifecycle`, `age_determination_bases` | generated |
| `request_signing` | `operation_sources[]`, `profiles[]` | generated |
| `webhook_signing` | `profiles[]` | generated |
| `vendor_metric_optimization` | `supported_metrics[]` | generated |
| `reporting_capabilities` | `available_installment_statuses[]`; `spot_reporting.available_statuses[]`, `supports_break_position` | generated |
| `supported_indicator_types` / `indicator_type` | `flight_extension_opportunity`, `scale_budget_opportunity` | generated |
| `notification event types` | `collection_card_removed`, `collection_card_updated`, `collection_catalog_bulk_change`, `proposal_created`; wholesale feed `EntityType.collection`, `WholesaleFeedEvent10/11/12`, `Payload26/27/28` | generated |
| specialisms | `sales_fixed_rate` (new specialism for fixed-price, non-guaranteed sellers); `sales-broadcast-tv` 1.1.0 | generated; `adcp.decisioning.specialisms`: (3) |
| `canonical_creatives` | still not declared by 3.3 | the `_forward_compat` capability patch stays |

## 5. Error codes and error details

| Code / detail | Schema | Where it fires | SDK status |
| --- | --- | --- | --- |
| `REFERENCE_DEFINITION_CHANGED` | `error-details/reference-definition-changed.json` | `accept_proposal` when a pinned definition drifted; send only to 3.3 peers | generated; minting and 3.2 downgrade mapping: (3) |
| `CREATIVE_MERGE_REJECTED` | `error-details/creative-merge-rejected.json` (`Rejection`, reason enum) | `sync_creatives` merges | generated; (3) |
| `RIGHTS_CHANNEL_VIOLATION` | `enums/error-code.json` | rights constraints `channels` / `excluded_channels` | generated; (3) |
| `UNSUPPORTED_FEATURE` + `unsupported-targeting-dimension` | `error-details/unsupported-targeting-dimension.json` (`SupportedDimension`) | replaces adapter-minted `UNSUPPORTED_TARGETING_DIMENSION` / `DEPRECATED_TARGETING_DIMENSION` | generated; (3) |
| `mutation_outcome` | `core/error.json` | every error (`not_applied`, `unknown`, `applied`) | generated; (3) |
| `definition_changed` | `enums/proposal-refinement-reason.json` | `refine_proposals` `unable` with `drifted_pins[]` | generated |
| `REVISION_UNREADABLE` with `reporting_revision_id` | 3.2.4 | already in SDK 9 | done |
| `request_body_malformed` | A2A hardening (strict unsigned-body parsing, Part shape rules, task-continuation binding) becomes MUST | | A2A server: (3) |
| `request_target_uri_malformed` | conformance vector `negative/026-non-ascii-host` re-coded | adopted with 3.2.4 (#1479) | done |
| `TERMS_REJECTED` | must not fire for replayed advertised measurement terms; windows reference `reporting_capabilities.measurement_windows[].window_id` | | decisioning: (3) |
| `COMPLIANCE_UNSATISFIED` | `build_creative` refinement that narrows inherited `required_disclosures` / `prohibited_claims` | | (3) |
| `ACCOUNT_NOT_FOUND` | discovery with an unresolved `account` | | decisioning: (3) |

## 6. Enum changes

| Enum | Change |
| --- | --- |
| `universal-macro` | Now `anyOf[enum, pattern ^GDPR_CONSENT_[1-9][0-9]*$]`; `ADDTL_CONSENT` added. Generated as `UniversalMacro(RootModel[UniversalMacro3 | UniversalMacro4])`: the closed enum is `UniversalMacro3`, the pattern string `UniversalMacro4`. **Breaking for code that used `UniversalMacro.MEDIA_BUY_ID`**; an alias for the enum belongs in `aliases.py` (2). |
| `demographic-system` | `nmo`, `videoamp` |
| video `Container` | `mxf`; `VideoCodec` `avc_intra`, `mpeg2`, `xavc`; `loudness` object (`Loudness`, `Standard`) on hosted video and coordinated placements |
| `pixel-tracking-event` | `start`, `first_quartile`, `midpoint`, `third_quartile`, `complete` |
| `adjustment-kind` | `operator_fee` |
| `available-metric` | `print_metrics`, `publication` |
| `error-code` | `CREATIVE_MERGE_REJECTED`, `REFERENCE_DEFINITION_CHANGED`, `RIGHTS_CHANNEL_VIOLATION` |
| `indicator-type` | `flight_extension_opportunity`, `scale_budget_opportunity` |
| `notification-type` / event types | `collection_card_removed`, `collection_card_updated`, `collection_catalog_bulk_change`, `proposal_created` |
| `proposal-refinement-reason` | `definition_changed` |
| `specialism` | `sales_fixed_rate` |
| `asset-content-type` | `video_brief` |
| `format-kind` (canonical) | `coordinated_placements`, `seller_rendered_stateful_display` |
| `product-card-reference-asset` `Role` | `sample_content` |
| `principal-changed-webhook` `Reason` | `relationship_changed`, `relationship_expiring` |
| `x-entity-types` | `+creative_seller_revision`, `+governance_budget_period`, `-evaluator` |
| new enums | `agent-onboarding-mode`, `agent-onboarding-requirement-kind`, `audience-match-rate-basis`, `audience-size-precision`, `creative-merge-rejection-reason`, `daypart-time-granularity`, `definition-pin-ref-kind`, `delivery-projection-basis`, `delivery-projection-end-state`, `later-proposal-reason`, `media-buy-lineage-reason`, `principal-relationship-status`, `proposal-disposition`, `reporting-custody`, `signal-status`, `spot-status`, `webhook-signing-profile` |
| x-extensible enums generated closed | `agent-onboarding-requirement-kind`, `advertiser-industry`, `media-buy-action-mode` carry `x-extensible: true` but generate as closed `StrEnum`s. The identity facade widens `kind` by hand; a generator rule for `x-extensible` is (2). |

## 7. Removals and deprecations

| Item | Detail | SDK status |
| --- | --- | --- |
| `evaluator_id` source form removed | `core/evaluator-spec.json`: `EvaluatorSpec3` gone, `EvaluatorSpec2` gains `agent_url` and loses `evaluator_id`; `list_evaluators` withdrawn; the seller's evaluator goes in `creative_policy.accepted_verifiers[]`. No alias. | generated; any SDK helper that minted `evaluator_id`: (3); migration note in `MIGRATION_v9_to_v10.md` |
| `seller_id` on event source and audience results | deprecated for `platform_id`; sellers emit both with identical values | generated; dual-emission on the seller side: (3) |
| Natural-key `AccountRef` after provisioning | deprecated; `account_id` with `account.stable_account_id` | (3) |
| RFC 9421 signing profiles | deprecated for Web Bot Auth; verifiers keep accepting through 3.x | warning: done (1) |
| Reporting snapshot compaction | experimental notice: superseded snapshot content may be compacted after retention; reads return `REFERENCE_NOT_FOUND` with `field: "reporting_revision_id"`; unowed revisions surface as `REVISION_UNREADABLE` | `adcp.reporting` / storage package: (3) |

## 8. Behavioural rules with no schema change

- Provisional buys (`confirmed_at: null`) stay `pending_start` until confirmed.
- Refinement carries compliance forward (`required_disclosures`, `prohibited_claims`); refined leaves get new `creative_id`s and are not auto-approved.
- Audience identifiers keep their wire form: `uids[].value` for `uid2` / `euid` raw, `hashed_email` agrees with the top-level field, `maid` lowercase hyphenated UUID.
- Responses conform to the served release (DR-0024); sellers stop listing releases they cannot serve; buyers parse tolerantly.
- Signing buyers answer `get_adcp_capabilities` at their agent URL.
- A2A invocation messages with more than one DataPart are rejected; the last-DataPart rule applies to responses only.
- Compliance: `version_negotiation` gains an advisory 3.1-pinned response-shape scenario; new scenarios `media_buy_seller/stable_account_id`, `media_buy_seller/definition_pin_drift`, `governance/budget_periods`, property and outcome-target scenarios. Beta.0 issues no 3.3 badge. The packaged compliance assets are `src/adcp/_compliance/3.3.0-beta.0/` (4443 manifest files, 242 provenance files).

## 9. Codegen notes (workstream 2 input)

- Regeneration against `schemas/cache/3.3.0-beta.0` changes 181 generated files and adds 44 (1135 total). The delta is semantic, not a renumber: the numbered-class churn it contains (`Action23 → Action25`, `Status22 → Status20`, `Result5 → Result4`, `Assets31 → Assets30/52/53`, `SupportedTarget5 → 6`, `Proposal3 → Proposal4`, `Status51 → 50`, `Status46 → 45`, `Action33 → 35`, `Action9 → 10`, `Action11 → 12`) is a consequence of new sibling schemas and cannot be discarded. No hand-written module referenced a removed numbered name; `aliases.py` resolved cleanly.
- `UniversalMacro` is now a `RootModel` union (above). `error_details.py` gained `UniversalMacro3` and `ReasonFromAgentPermissionDenied`.
- Provenance is hoisted to `#/$defs/Provenance` in bundles; envelope composition goes through `core/version-envelope.json`.
- Schema defaults materialized on dump now include `include_collections: false` on discovery requests and `hold_extension: false` on `proposal_refinement` (the `default_emitted` class, #1613). The wire round-trip conformance suite from #1617 runs against the 3.3 bundle.
- `make validate-generated` (`generate_types.py --check`, versioned stubs) is deterministic on the 3.3 tree. The versioned view modules (`adcp.types.v30/v31/v32`, `versioned_bases`) have no `v33` yet; `scripts/generate_versioned_stubs.VERSIONS` would need `"v33": "3.3-beta.0"`.
- `skills/adcp-media-buy/SKILL.md` and the request-signing conformance vectors (`tests/conformance/vectors/request-signing/`, 130 files, manifest pinned) came from the 3.3 bundle.
