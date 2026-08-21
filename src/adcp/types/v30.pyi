"""Generated typing surface for the dynamic version-scoped models."""

from __future__ import annotations

import builtins
from typing import Any, Literal, overload
from typing_extensions import Never, NotRequired, Required, TypedDict

from adcp.types.versioned import VersionedSchemaModel

# Underscored TypedDicts are private stub details used to type nested values.
# Runtime composition remains dict-based through the public boundary models.

class _ExternalCoreBrandRef(TypedDict, total=False):
    domain: Required[builtins.str]
    brand_id: NotRequired[builtins.str]
    industries: NotRequired[builtins.list[builtins.str]]
    data_subject_contestation: NotRequired[_ExternalCoreBrandRefDataSubjectContestation]

class _AcquireRightsRequestCampaign(TypedDict, total=False):
    description: Required[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    estimated_impressions: NotRequired[builtins.int]
    start_date: NotRequired[builtins.str]
    end_date: NotRequired[builtins.str]

class _ExternalCorePushNotificationConfig(TypedDict, total=False):
    url: Required[builtins.str]
    token: NotRequired[builtins.str]
    authentication: NotRequired[_ExternalCorePushNotificationConfigAuthentication]

class _ExternalBrandRightsTerms(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    amount: Required[builtins.float]
    currency: Required[builtins.str]
    period: NotRequired[Literal['daily', 'weekly', 'monthly', 'quarterly', 'annual', 'one_time']]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    impression_cap: NotRequired[builtins.int]
    overage_cpm: NotRequired[builtins.float]
    start_date: NotRequired[builtins.str]
    end_date: NotRequired[builtins.str]
    exclusivity: NotRequired[_ExternalBrandRightsTermsExclusivity]

class _ExternalCoreGenerationCredential(TypedDict, total=False):
    provider: Required[builtins.str]
    rights_key: Required[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    expires_at: NotRequired[builtins.str]
    endpoint: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _AcquireRightsResponseDisclosure(TypedDict, total=False):
    required: Required[builtins.bool]
    text: NotRequired[builtins.str]

class _ExternalCoreRightsConstraint(TypedDict, total=False):
    rights_id: Required[builtins.str]
    rights_agent: Required[_ExternalCoreRightsConstraintRightsAgent]
    valid_from: NotRequired[builtins.str]
    valid_until: NotRequired[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    excluded_countries: NotRequired[builtins.list[builtins.str]]
    impression_cap: NotRequired[builtins.int]
    right_type: NotRequired[Literal['talent', 'character', 'brand_ip', 'music', 'stock_media']]
    approval_status: NotRequired[Literal['pending', 'approved', 'rejected']]
    verification_url: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreError(TypedDict, total=False):
    code: Required[builtins.str]
    message: Required[builtins.str]
    field: NotRequired[builtins.str]
    suggestion: NotRequired[builtins.str]
    retry_after: NotRequired[builtins.float]
    issues: NotRequired[builtins.list[_ExternalCoreErrorIssuesItem]]
    details: NotRequired[builtins.dict[builtins.str, Any]]
    recovery: NotRequired[Literal['transient', 'correctable', 'terminal']]

class _ActivateSignalRequestDestinationsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]

class _ActivateSignalRequestDestinationsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]

class _ActivateSignalRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ActivateSignalRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ActivateSignalResponseDeploymentsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_ActivateSignalResponseDeploymentsItemVariant1ActivationKeyVariant1 | _ActivateSignalResponseDeploymentsItemVariant1ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

class _ActivateSignalResponseDeploymentsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_ActivateSignalResponseDeploymentsItemVariant2ActivationKeyVariant1 | _ActivateSignalResponseDeploymentsItemVariant2ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

class _ExternalCoreCreativeManifest(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    assets: Required[builtins.dict[builtins.str, Any]]
    rights: NotRequired[builtins.list[_ExternalCoreRightsConstraint]]
    industry_identifiers: NotRequired[builtins.list[_ExternalCoreIndustryIdentifier]]
    provenance: NotRequired[_ExternalCoreProvenance]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatId(TypedDict, total=False):
    agent_url: Required[builtins.str]
    id: Required[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    duration_ms: NotRequired[builtins.float]

class _BuildCreativeRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _BuildCreativeRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _BuildCreativeRequestPreviewInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _BuildCreativeResponsePreview(TypedDict, total=False):
    previews: Required[builtins.list[_BuildCreativeResponsePreviewPreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

class _ExternalCoreCreativeConsumption(TypedDict, total=False):
    tokens: NotRequired[builtins.int]
    images_generated: NotRequired[builtins.int]
    renders: NotRequired[builtins.int]
    duration_seconds: NotRequired[builtins.float]

class _BuildCreativeResponsePreview2(TypedDict, total=False):
    previews: Required[builtins.list[_BuildCreativeResponsePreview2PreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

class _ExternalContentStandardsArtifact(TypedDict, total=False):
    property_rid: Required[builtins.str]
    artifact_id: Required[builtins.str]
    variant_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    url: NotRequired[builtins.str]
    published_time: NotRequired[builtins.str]
    last_update_time: NotRequired[builtins.str]
    assets: Required[builtins.list[_ExternalContentStandardsArtifactAssetsItemVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant3 | _ExternalContentStandardsArtifactAssetsItemVariant4]]
    metadata: NotRequired[_ExternalContentStandardsArtifactMetadata]
    provenance: NotRequired[_ExternalCoreProvenance]
    identifiers: NotRequired[_ExternalContentStandardsArtifactIdentifiers]

class _CalibrateContentResponseFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    status: Required[Literal['passed', 'failed', 'warning', 'unevaluated']]
    policy_id: NotRequired[builtins.str]
    explanation: NotRequired[builtins.str]
    confidence: NotRequired[builtins.float]

class _ExternalCorePlannedDelivery(TypedDict, total=False):
    geo: NotRequired[_ExternalCorePlannedDeliveryGeo]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    frequency_cap: NotRequired[_ExternalCoreFrequencyCap]
    audience_summary: NotRequired[builtins.str]
    audience_targeting: NotRequired[builtins.list[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant4]]
    total_budget: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]
    enforced_policies: NotRequired[builtins.list[builtins.str]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _CheckGovernanceRequestDeliveryMetrics(TypedDict, total=False):
    reporting_period: Required[_CheckGovernanceRequestDeliveryMetricsReportingPeriod]
    spend: NotRequired[builtins.float]
    cumulative_spend: NotRequired[builtins.float]
    impressions: NotRequired[builtins.int]
    cumulative_impressions: NotRequired[builtins.int]
    geo_distribution: NotRequired[builtins.dict[builtins.str, builtins.float]]
    channel_distribution: NotRequired[builtins.dict[builtins.str, builtins.float]]
    pacing: NotRequired[Literal['ahead', 'on_track', 'behind']]
    audience_distribution: NotRequired[_CheckGovernanceRequestDeliveryMetricsAudienceDistribution]

class _ExternalCoreBusinessEntity(TypedDict, total=False):
    legal_name: Required[builtins.str]
    vat_id: NotRequired[builtins.str]
    tax_id: NotRequired[builtins.str]
    registration_number: NotRequired[builtins.str]
    address: NotRequired[_ExternalCoreBusinessEntityAddress]
    contacts: NotRequired[builtins.list[_ExternalCoreBusinessEntityContactsItem]]
    bank: NotRequired[_ExternalCoreBusinessEntityBank]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _CheckGovernanceResponseFindingsItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    policy_id: NotRequired[builtins.str]
    source_plan_id: NotRequired[builtins.str]
    severity: Required[Literal['info', 'warning', 'critical']]
    explanation: Required[builtins.str]
    details: NotRequired[builtins.dict[builtins.str, Any]]
    confidence: NotRequired[builtins.float]
    uncertainty_reason: NotRequired[builtins.str]

class _CheckGovernanceResponseConditionsItem(TypedDict, total=False):
    field: Required[builtins.str]
    required_value: NotRequired[Any]
    reason: Required[builtins.str]

class _ComplyTestControllerRequestParams(TypedDict, total=False):
    creative_id: NotRequired[builtins.str]
    account_id: NotRequired[builtins.str]
    media_buy_id: NotRequired[builtins.str]
    session_id: NotRequired[builtins.str]
    product_id: NotRequired[builtins.str]
    pricing_option_id: NotRequired[builtins.str]
    plan_id: NotRequired[builtins.str]
    fixture: NotRequired[builtins.dict[builtins.str, Any]]
    status: NotRequired[builtins.str]
    rejection_reason: NotRequired[builtins.str]
    termination_reason: NotRequired[builtins.str]
    impressions: NotRequired[builtins.int]
    clicks: NotRequired[builtins.int]
    conversions: NotRequired[builtins.int]
    reported_spend: NotRequired[_ComplyTestControllerRequestParamsReportedSpend]
    spend_percentage: NotRequired[builtins.float]
    arm: NotRequired[Literal['submitted', 'input-required']]
    task_id: NotRequired[builtins.str]
    message: NotRequired[builtins.str]
    format_id: NotRequired[builtins.str]
    result: NotRequired[builtins.dict[builtins.str, Any]]

class _ComplyTestControllerResponseForced(TypedDict, total=False):
    arm: Required[Literal['submitted', 'input-required']]
    task_id: NotRequired[builtins.str]

class _ContextMatchRequestArtifactRefsItem(TypedDict, total=False):
    type: Required[Literal['url', 'url_hash', 'eidr', 'gracenote', 'isrc', 'gtin', 'rss_guid', 'isbn', 'custom']]
    value: Required[builtins.str]

class _ContextMatchRequestGeo(TypedDict, total=False):
    country: NotRequired[builtins.str]
    region: NotRequired[builtins.str]
    metro: NotRequired[_ContextMatchRequestGeoMetro]

class _ContextMatchRequestContextSignals(TypedDict, total=False):
    topics: NotRequired[builtins.list[builtins.str]]
    taxonomy_source: NotRequired[builtins.str]
    taxonomy_id: NotRequired[builtins.int]
    sentiment: NotRequired[Literal['positive', 'negative', 'neutral', 'mixed']]
    keywords: NotRequired[builtins.list[builtins.str]]
    language: NotRequired[builtins.str]
    content_policies: NotRequired[builtins.list[builtins.str]]
    summary: NotRequired[builtins.str]
    embedding: NotRequired[builtins.str]
    embedding_model: NotRequired[builtins.str]
    embedding_dims: NotRequired[builtins.int]

class _ExternalTmpOffer(TypedDict, total=False):
    package_id: Required[builtins.str]
    seller_agent: NotRequired[_ExternalCoreSellerAgentRef]
    brand: NotRequired[_ExternalCoreBrandRef]
    price: NotRequired[_ExternalTmpOfferPrice]
    summary: NotRequired[builtins.str]
    creative_manifest: NotRequired[_ExternalCoreCreativeManifest]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]

class _ContextMatchResponseSignals(TypedDict, total=False):
    segments: NotRequired[builtins.list[builtins.str]]
    targeting_kvs: NotRequired[builtins.list[_ContextMatchResponseSignalsTargetingKvsItem]]

class _CreateCollectionListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _CreateCollectionListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _CreateCollectionListRequestBaseCollectionsItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['distribution_ids']]
    identifiers: Required[builtins.list[_CreateCollectionListRequestBaseCollectionsItemVariant1IdentifiersItem]]

class _CreateCollectionListRequestBaseCollectionsItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_collections']]
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

class _CreateCollectionListRequestBaseCollectionsItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['publisher_genres']]
    publisher_domain: Required[builtins.str]
    genres: Required[builtins.list[builtins.str]]
    genre_taxonomy: Required[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]

class _ExternalCollectionCollectionListFilters(TypedDict, total=False):
    content_ratings_exclude: NotRequired[builtins.list[_ExternalCoreContentRating]]
    content_ratings_include: NotRequired[builtins.list[_ExternalCoreContentRating]]
    genres_exclude: NotRequired[builtins.list[builtins.str]]
    genres_include: NotRequired[builtins.list[builtins.str]]
    genre_taxonomy: NotRequired[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]
    kinds: NotRequired[builtins.list[Literal['series', 'publication', 'event_series', 'rotation']]]
    exclude_distribution_ids: NotRequired[builtins.list[_ExternalCollectionCollectionListFiltersExcludeDistributionIdsItem]]
    production_quality: NotRequired[builtins.list[Literal['professional', 'prosumer', 'ugc']]]

class _ExternalCollectionCollectionList(TypedDict, total=False):
    list_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    account: NotRequired[_ExternalCollectionCollectionListAccountVariant1 | _ExternalCollectionCollectionListAccountVariant2]
    base_collections: NotRequired[builtins.list[_ExternalCollectionCollectionListBaseCollectionsItemVariant1 | _ExternalCollectionCollectionListBaseCollectionsItemVariant2 | _ExternalCollectionCollectionListBaseCollectionsItemVariant3]]
    filters: NotRequired[_ExternalCollectionCollectionListFilters]
    brand: NotRequired[_ExternalCoreBrandRef]
    webhook_url: NotRequired[builtins.str]
    cache_duration_hours: NotRequired[builtins.int]
    created_at: NotRequired[builtins.str]
    updated_at: NotRequired[builtins.str]
    collection_count: NotRequired[builtins.int]

class _CreateContentStandardsRequestScope(TypedDict, total=False):
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    languages_any: Required[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]

class _ExternalGovernancePolicyEntry(TypedDict, total=False):
    policy_id: Required[builtins.str]
    source: NotRequired[Literal['registry', 'inline']]
    version: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    description: NotRequired[builtins.str]
    category: NotRequired[Literal['regulation', 'standard']]
    enforcement: Required[Literal['must', 'should', 'may']]
    requires_human_review: NotRequired[builtins.bool]
    jurisdictions: NotRequired[builtins.list[builtins.str]]
    region_aliases: NotRequired[builtins.dict[builtins.str, builtins.list[builtins.str]]]
    policy_categories: NotRequired[builtins.list[builtins.str]]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    governance_domains: NotRequired[builtins.list[Literal['campaign', 'property', 'creative', 'content_standards']]]
    effective_date: NotRequired[builtins.str]
    sunset_date: NotRequired[builtins.str]
    source_url: NotRequired[builtins.str]
    source_name: NotRequired[builtins.str]
    policy: Required[builtins.str]
    guidance: NotRequired[builtins.str]
    exemplars: NotRequired[_ExternalGovernancePolicyEntryExemplars]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _CreateContentStandardsRequestCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_CreateContentStandardsRequestCalibrationExemplarsFailItemVariant1 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2]]

class _CreateMediaBuyRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _CreateMediaBuyRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _CreateMediaBuyRequestTotalBudget(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

class _ExternalMediaBuyPackageRequest(TypedDict, total=False):
    adcp_major_version: NotRequired[builtins.int]
    product_id: Required[builtins.str]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    budget: Required[builtins.float]
    pacing: NotRequired[Literal['even', 'asap', 'front_loaded']]
    pricing_option_id: Required[builtins.str]
    bid_price: NotRequired[builtins.float]
    impressions: NotRequired[builtins.float]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    catalogs: NotRequired[builtins.list[_ExternalCoreCatalog]]
    optimization_goals: NotRequired[builtins.list[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2]]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    measurement_terms: NotRequired[_ExternalCoreMeasurementTerms]
    performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    creative_assignments: NotRequired[builtins.list[_ExternalCoreCreativeAssignment]]
    creatives: NotRequired[builtins.list[_ExternalCoreCreativeAsset]]
    agency_estimate_number: NotRequired[builtins.str]
    context: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _CreateMediaBuyRequestIoAcceptance(TypedDict, total=False):
    io_id: Required[builtins.str]
    accepted_at: Required[builtins.str]
    signatory: Required[builtins.str]
    signature_id: NotRequired[builtins.str]

class _ExternalCoreReportingWebhook(TypedDict, total=False):
    url: Required[builtins.str]
    token: NotRequired[builtins.str]
    authentication: Required[_ExternalCoreReportingWebhookAuthentication]
    reporting_frequency: Required[Literal['hourly', 'daily', 'monthly']]
    requested_metrics: NotRequired[builtins.list[Literal['impressions', 'spend', 'clicks', 'ctr', 'video_completions', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'viewability', 'engagement_rate', 'views', 'completed_views', 'leads', 'reach', 'frequency', 'grps', 'quartile_data', 'dooh_metrics', 'cost_per_click']]]

class _CreateMediaBuyRequestArtifactWebhook(TypedDict, total=False):
    url: Required[builtins.str]
    token: NotRequired[builtins.str]
    authentication: Required[_CreateMediaBuyRequestArtifactWebhookAuthentication]
    delivery_mode: Required[Literal['realtime', 'batched']]
    batch_frequency: NotRequired[Literal['hourly', 'daily']]
    sampling_rate: NotRequired[builtins.float]

class _ExternalCoreAccount(TypedDict, total=False):
    account_id: Required[builtins.str]
    name: Required[builtins.str]
    advertiser: NotRequired[builtins.str]
    billing_proxy: NotRequired[builtins.str]
    status: Required[Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed']]
    brand: NotRequired[_ExternalCoreBrandRef]
    operator: NotRequired[builtins.str]
    billing: NotRequired[Literal['operator', 'agent', 'advertiser']]
    billing_entity: NotRequired[_ExternalCoreBusinessEntity]
    rate_card: NotRequired[builtins.str]
    payment_terms: NotRequired[Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay']]
    credit_limit: NotRequired[_ExternalCoreAccountCreditLimit]
    setup: NotRequired[_ExternalCoreAccountSetup]
    account_scope: NotRequired[Literal['operator', 'brand', 'operator_brand', 'agent']]
    governance_agents: NotRequired[builtins.list[_ExternalCoreAccountGovernanceAgentsItem]]
    reporting_bucket: NotRequired[_ExternalCoreAccountReportingBucket]
    sandbox: NotRequired[builtins.bool]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCorePackage(TypedDict, total=False):
    package_id: Required[builtins.str]
    product_id: NotRequired[builtins.str]
    budget: NotRequired[builtins.float]
    pacing: NotRequired[Literal['even', 'asap', 'front_loaded']]
    pricing_option_id: NotRequired[builtins.str]
    bid_price: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    impressions: NotRequired[builtins.float]
    catalogs: NotRequired[builtins.list[_ExternalCoreCatalog]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    measurement_terms: NotRequired[_ExternalCoreMeasurementTerms]
    performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    creative_assignments: NotRequired[builtins.list[_ExternalCoreCreativeAssignment]]
    format_ids_to_provide: NotRequired[builtins.list[_ExternalCoreFormatId]]
    optimization_goals: NotRequired[builtins.list[_ExternalCorePackageOptimizationGoalsItemVariant1 | _ExternalCorePackageOptimizationGoalsItemVariant2]]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    canceled: NotRequired[builtins.bool]
    cancellation: NotRequired[_ExternalCorePackageCancellation]
    agency_estimate_number: NotRequired[builtins.str]
    creative_deadline: NotRequired[builtins.str]
    context: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _CreatePropertyListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _CreatePropertyListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _CreatePropertyListRequestBasePropertiesItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['publisher_tags']]
    publisher_domain: Required[builtins.str]
    tags: Required[builtins.list[builtins.str]]

class _CreatePropertyListRequestBasePropertiesItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_ids']]
    publisher_domain: Required[builtins.str]
    property_ids: Required[builtins.list[builtins.str]]

class _CreatePropertyListRequestBasePropertiesItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['identifiers']]
    identifiers: Required[builtins.list[_ExternalCoreIdentifier]]

class _ExternalPropertyPropertyListFilters(TypedDict, total=False):
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    property_types: NotRequired[builtins.list[Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'linear_tv', 'streaming_audio', 'ai_assistant']]]
    feature_requirements: NotRequired[builtins.list[_ExternalCoreFeatureRequirement]]
    exclude_identifiers: NotRequired[builtins.list[_ExternalCoreIdentifier]]

class _ExternalPropertyPropertyList(TypedDict, total=False):
    list_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    account: NotRequired[_ExternalPropertyPropertyListAccountVariant1 | _ExternalPropertyPropertyListAccountVariant2]
    base_properties: NotRequired[builtins.list[_ExternalPropertyPropertyListBasePropertiesItemVariant1 | _ExternalPropertyPropertyListBasePropertiesItemVariant2 | _ExternalPropertyPropertyListBasePropertiesItemVariant3]]
    filters: NotRequired[_ExternalPropertyPropertyListFilters]
    brand: NotRequired[_ExternalCoreBrandRef]
    webhook_url: NotRequired[builtins.str]
    cache_duration_hours: NotRequired[builtins.int]
    created_at: NotRequired[builtins.str]
    updated_at: NotRequired[builtins.str]
    property_count: NotRequired[builtins.int]
    pricing_options: NotRequired[builtins.list[_ExternalPropertyPropertyListPricingOptionsItemVariant1 | _ExternalPropertyPropertyListPricingOptionsItemVariant2 | _ExternalPropertyPropertyListPricingOptionsItemVariant3 | _ExternalPropertyPropertyListPricingOptionsItemVariant4 | _ExternalPropertyPropertyListPricingOptionsItemVariant5]]

class _DeleteCollectionListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _DeleteCollectionListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _DeletePropertyListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _DeletePropertyListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetAccountFinancialsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetAccountFinancialsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalCoreDateRange(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

class _GetAccountFinancialsResponseAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetAccountFinancialsResponseAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetAccountFinancialsResponseSpend(TypedDict, total=False):
    total_spend: Required[builtins.float]
    media_buy_count: NotRequired[builtins.int]

class _GetAccountFinancialsResponseCredit(TypedDict, total=False):
    credit_limit: Required[builtins.float]
    available_credit: Required[builtins.float]
    utilization_percent: NotRequired[builtins.float]

class _GetAccountFinancialsResponseBalance(TypedDict, total=False):
    available: Required[builtins.float]
    last_top_up: NotRequired[_GetAccountFinancialsResponseBalanceLastTopUp]

class _GetAccountFinancialsResponseInvoicesItem(TypedDict, total=False):
    invoice_id: Required[builtins.str]
    period: NotRequired[_ExternalCoreDateRange]
    amount: Required[builtins.float]
    status: Required[Literal['draft', 'issued', 'paid', 'past_due', 'void']]
    due_date: NotRequired[builtins.str]
    paid_date: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseAdcp(TypedDict, total=False):
    major_versions: Required[builtins.list[builtins.int]]
    idempotency: Required[_GetAdcpCapabilitiesResponseAdcpIdempotencyVariant1 | _GetAdcpCapabilitiesResponseAdcpIdempotencyVariant2]

class _GetAdcpCapabilitiesResponseAccount(TypedDict, total=False):
    require_operator_auth: NotRequired[builtins.bool]
    authorization_endpoint: NotRequired[builtins.str]
    supported_billing: NotRequired[builtins.list[Literal['operator', 'agent', 'advertiser']]]
    required_for_products: NotRequired[builtins.bool]
    account_financials: NotRequired[builtins.bool]
    sandbox: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseMediaBuy(TypedDict, total=False):
    supported_pricing_models: NotRequired[builtins.list[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]]
    reporting_delivery_methods: NotRequired[builtins.list[Literal['webhook', 'offline']]]
    offline_delivery_protocols: NotRequired[builtins.list[Literal['s3', 'gcs', 'azure_blob']]]
    features: NotRequired[_ExternalCoreMediaBuyFeatures]
    execution: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecution]
    audience_targeting: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyAudienceTargeting]
    conversion_tracking: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyConversionTracking]
    content_standards: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyContentStandards]
    portfolio: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyPortfolio]

class _GetAdcpCapabilitiesResponseSignals(TypedDict, total=False):
    data_provider_domains: NotRequired[builtins.list[builtins.str]]
    features: NotRequired[_GetAdcpCapabilitiesResponseSignalsFeatures]

class _GetAdcpCapabilitiesResponseGovernance(TypedDict, total=False):
    aggregation_window_days: NotRequired[builtins.int]
    property_features: NotRequired[builtins.list[_GetAdcpCapabilitiesResponseGovernancePropertyFeaturesItem]]
    creative_features: NotRequired[builtins.list[_GetAdcpCapabilitiesResponseGovernanceCreativeFeaturesItem]]

class _GetAdcpCapabilitiesResponseSponsoredIntelligence(TypedDict, total=False):
    endpoint: Required[_GetAdcpCapabilitiesResponseSponsoredIntelligenceEndpoint]
    capabilities: Required[_ExternalSponsoredIntelligenceSiCapabilities]
    brand_url: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseBrand(TypedDict, total=False):
    rights: NotRequired[builtins.bool]
    right_types: NotRequired[builtins.list[Literal['talent', 'character', 'brand_ip', 'music', 'stock_media']]]
    available_uses: NotRequired[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    generation_providers: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseCreative(TypedDict, total=False):
    supports_compliance: NotRequired[builtins.bool]
    has_creative_library: NotRequired[builtins.bool]
    supports_generation: NotRequired[builtins.bool]
    supports_transformation: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseRequestSigning(TypedDict, total=False):
    supported: Required[builtins.bool]
    covers_content_digest: NotRequired[Literal['required', 'forbidden', 'either']]
    required_for: NotRequired[builtins.list[builtins.str]]
    warn_for: NotRequired[builtins.list[builtins.str]]
    supported_for: NotRequired[builtins.list[builtins.str]]

class _GetAdcpCapabilitiesResponseWebhookSigning(TypedDict, total=False):
    supported: Required[builtins.bool]
    profile: NotRequired[Literal['adcp/webhook-signing/v1']]
    algorithms: NotRequired[builtins.list[Literal['ed25519', 'ecdsa-p256-sha256']]]
    legacy_hmac_fallback: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseIdentity(TypedDict, total=False):
    per_principal_key_isolation: NotRequired[builtins.bool]
    key_origins: NotRequired[_GetAdcpCapabilitiesResponseIdentityKeyOrigins]
    compromise_notification: NotRequired[_GetAdcpCapabilitiesResponseIdentityCompromiseNotification]

class _GetAdcpCapabilitiesResponseComplianceTesting(TypedDict, total=False):
    scenarios: Required[builtins.list[Literal['force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend']]]

class _GetBrandIdentityResponseHouse(TypedDict, total=False):
    domain: Required[builtins.str]
    name: Required[builtins.str]

class _GetBrandIdentityResponseLogosItem(TypedDict, total=False):
    url: Required[builtins.str]
    orientation: NotRequired[Literal['square', 'horizontal', 'vertical', 'stacked']]
    background: NotRequired[Literal['dark-bg', 'light-bg', 'transparent-bg']]
    variant: NotRequired[Literal['primary', 'secondary', 'icon', 'wordmark', 'full-lockup']]
    tags: NotRequired[builtins.list[builtins.str]]
    usage: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]

class _GetBrandIdentityResponseColors(TypedDict, total=False):
    primary: NotRequired[builtins.str | builtins.list[builtins.str]]
    secondary: NotRequired[builtins.str | builtins.list[builtins.str]]
    accent: NotRequired[builtins.str | builtins.list[builtins.str]]
    background: NotRequired[builtins.str | builtins.list[builtins.str]]
    text: NotRequired[builtins.str | builtins.list[builtins.str]]

class _GetBrandIdentityResponseFonts(TypedDict, total=False):
    primary: NotRequired[builtins.str | _FontRoleVariant2]
    secondary: NotRequired[builtins.str | _FontRoleVariant2]

class _GetBrandIdentityResponseTone(TypedDict, total=False):
    voice: NotRequired[builtins.str]
    attributes: NotRequired[builtins.list[builtins.str]]
    dos: NotRequired[builtins.list[builtins.str]]
    donts: NotRequired[builtins.list[builtins.str]]

class _GetBrandIdentityResponseVoiceSynthesis(TypedDict, total=False):
    provider: NotRequired[builtins.str]
    voice_id: NotRequired[builtins.str]
    settings: NotRequired[builtins.dict[builtins.str, Any]]

class _GetBrandIdentityResponseAssetsItem(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_type: Required[Literal['image', 'video', 'audio', 'text', 'markdown', 'html', 'css', 'javascript', 'vast', 'daast', 'url', 'webhook', 'brief', 'catalog']]
    url: Required[builtins.str]
    tags: NotRequired[builtins.list[builtins.str]]
    name: NotRequired[builtins.str]
    description: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    duration_seconds: NotRequired[builtins.float]
    file_size_bytes: NotRequired[builtins.int]
    format: NotRequired[builtins.str]

class _GetBrandIdentityResponseRights(TypedDict, total=False):
    available_uses: NotRequired[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    excluded_countries: NotRequired[builtins.list[builtins.str]]
    exclusivity_model: NotRequired[builtins.str]
    content_restrictions: NotRequired[builtins.list[builtins.str]]

class _GetCollectionListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetCollectionListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetCollectionListRequestPagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

class _GetCollectionListResponseCollectionsItem(TypedDict, total=False):
    collection_rid: NotRequired[builtins.str]
    name: Required[builtins.str]
    distribution_ids: NotRequired[builtins.list[_GetCollectionListResponseCollectionsItemDistributionIdsItem]]
    content_rating: NotRequired[_ExternalCoreContentRating]
    genre: NotRequired[builtins.list[builtins.str]]
    genre_taxonomy: NotRequired[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]
    kind: NotRequired[Literal['series', 'publication', 'event_series', 'rotation']]

class _ExternalCorePaginationResponse(TypedDict, total=False):
    has_more: Required[builtins.bool]
    cursor: NotRequired[builtins.str]
    total_count: NotRequired[builtins.int]

class _GetCollectionListResponseCoverageGapsValueItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

class _GetContentStandardsResponseCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_ExternalContentStandardsArtifact]]

class _GetContentStandardsResponsePricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetContentStandardsResponsePricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetContentStandardsResponsePricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetContentStandardsResponsePricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetContentStandardsResponsePricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_GetContentStandardsResponsePricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetCreativeDeliveryRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetCreativeDeliveryRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalCorePaginationRequest(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

class _GetCreativeDeliveryResponseReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]
    timezone: NotRequired[builtins.str]

class _GetCreativeDeliveryResponseCreativesItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    media_buy_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    totals: NotRequired[_ExternalCoreDeliveryMetrics]
    variant_count: NotRequired[builtins.int]
    variants: Required[builtins.list[_ExternalCoreCreativeVariant]]

class _GetCreativeDeliveryResponsePagination(TypedDict, total=False):
    limit: Required[builtins.int]
    offset: Required[builtins.int]
    has_more: Required[builtins.bool]
    total: NotRequired[builtins.int]

class _GetCreativeFeaturesRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetCreativeFeaturesRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalCreativeCreativeFeatureResult(TypedDict, total=False):
    feature_id: Required[builtins.str]
    value: Required[builtins.bool | builtins.float | builtins.str]
    unit: NotRequired[builtins.str]
    confidence: NotRequired[builtins.float]
    measured_at: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    methodology_version: NotRequired[builtins.str]
    details: NotRequired[builtins.dict[builtins.str, Any]]
    policy_id: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetMediaBuyArtifactsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetMediaBuyArtifactsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetMediaBuyArtifactsRequestTimeRange(TypedDict, total=False):
    start: NotRequired[builtins.str]
    end: NotRequired[builtins.str]

class _GetMediaBuyArtifactsRequestPagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

class _GetMediaBuyArtifactsResponseArtifactsItem(TypedDict, total=False):
    record_id: Required[builtins.str]
    timestamp: NotRequired[builtins.str]
    package_id: NotRequired[builtins.str]
    artifact: Required[_ExternalContentStandardsArtifact]
    country: NotRequired[builtins.str]
    channel: NotRequired[builtins.str]
    brand_context: NotRequired[_GetMediaBuyArtifactsResponseArtifactsItemBrandContext]
    local_verdict: NotRequired[Literal['pass', 'fail', 'unevaluated']]

class _GetMediaBuyArtifactsResponseCollectionInfo(TypedDict, total=False):
    total_deliveries: NotRequired[builtins.int]
    total_collected: NotRequired[builtins.int]
    returned_count: NotRequired[builtins.int]
    effective_rate: NotRequired[builtins.float]

class _GetMediaBuyDeliveryRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetMediaBuyDeliveryRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetMediaBuyDeliveryRequestAttributionWindow(TypedDict, total=False):
    post_click: NotRequired[_GetMediaBuyDeliveryRequestAttributionWindowPostClick]
    post_view: NotRequired[_GetMediaBuyDeliveryRequestAttributionWindowPostView]
    model: NotRequired[Literal['last_touch', 'first_touch', 'linear', 'time_decay', 'data_driven']]

class _GetMediaBuyDeliveryRequestReportingDimensions(TypedDict, total=False):
    geo: NotRequired[_GetMediaBuyDeliveryRequestReportingDimensionsGeo]
    device_type: NotRequired[_GetMediaBuyDeliveryRequestReportingDimensionsDeviceType]
    device_platform: NotRequired[_GetMediaBuyDeliveryRequestReportingDimensionsDevicePlatform]
    audience: NotRequired[_GetMediaBuyDeliveryRequestReportingDimensionsAudience]
    placement: NotRequired[_GetMediaBuyDeliveryRequestReportingDimensionsPlacement]

class _GetMediaBuyDeliveryResponseReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

class _ExternalCoreAttributionWindow(TypedDict, total=False):
    post_click: NotRequired[_ExternalCoreAttributionWindowPostClick]
    post_view: NotRequired[_ExternalCoreAttributionWindowPostView]
    model: Required[Literal['last_touch', 'first_touch', 'linear', 'time_decay', 'data_driven']]

class _GetMediaBuyDeliveryResponseAggregatedTotals(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    media_buy_count: Required[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItem(TypedDict, total=False):
    media_buy_id: Required[builtins.str]
    status: Required[Literal['pending_creatives', 'pending_start', 'pending', 'active', 'paused', 'completed', 'rejected', 'canceled', 'failed', 'reporting_delayed']]
    expected_availability: NotRequired[builtins.str]
    is_adjusted: NotRequired[builtins.bool]
    pricing_model: NotRequired[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]
    totals: Required[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotals]
    by_package: Required[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItem]]
    daily_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemDailyBreakdownItem]]

class _GetMediaBuysRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetMediaBuysRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetMediaBuysResponseMediaBuysItem(TypedDict, total=False):
    media_buy_id: Required[builtins.str]
    account: NotRequired[_ExternalCoreAccount]
    invoice_recipient: NotRequired[_ExternalCoreBusinessEntity]
    status: Required[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']]
    currency: Required[builtins.str]
    total_budget: Required[builtins.float]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    creative_deadline: NotRequired[builtins.str]
    confirmed_at: NotRequired[builtins.str]
    cancellation: NotRequired[_GetMediaBuysResponseMediaBuysItemCancellation]
    revision: NotRequired[builtins.int]
    created_at: NotRequired[builtins.str]
    updated_at: NotRequired[builtins.str]
    valid_actions: NotRequired[builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']]]
    history: NotRequired[builtins.list[_GetMediaBuysResponseMediaBuysItemHistoryItem]]
    packages: Required[builtins.list[_GetMediaBuysResponseMediaBuysItemPackagesItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetPlanAuditLogsResponsePlansItem(TypedDict, total=False):
    plan_id: Required[builtins.str]
    plan_version: Required[builtins.int]
    status: Required[Literal['active', 'suspended', 'completed']]
    budget: Required[_GetPlanAuditLogsResponsePlansItemBudget]
    channel_allocation: NotRequired[builtins.dict[builtins.str, _GetPlanAuditLogsResponsePlansItemChannelAllocationValue]]
    summary: Required[_GetPlanAuditLogsResponsePlansItemSummary]
    entries: NotRequired[builtins.list[_GetPlanAuditLogsResponsePlansItemEntriesItem]]
    governed_actions: Required[builtins.list[_GetPlanAuditLogsResponsePlansItemGovernedActionsItem]]

class _ExternalCoreProduct(TypedDict, total=False):
    product_id: Required[builtins.str]
    name: Required[builtins.str]
    description: Required[builtins.str]
    publisher_properties: Required[builtins.list[_ExternalCoreProductPublisherPropertiesItemVariant1 | _ExternalCoreProductPublisherPropertiesItemVariant2 | _ExternalCoreProductPublisherPropertiesItemVariant3]]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    format_ids: Required[builtins.list[_ExternalCoreFormatId]]
    placements: NotRequired[builtins.list[_ExternalCorePlacement]]
    delivery_type: Required[Literal['guaranteed', 'non_guaranteed']]
    exclusivity: NotRequired[Literal['none', 'category', 'exclusive']]
    pricing_options: Required[builtins.list[_ExternalCoreProductPricingOptionsItemVariant1 | _ExternalCoreProductPricingOptionsItemVariant2 | _ExternalCoreProductPricingOptionsItemVariant3 | _ExternalCoreProductPricingOptionsItemVariant4 | _ExternalCoreProductPricingOptionsItemVariant5 | _ExternalCoreProductPricingOptionsItemVariant6 | _ExternalCoreProductPricingOptionsItemVariant7 | _ExternalCoreProductPricingOptionsItemVariant8 | _ExternalCoreProductPricingOptionsItemVariant9]]
    forecast: NotRequired[_ExternalCoreDeliveryForecast]
    outcome_measurement: NotRequired[_ExternalCoreOutcomeMeasurement]
    delivery_measurement: NotRequired[_ExternalCoreProductDeliveryMeasurement]
    measurement_terms: NotRequired[_ExternalCoreMeasurementTerms]
    performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    cancellation_policy: NotRequired[_ExternalCoreCancellationPolicy]
    reporting_capabilities: Required[_ExternalCoreReportingCapabilities]
    creative_policy: NotRequired[_ExternalCoreCreativePolicy]
    is_custom: NotRequired[builtins.bool]
    property_targeting_allowed: NotRequired[builtins.bool]
    data_provider_signals: NotRequired[builtins.list[_ExternalCoreProductDataProviderSignalsItemVariant1 | _ExternalCoreProductDataProviderSignalsItemVariant2 | _ExternalCoreProductDataProviderSignalsItemVariant3]]
    signal_targeting_allowed: NotRequired[builtins.bool]
    catalog_types: NotRequired[builtins.list[Literal['offering', 'product', 'inventory', 'store', 'promotion', 'hotel', 'flight', 'job', 'vehicle', 'real_estate', 'education', 'destination', 'app']]]
    metric_optimization: NotRequired[_ExternalCoreProductMetricOptimization]
    max_optimization_goals: NotRequired[builtins.int]
    measurement_readiness: NotRequired[_ExternalCoreMeasurementReadiness]
    conversion_tracking: NotRequired[_ExternalCoreProductConversionTracking]
    catalog_match: NotRequired[_ExternalCoreProductCatalogMatch]
    brief_relevance: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    product_card: NotRequired[_ExternalCoreProductProductCard]
    product_card_detailed: NotRequired[_ExternalCoreProductProductCardDetailed]
    collections: NotRequired[builtins.list[_ExternalCoreCollectionSelector]]
    collection_targeting_allowed: NotRequired[builtins.bool]
    installments: NotRequired[builtins.list[_ExternalCoreInstallment]]
    enforced_policies: NotRequired[builtins.list[builtins.str]]
    trusted_match: NotRequired[_ExternalCoreProductTrustedMatch]
    material_submission: NotRequired[_ExternalCoreProductMaterialSubmission]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetProductsRequestRefineItemVariant1(TypedDict, total=False):
    scope: Required[Literal['request']]
    ask: Required[builtins.str]

class _GetProductsRequestRefineItemVariant2(TypedDict, total=False):
    scope: Required[Literal['product']]
    product_id: Required[builtins.str]
    action: NotRequired[Literal['include', 'omit', 'more_like_this']]
    ask: NotRequired[builtins.str]

class _GetProductsRequestRefineItemVariant3(TypedDict, total=False):
    scope: Required[Literal['proposal']]
    proposal_id: Required[builtins.str]
    action: NotRequired[Literal['include', 'omit', 'finalize']]
    ask: NotRequired[builtins.str]

class _ExternalCoreCatalog(TypedDict, total=False):
    catalog_id: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    type: Required[Literal['offering', 'product', 'inventory', 'store', 'promotion', 'hotel', 'flight', 'job', 'vehicle', 'real_estate', 'education', 'destination', 'app']]
    url: NotRequired[builtins.str]
    feed_format: NotRequired[Literal['google_merchant_center', 'facebook_catalog', 'shopify', 'linkedin_jobs', 'custom']]
    update_frequency: NotRequired[Literal['realtime', 'hourly', 'daily', 'weekly']]
    items: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]
    ids: NotRequired[builtins.list[builtins.str]]
    gtins: NotRequired[builtins.list[builtins.str]]
    tags: NotRequired[builtins.list[builtins.str]]
    category: NotRequired[builtins.str]
    query: NotRequired[builtins.str]
    conversion_events: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    content_id_type: NotRequired[Literal['sku', 'gtin', 'offering_id', 'job_id', 'hotel_id', 'flight_id', 'vehicle_id', 'listing_id', 'store_id', 'program_id', 'destination_id', 'app_id']]
    feed_field_mappings: NotRequired[builtins.list[_ExternalCoreCatalogFieldMapping]]

class _GetProductsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetProductsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalCoreProductFilters(TypedDict, total=False):
    delivery_type: NotRequired[Literal['guaranteed', 'non_guaranteed']]
    exclusivity: NotRequired[Literal['none', 'category', 'exclusive']]
    is_fixed_price: NotRequired[builtins.bool]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    standard_formats_only: NotRequired[builtins.bool]
    min_exposures: NotRequired[builtins.int]
    start_date: NotRequired[builtins.str]
    end_date: NotRequired[builtins.str]
    budget_range: NotRequired[_ExternalCoreProductFiltersBudgetRange]
    countries: NotRequired[builtins.list[builtins.str]]
    regions: NotRequired[builtins.list[builtins.str]]
    metros: NotRequired[builtins.list[_ExternalCoreProductFiltersMetrosItem]]
    channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    required_axe_integrations: NotRequired[builtins.list[builtins.str]]
    trusted_match: NotRequired[_ExternalCoreProductFiltersTrustedMatch]
    required_features: NotRequired[_ExternalCoreMediaBuyFeatures]
    required_geo_targeting: NotRequired[builtins.list[_ExternalCoreProductFiltersRequiredGeoTargetingItem]]
    signal_targeting: NotRequired[builtins.list[_ExternalCoreProductFiltersSignalTargetingItemVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant2 | _ExternalCoreProductFiltersSignalTargetingItemVariant3]]
    postal_areas: NotRequired[builtins.list[_ExternalCoreProductFiltersPostalAreasItem]]
    geo_proximity: NotRequired[builtins.list[_ExternalCoreProductFiltersGeoProximityItemVariant1 | _ExternalCoreProductFiltersGeoProximityItemVariant2 | _ExternalCoreProductFiltersGeoProximityItemVariant3]]
    required_performance_standards: NotRequired[builtins.list[_ExternalCorePerformanceStandard]]
    keywords: NotRequired[builtins.list[_ExternalCoreProductFiltersKeywordsItem]]

class _ExternalCorePropertyListRef(TypedDict, total=False):
    agent_url: Required[builtins.str]
    list_id: Required[builtins.str]
    auth_token: NotRequired[builtins.str]

class _GetProductsRequestTimeBudget(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCoreProposal(TypedDict, total=False):
    proposal_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    allocations: Required[builtins.list[_ExternalCoreProductAllocation]]
    proposal_status: NotRequired[Literal['draft', 'committed']]
    expires_at: NotRequired[builtins.str]
    insertion_order: NotRequired[_ExternalCoreInsertionOrder]
    total_budget_guidance: NotRequired[_ExternalCoreProposalTotalBudgetGuidance]
    brief_alignment: NotRequired[builtins.str]
    forecast: NotRequired[_ExternalCoreDeliveryForecast]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetProductsResponseRefinementAppliedItemVariant1(TypedDict, total=False):
    scope: Required[Literal['request']]
    status: Required[Literal['applied', 'partial', 'unable']]
    notes: NotRequired[builtins.str]

class _GetProductsResponseRefinementAppliedItemVariant2(TypedDict, total=False):
    scope: Required[Literal['product']]
    product_id: Required[builtins.str]
    status: Required[Literal['applied', 'partial', 'unable']]
    notes: NotRequired[builtins.str]

class _GetProductsResponseRefinementAppliedItemVariant3(TypedDict, total=False):
    scope: Required[Literal['proposal']]
    proposal_id: Required[builtins.str]
    status: Required[Literal['applied', 'partial', 'unable']]
    notes: NotRequired[builtins.str]

class _GetProductsResponseIncompleteItem(TypedDict, total=False):
    scope: Required[Literal['products', 'pricing', 'forecast', 'proposals']]
    description: Required[builtins.str]
    estimated_wait: NotRequired[_GetProductsResponseIncompleteItemEstimatedWait]

class _GetPropertyListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetPropertyListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetPropertyListRequestPagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

class _ExternalCoreIdentifier(TypedDict, total=False):
    type: Required[Literal['domain', 'subdomain', 'network_id', 'ios_bundle', 'android_package', 'apple_app_store_id', 'google_play_id', 'roku_store_id', 'fire_tv_asin', 'samsung_app_id', 'apple_tv_bundle', 'bundle_id', 'venue_id', 'screen_id', 'openooh_venue_type', 'rss_url', 'apple_podcast_id', 'spotify_collection_id', 'podcast_guid', 'station_id', 'facility_id']]
    value: Required[builtins.str]

class _GetRightsResponseRightsItem(TypedDict, total=False):
    rights_id: Required[builtins.str]
    brand_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    right_type: NotRequired[Literal['talent', 'character', 'brand_ip', 'music', 'stock_media']]
    match_score: NotRequired[builtins.float]
    match_reasons: NotRequired[builtins.list[builtins.str]]
    available_uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    countries: NotRequired[builtins.list[builtins.str]]
    excluded_countries: NotRequired[builtins.list[builtins.str]]
    exclusivity_status: NotRequired[_GetRightsResponseRightsItemExclusivityStatus]
    pricing_options: Required[builtins.list[_ExternalBrandRightsPricingOption]]
    content_restrictions: NotRequired[builtins.list[builtins.str]]
    preview_assets: NotRequired[builtins.list[_GetRightsResponseRightsItemPreviewAssetsItem]]

class _GetRightsResponseExcludedItem(TypedDict, total=False):
    brand_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    reason: Required[builtins.str]
    suggestions: NotRequired[builtins.list[builtins.str]]

class _GetSignalsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _GetSignalsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _GetSignalsRequestSignalIdsItemVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _GetSignalsRequestSignalIdsItemVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _GetSignalsRequestDestinationsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]

class _GetSignalsRequestDestinationsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]

class _ExternalCoreSignalFilters(TypedDict, total=False):
    catalog_types: NotRequired[builtins.list[Literal['marketplace', 'custom', 'owned']]]
    data_providers: NotRequired[builtins.list[builtins.str]]
    max_cpm: NotRequired[builtins.float]
    max_percent: NotRequired[builtins.float]
    min_coverage_percentage: NotRequired[builtins.float]

class _GetSignalsResponseSignalsItem(TypedDict, total=False):
    signal_id: Required[_GetSignalsResponseSignalsItemSignalIdVariant1 | _GetSignalsResponseSignalsItemSignalIdVariant2]
    signal_agent_segment_id: Required[builtins.str]
    name: Required[builtins.str]
    description: Required[builtins.str]
    value_type: NotRequired[Literal['binary', 'categorical', 'numeric']]
    categories: NotRequired[builtins.list[builtins.str]]
    range: NotRequired[_GetSignalsResponseSignalsItemRange]
    signal_type: Required[Literal['marketplace', 'custom', 'owned']]
    data_provider: Required[builtins.str]
    coverage_percentage: Required[builtins.float]
    deployments: Required[builtins.list[_GetSignalsResponseSignalsItemDeploymentsItemVariant1 | _GetSignalsResponseSignalsItemDeploymentsItemVariant2]]
    pricing_options: Required[builtins.list[_GetSignalsResponseSignalsItemPricingOptionsItemVariant1 | _GetSignalsResponseSignalsItemPricingOptionsItemVariant2 | _GetSignalsResponseSignalsItemPricingOptionsItemVariant3 | _GetSignalsResponseSignalsItemPricingOptionsItemVariant4 | _GetSignalsResponseSignalsItemPricingOptionsItemVariant5]]

class _IdentityMatchRequestIdentitiesItem(TypedDict, total=False):
    user_token: Required[builtins.str]
    uid_type: Required[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]

class _IdentityMatchRequestConsent(TypedDict, total=False):
    gdpr: NotRequired[builtins.bool]
    tcf_consent: NotRequired[builtins.str]
    gpp: NotRequired[builtins.str]
    us_privacy: NotRequired[builtins.str]

class _ListCollectionListsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ListCollectionListsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalContentStandardsContentStandards(TypedDict, total=False):
    standards_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    languages_any: NotRequired[builtins.list[builtins.str]]
    policies: NotRequired[builtins.list[_ExternalGovernancePolicyEntry]]
    calibration_exemplars: NotRequired[_ExternalContentStandardsContentStandardsCalibrationExemplars]
    pricing_options: NotRequired[builtins.list[_ExternalContentStandardsContentStandardsPricingOptionsItemVariant1 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant2 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant3 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant4 | _ExternalContentStandardsContentStandardsPricingOptionsItemVariant5]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ListCreativeFormatsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ListCreativeFormatsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalCoreFormat(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    example_url: NotRequired[builtins.str]
    accepts_parameters: NotRequired[builtins.list[Literal['dimensions', 'duration']]]
    renders: NotRequired[builtins.list[_ExternalCoreFormatRendersItemVariant1 | _ExternalCoreFormatRendersItemVariant2]]
    assets: NotRequired[builtins.list[_ExternalCoreFormatAssetsItemVariant1 | _ExternalCoreFormatAssetsItemVariant2 | _ExternalCoreFormatAssetsItemVariant3 | _ExternalCoreFormatAssetsItemVariant4 | _ExternalCoreFormatAssetsItemVariant5 | _ExternalCoreFormatAssetsItemVariant6 | _ExternalCoreFormatAssetsItemVariant7 | _ExternalCoreFormatAssetsItemVariant8 | _ExternalCoreFormatAssetsItemVariant9 | _ExternalCoreFormatAssetsItemVariant10 | _ExternalCoreFormatAssetsItemVariant11 | _ExternalCoreFormatAssetsItemVariant12 | _ExternalCoreFormatAssetsItemVariant13 | _ExternalCoreFormatAssetsItemVariant14 | _ExternalCoreFormatAssetsItemVariant15]]
    delivery: NotRequired[builtins.dict[builtins.str, Any]]
    supported_macros: NotRequired[builtins.list[Literal['MEDIA_BUY_ID', 'PACKAGE_ID', 'CREATIVE_ID', 'CACHEBUSTER', 'TIMESTAMP', 'CLICK_URL', 'GDPR', 'GDPR_CONSENT', 'US_PRIVACY', 'GPP_STRING', 'GPP_SID', 'IP_ADDRESS', 'LIMIT_AD_TRACKING', 'DEVICE_TYPE', 'OS', 'OS_VERSION', 'DEVICE_MAKE', 'DEVICE_MODEL', 'USER_AGENT', 'APP_BUNDLE', 'APP_NAME', 'COUNTRY', 'REGION', 'CITY', 'ZIP', 'DMA', 'LAT', 'LONG', 'DEVICE_ID', 'DEVICE_ID_TYPE', 'DOMAIN', 'PAGE_URL', 'REFERRER', 'KEYWORDS', 'PLACEMENT_ID', 'FOLD_POSITION', 'AD_WIDTH', 'AD_HEIGHT', 'VIDEO_ID', 'VIDEO_TITLE', 'VIDEO_DURATION', 'VIDEO_CATEGORY', 'CONTENT_GENRE', 'CONTENT_RATING', 'PLAYER_WIDTH', 'PLAYER_HEIGHT', 'POD_POSITION', 'POD_SIZE', 'AD_BREAK_ID', 'STATION_ID', 'COLLECTION_NAME', 'INSTALLMENT_ID', 'AUDIO_DURATION', 'TMPX', 'AXEM', 'CATALOG_ID', 'SKU', 'GTIN', 'OFFERING_ID', 'JOB_ID', 'HOTEL_ID', 'FLIGHT_ID', 'VEHICLE_ID', 'LISTING_ID', 'STORE_ID', 'PROGRAM_ID', 'DESTINATION_ID', 'CREATIVE_VARIANT_ID', 'APP_ITEM_ID'] | builtins.str]]
    input_format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    output_format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    format_card: NotRequired[_ExternalCoreFormatFormatCard]
    accessibility: NotRequired[_ExternalCoreFormatAccessibility]
    supported_disclosure_positions: NotRequired[builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']]]
    disclosure_capabilities: NotRequired[builtins.list[_ExternalCoreFormatDisclosureCapabilitiesItem]]
    format_card_detailed: NotRequired[_ExternalCoreFormatFormatCardDetailed]
    reported_metrics: NotRequired[builtins.list[Literal['impressions', 'spend', 'clicks', 'ctr', 'video_completions', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'viewability', 'engagement_rate', 'views', 'completed_views', 'leads', 'reach', 'frequency', 'grps', 'quartile_data', 'dooh_metrics', 'cost_per_click']]]
    pricing_options: NotRequired[builtins.list[_ExternalCoreFormatPricingOptionsItemVariant1 | _ExternalCoreFormatPricingOptionsItemVariant2 | _ExternalCoreFormatPricingOptionsItemVariant3 | _ExternalCoreFormatPricingOptionsItemVariant4 | _ExternalCoreFormatPricingOptionsItemVariant5]]

class _ListCreativeFormatsResponseCreativeAgentsItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    agent_name: NotRequired[builtins.str]
    capabilities: NotRequired[builtins.list[Literal['validation', 'assembly', 'generation', 'preview', 'delivery']]]

class _ExternalCoreCreativeFilters(TypedDict, total=False):
    accounts: NotRequired[builtins.list[_ExternalCoreCreativeFiltersAccountsItemVariant1 | _ExternalCoreCreativeFiltersAccountsItemVariant2]]
    statuses: NotRequired[builtins.list[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]]
    tags: NotRequired[builtins.list[builtins.str]]
    tags_any: NotRequired[builtins.list[builtins.str]]
    name_contains: NotRequired[builtins.str]
    creative_ids: NotRequired[builtins.list[builtins.str]]
    created_after: NotRequired[builtins.str]
    created_before: NotRequired[builtins.str]
    updated_after: NotRequired[builtins.str]
    updated_before: NotRequired[builtins.str]
    assigned_to_packages: NotRequired[builtins.list[builtins.str]]
    media_buy_ids: NotRequired[builtins.list[builtins.str]]
    unassigned: NotRequired[builtins.bool]
    has_served: NotRequired[builtins.bool]
    concept_ids: NotRequired[builtins.list[builtins.str]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]
    has_variables: NotRequired[builtins.bool]

class _ListCreativesRequestSort(TypedDict, total=False):
    field: NotRequired[Literal['created_date', 'updated_date', 'name', 'status', 'assignment_count']]
    direction: NotRequired[Literal['asc', 'desc']]

class _ListCreativesRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ListCreativesRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ListCreativesResponseQuerySummary(TypedDict, total=False):
    total_matching: Required[builtins.int]
    returned: Required[builtins.int]
    filters_applied: NotRequired[builtins.list[builtins.str]]
    sort_applied: NotRequired[_ListCreativesResponseQuerySummarySortApplied]

class _ListCreativesResponseCreativesItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    account: NotRequired[_ExternalCoreAccount]
    name: Required[builtins.str]
    format_id: Required[_ExternalCoreFormatId]
    status: Required[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]
    created_date: Required[builtins.str]
    updated_date: Required[builtins.str]
    assets: NotRequired[builtins.dict[builtins.str, Any]]
    tags: NotRequired[builtins.list[builtins.str]]
    concept_id: NotRequired[builtins.str]
    concept_name: NotRequired[builtins.str]
    variables: NotRequired[builtins.list[_ExternalCoreCreativeVariable]]
    assignments: NotRequired[_ListCreativesResponseCreativesItemAssignments]
    snapshot: NotRequired[_ListCreativesResponseCreativesItemSnapshot]
    snapshot_unavailable_reason: NotRequired[Literal['SNAPSHOT_UNSUPPORTED', 'SNAPSHOT_TEMPORARILY_UNAVAILABLE', 'SNAPSHOT_PERMISSION_DENIED']]
    items: NotRequired[builtins.list[_ListCreativesResponseCreativesItemItemsItemVariant1 | _ListCreativesResponseCreativesItemItemsItemVariant2]]
    pricing_options: NotRequired[builtins.list[_ListCreativesResponseCreativesItemPricingOptionsItemVariant1 | _ListCreativesResponseCreativesItemPricingOptionsItemVariant2 | _ListCreativesResponseCreativesItemPricingOptionsItemVariant3 | _ListCreativesResponseCreativesItemPricingOptionsItemVariant4 | _ListCreativesResponseCreativesItemPricingOptionsItemVariant5]]

class _ListCreativesResponseStatusSummary(TypedDict, total=False):
    processing: NotRequired[builtins.int]
    approved: NotRequired[builtins.int]
    pending_review: NotRequired[builtins.int]
    rejected: NotRequired[builtins.int]
    archived: NotRequired[builtins.int]

class _ListPropertyListsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ListPropertyListsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalCoreEvent(TypedDict, total=False):
    event_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_time: Required[builtins.str]
    user_match: NotRequired[_ExternalCoreUserMatch]
    custom_data: NotRequired[_ExternalCoreEventCustomData]
    action_source: NotRequired[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_url: NotRequired[builtins.str]
    custom_event_name: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _LogEventResponsePartialFailuresItem(TypedDict, total=False):
    event_id: Required[builtins.str]
    code: Required[builtins.str]
    message: Required[builtins.str]

class _PackageRequestOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_PackageRequestOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_PackageRequestOptimizationGoalsItemVariant1TargetVariant1 | _PackageRequestOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

class _PackageRequestOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_PackageRequestOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_PackageRequestOptimizationGoalsItemVariant2TargetVariant1 | _PackageRequestOptimizationGoalsItemVariant2TargetVariant2 | _PackageRequestOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_PackageRequestOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

class _ExternalCoreTargeting(TypedDict, total=False):
    geo_countries: NotRequired[builtins.list[builtins.str]]
    geo_countries_exclude: NotRequired[builtins.list[builtins.str]]
    geo_regions: NotRequired[builtins.list[builtins.str]]
    geo_regions_exclude: NotRequired[builtins.list[builtins.str]]
    geo_metros: NotRequired[builtins.list[_ExternalCoreTargetingGeoMetrosItem]]
    geo_metros_exclude: NotRequired[builtins.list[_ExternalCoreTargetingGeoMetrosExcludeItem]]
    geo_postal_areas: NotRequired[builtins.list[_ExternalCoreTargetingGeoPostalAreasItem]]
    geo_postal_areas_exclude: NotRequired[builtins.list[_ExternalCoreTargetingGeoPostalAreasExcludeItem]]
    daypart_targets: NotRequired[builtins.list[_ExternalCoreDaypartTarget]]
    axe_include_segment: NotRequired[builtins.str]
    axe_exclude_segment: NotRequired[builtins.str]
    audience_include: NotRequired[builtins.list[builtins.str]]
    audience_exclude: NotRequired[builtins.list[builtins.str]]
    frequency_cap: NotRequired[_ExternalCoreFrequencyCap]
    property_list: NotRequired[_ExternalCorePropertyListRef]
    collection_list: NotRequired[_ExternalCoreCollectionListRef]
    collection_list_exclude: NotRequired[_ExternalCoreCollectionListRef]
    age_restriction: NotRequired[_ExternalCoreTargetingAgeRestriction]
    device_platform: NotRequired[builtins.list[Literal['ios', 'android', 'windows', 'macos', 'linux', 'chromeos', 'tvos', 'tizen', 'webos', 'fire_os', 'roku_os', 'unknown']]]
    device_type: NotRequired[builtins.list[Literal['desktop', 'mobile', 'tablet', 'ctv', 'dooh', 'unknown']]]
    device_type_exclude: NotRequired[builtins.list[Literal['desktop', 'mobile', 'tablet', 'ctv', 'dooh', 'unknown']]]
    store_catchments: NotRequired[builtins.list[_ExternalCoreTargetingStoreCatchmentsItem]]
    geo_proximity: NotRequired[builtins.list[_ExternalCoreTargetingGeoProximityItemVariant1 | _ExternalCoreTargetingGeoProximityItemVariant2 | _ExternalCoreTargetingGeoProximityItemVariant3]]
    language: NotRequired[builtins.list[builtins.str]]
    keyword_targets: NotRequired[builtins.list[_ExternalCoreTargetingKeywordTargetsItem]]
    negative_keywords: NotRequired[builtins.list[_ExternalCoreTargetingNegativeKeywordsItem]]

class _ExternalCoreMeasurementTerms(TypedDict, total=False):
    billing_measurement: NotRequired[_ExternalCoreMeasurementTermsBillingMeasurement]
    makegood_policy: NotRequired[_ExternalCoreMeasurementTermsMakegoodPolicy]

class _ExternalCorePerformanceStandard(TypedDict, total=False):
    metric: Required[Literal['viewability', 'ivt', 'completion_rate', 'brand_safety', 'attention_score']]
    threshold: Required[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]
    vendor: Required[_ExternalCoreBrandRef]

class _ExternalCoreCreativeAssignment(TypedDict, total=False):
    creative_id: Required[builtins.str]
    weight: NotRequired[builtins.float]
    placement_ids: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreCreativeAsset(TypedDict, total=False):
    creative_id: Required[builtins.str]
    name: Required[builtins.str]
    format_id: Required[_ExternalCoreFormatId]
    assets: Required[builtins.dict[builtins.str, Any]]
    inputs: NotRequired[builtins.list[_ExternalCoreCreativeAssetInputsItem]]
    tags: NotRequired[builtins.list[builtins.str]]
    status: NotRequired[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]
    weight: NotRequired[builtins.float]
    placement_ids: NotRequired[builtins.list[builtins.str]]
    industry_identifiers: NotRequired[builtins.list[_ExternalCoreIndustryIdentifier]]
    provenance: NotRequired[_ExternalCoreProvenance]

class _PreviewCreativeRequestInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _PreviewCreativeRequestRequestsItem(TypedDict, total=False):
    format_id: NotRequired[_ExternalCoreFormatId]
    creative_manifest: Required[_ExternalCoreCreativeManifest]
    inputs: NotRequired[builtins.list[_PreviewCreativeRequestRequestsItemInputsItem]]
    template_id: NotRequired[builtins.str]
    quality: NotRequired[Literal['draft', 'production']]
    output_format: NotRequired[Literal['url', 'html']]
    item_limit: NotRequired[builtins.int]

class _PreviewCreativeResponsePreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponsePreviewsItemRendersItemVariant1 | _PreviewCreativeResponsePreviewsItemRendersItemVariant2 | _PreviewCreativeResponsePreviewsItemRendersItemVariant3]]
    input: Required[_PreviewCreativeResponsePreviewsItemInput]

class _PreviewCreativeResponseResultsItemVariant1(TypedDict, total=False):
    success: Required[Literal[True]]
    creative_id: Required[builtins.str]
    response: Required[_PreviewCreativeResponseResultsItemVariant1Response]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

class _PreviewCreativeResponseResultsItemVariant2(TypedDict, total=False):
    success: Required[Literal[False]]
    creative_id: Required[builtins.str]
    response: NotRequired[_PreviewCreativeResponseResultsItemVariant2Response]
    errors: Required[builtins.list[_ExternalCoreError]]

class _PreviewCreativeResponsePreviewsItem2(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponsePreviewsItem2RendersItemVariant1 | _PreviewCreativeResponsePreviewsItem2RendersItemVariant2 | _PreviewCreativeResponsePreviewsItem2RendersItemVariant3]]

class _ExternalCoreDatetimeRange(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

class _ReportPlanOutcomeRequestSellerResponse(TypedDict, total=False):
    seller_reference: NotRequired[builtins.str]
    committed_budget: NotRequired[builtins.float]
    packages: NotRequired[builtins.list[_ReportPlanOutcomeRequestSellerResponsePackagesItem]]
    planned_delivery: NotRequired[_ExternalCorePlannedDelivery]
    creative_deadline: NotRequired[builtins.str]

class _ReportPlanOutcomeRequestDelivery(TypedDict, total=False):
    reporting_period: NotRequired[_ReportPlanOutcomeRequestDeliveryReportingPeriod]
    impressions: NotRequired[builtins.int]
    spend: NotRequired[builtins.float]
    cpm: NotRequired[builtins.float]
    viewability_rate: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]

class _ReportPlanOutcomeRequestError(TypedDict, total=False):
    code: NotRequired[builtins.str]
    message: NotRequired[builtins.str]

class _ReportPlanOutcomeResponseFindingsItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    severity: Required[Literal['info', 'warning', 'critical']]
    explanation: Required[builtins.str]
    details: NotRequired[builtins.dict[builtins.str, Any]]

class _ReportPlanOutcomeResponsePlanSummary(TypedDict, total=False):
    total_committed: NotRequired[builtins.float]
    budget_remaining: NotRequired[builtins.float]

class _ReportUsageRequestUsageItem(TypedDict, total=False):
    account: Required[_ReportUsageRequestUsageItemAccountVariant1 | _ReportUsageRequestUsageItemAccountVariant2]
    media_buy_id: NotRequired[builtins.str]
    vendor_cost: Required[builtins.float]
    currency: Required[builtins.str]
    pricing_option_id: NotRequired[builtins.str]
    impressions: NotRequired[builtins.int]
    media_spend: NotRequired[builtins.float]
    signal_agent_segment_id: NotRequired[builtins.str]
    standards_id: NotRequired[builtins.str]
    rights_id: NotRequired[builtins.str]
    creative_id: NotRequired[builtins.str]
    property_list_id: NotRequired[builtins.str]

class _SiGetOfferingResponseOffering(TypedDict, total=False):
    offering_id: NotRequired[builtins.str]
    title: NotRequired[builtins.str]
    summary: NotRequired[builtins.str]
    tagline: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    price_hint: NotRequired[builtins.str]
    image_url: NotRequired[builtins.str]
    landing_url: NotRequired[builtins.str]

class _SiGetOfferingResponseMatchingProductsItem(TypedDict, total=False):
    product_id: Required[builtins.str]
    name: Required[builtins.str]
    price: NotRequired[builtins.str]
    original_price: NotRequired[builtins.str]
    image_url: NotRequired[builtins.str]
    availability_summary: NotRequired[builtins.str]
    url: NotRequired[builtins.str]

class _ExternalSponsoredIntelligenceSiIdentity(TypedDict, total=False):
    consent_granted: Required[builtins.bool]
    consent_timestamp: NotRequired[builtins.str]
    consent_scope: NotRequired[builtins.list[Literal['name', 'email', 'shipping_address', 'phone', 'locale']]]
    privacy_policy_acknowledged: NotRequired[_ExternalSponsoredIntelligenceSiIdentityPrivacyPolicyAcknowledged]
    user: NotRequired[_ExternalSponsoredIntelligenceSiIdentityUser]
    anonymous_session_id: NotRequired[builtins.str]

class _ExternalSponsoredIntelligenceSiCapabilities(TypedDict, total=False):
    modalities: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesModalities]
    components: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesComponents]
    commerce: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesCommerce]
    a2ui: NotRequired[_ExternalSponsoredIntelligenceSiCapabilitiesA2ui]
    mcp_apps: NotRequired[builtins.bool]

class _SiInitiateSessionResponseResponse(TypedDict, total=False):
    message: NotRequired[builtins.str]
    ui_elements: NotRequired[builtins.list[_ExternalSponsoredIntelligenceSiUiElement]]

class _SiSendMessageRequestActionResponse(TypedDict, total=False):
    action: NotRequired[builtins.str]
    payload: NotRequired[builtins.dict[builtins.str, Any]]

class _SiSendMessageResponseResponse(TypedDict, total=False):
    message: NotRequired[builtins.str]
    surface: NotRequired[_ExternalA2uiSurface]
    ui_elements: NotRequired[builtins.list[_ExternalSponsoredIntelligenceSiUiElement]]

class _SiSendMessageResponseHandoff(TypedDict, total=False):
    type: NotRequired[Literal['transaction', 'complete']]
    intent: NotRequired[_SiSendMessageResponseHandoffIntent]
    context_for_checkout: NotRequired[_SiSendMessageResponseHandoffContextForCheckout]

class _SiTerminateSessionRequestTerminationContext(TypedDict, total=False):
    summary: NotRequired[builtins.str]
    transaction_intent: NotRequired[_SiTerminateSessionRequestTerminationContextTransactionIntent]
    cause: NotRequired[builtins.str]

class _SiTerminateSessionResponseAcpHandoff(TypedDict, total=False):
    checkout_url: NotRequired[builtins.str]
    checkout_token: NotRequired[builtins.str]
    payload: NotRequired[builtins.dict[builtins.str, Any]]
    expires_at: NotRequired[builtins.str]

class _SiTerminateSessionResponseFollowUp(TypedDict, total=False):
    action: NotRequired[Literal['save_for_later', 'set_reminder', 'subscribe_updates', 'none']]
    data: NotRequired[builtins.dict[builtins.str, Any]]

class _SyncAccountsRequestAccountsItem(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    billing: Required[Literal['operator', 'agent', 'advertiser']]
    billing_entity: NotRequired[_ExternalCoreBusinessEntity]
    payment_terms: NotRequired[Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay']]
    sandbox: NotRequired[builtins.bool]
    preferred_reporting_protocol: NotRequired[Literal['s3', 'gcs', 'azure_blob']]

class _SyncAccountsResponseAccountsItem(TypedDict, total=False):
    account_id: NotRequired[builtins.str]
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    name: NotRequired[builtins.str]
    action: Required[Literal['created', 'updated', 'unchanged', 'failed']]
    status: Required[Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed']]
    billing: NotRequired[Literal['operator', 'agent', 'advertiser']]
    billing_entity: NotRequired[_ExternalCoreBusinessEntity]
    account_scope: NotRequired[Literal['operator', 'brand', 'operator_brand', 'agent']]
    setup: NotRequired[_SyncAccountsResponseAccountsItemSetup]
    rate_card: NotRequired[builtins.str]
    payment_terms: NotRequired[Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay']]
    credit_limit: NotRequired[_SyncAccountsResponseAccountsItemCreditLimit]
    errors: NotRequired[builtins.list[_ExternalCoreError]]
    warnings: NotRequired[builtins.list[builtins.str]]
    sandbox: NotRequired[builtins.bool]

class _SyncAudiencesRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _SyncAudiencesRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _SyncAudiencesRequestAudiencesItem(TypedDict, total=False):
    audience_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    description: NotRequired[builtins.str]
    audience_type: NotRequired[Literal['crm', 'suppression', 'lookalike_seed']]
    tags: NotRequired[builtins.list[builtins.str]]
    add: NotRequired[builtins.list[_ExternalCoreAudienceMember]]
    remove: NotRequired[builtins.list[_ExternalCoreAudienceMember]]
    delete: NotRequired[builtins.bool]
    consent_basis: NotRequired[Literal['consent', 'legitimate_interest', 'contract', 'legal_obligation']]

class _SyncAudiencesResponseAudiencesItem(TypedDict, total=False):
    audience_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    seller_id: NotRequired[builtins.str]
    action: Required[Literal['created', 'updated', 'unchanged', 'deleted', 'failed']]
    status: NotRequired[Literal['processing', 'ready', 'too_small']]
    uploaded_count: NotRequired[builtins.int]
    total_uploaded_count: NotRequired[builtins.int]
    matched_count: NotRequired[builtins.int]
    effective_match_rate: NotRequired[builtins.float]
    match_breakdown: NotRequired[builtins.list[_SyncAudiencesResponseAudiencesItemMatchBreakdownItem]]
    last_synced_at: NotRequired[builtins.str]
    minimum_size: NotRequired[builtins.int]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

class _SyncCatalogsRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _SyncCatalogsRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _SyncCatalogsResponseCatalogsItem(TypedDict, total=False):
    catalog_id: Required[builtins.str]
    action: Required[Literal['created', 'updated', 'unchanged', 'failed', 'deleted']]
    platform_id: NotRequired[builtins.str]
    item_count: NotRequired[builtins.int]
    items_approved: NotRequired[builtins.int]
    items_pending: NotRequired[builtins.int]
    items_rejected: NotRequired[builtins.int]
    item_issues: NotRequired[builtins.list[_SyncCatalogsResponseCatalogsItemItemIssuesItem]]
    last_synced_at: NotRequired[builtins.str]
    next_fetch_at: NotRequired[builtins.str]
    changes: NotRequired[builtins.list[builtins.str]]
    errors: NotRequired[builtins.list[_ExternalCoreError]]
    warnings: NotRequired[builtins.list[builtins.str]]

class _SyncCreativesRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _SyncCreativesRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _SyncCreativesRequestAssignmentsItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    package_id: Required[builtins.str]
    weight: NotRequired[builtins.float]
    placement_ids: NotRequired[builtins.list[builtins.str]]

class _SyncCreativesResponseCreativesItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    account: NotRequired[_ExternalCoreAccount]
    action: Required[Literal['created', 'updated', 'unchanged', 'failed', 'deleted']]
    status: NotRequired[Literal['processing', 'pending_review', 'approved', 'rejected', 'archived']]
    platform_id: NotRequired[builtins.str]
    changes: NotRequired[builtins.list[builtins.str]]
    errors: NotRequired[builtins.list[_ExternalCoreError]]
    warnings: NotRequired[builtins.list[builtins.str]]
    preview_url: NotRequired[builtins.str]
    expires_at: NotRequired[builtins.str]
    assigned_to: NotRequired[builtins.list[builtins.str]]
    assignment_errors: NotRequired[builtins.dict[builtins.str, Any]]

class _SyncEventSourcesRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _SyncEventSourcesRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _SyncEventSourcesRequestEventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    allowed_domains: NotRequired[builtins.list[builtins.str]]

class _SyncEventSourcesResponseEventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    name: NotRequired[builtins.str]
    seller_id: NotRequired[builtins.str]
    event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    action_source: NotRequired[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    managed_by: NotRequired[Literal['buyer', 'seller']]
    setup: NotRequired[_SyncEventSourcesResponseEventSourcesItemSetup]
    action: Required[Literal['created', 'updated', 'unchanged', 'deleted', 'failed']]
    health: NotRequired[_ExternalCoreEventSourceHealth]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

class _SyncGovernanceRequestAccountsItem(TypedDict, total=False):
    account: Required[_SyncGovernanceRequestAccountsItemAccountVariant1 | _SyncGovernanceRequestAccountsItemAccountVariant2]
    governance_agents: Required[builtins.list[_SyncGovernanceRequestAccountsItemGovernanceAgentsItem]]

class _SyncGovernanceResponseAccountsItem(TypedDict, total=False):
    account: Required[_SyncGovernanceResponseAccountsItemAccountVariant1 | _SyncGovernanceResponseAccountsItemAccountVariant2]
    status: Required[Literal['synced', 'failed']]
    governance_agents: NotRequired[builtins.list[_SyncGovernanceResponseAccountsItemGovernanceAgentsItem]]
    errors: NotRequired[builtins.list[_ExternalCoreError]]

class _SyncPlansRequestPlansItem(TypedDict, total=False):
    plan_id: Required[builtins.str]
    brand: Required[_ExternalCoreBrandRef]
    objectives: Required[builtins.str]
    budget: Required[_SyncPlansRequestPlansItemBudgetVariant1 | _SyncPlansRequestPlansItemBudgetVariant2]
    channels: NotRequired[_SyncPlansRequestPlansItemChannels]
    flight: Required[_SyncPlansRequestPlansItemFlight]
    countries: NotRequired[builtins.list[builtins.str]]
    regions: NotRequired[builtins.list[builtins.str]]
    policy_ids: NotRequired[builtins.list[builtins.str]]
    policy_categories: NotRequired[builtins.list[builtins.str]]
    audience: NotRequired[_ExternalGovernanceAudienceConstraints]
    restricted_attributes: NotRequired[builtins.list[Literal['racial_ethnic_origin', 'political_opinions', 'religious_beliefs', 'trade_union_membership', 'health_data', 'sex_life_sexual_orientation', 'genetic_data', 'biometric_data', 'age', 'familial_status']]]
    restricted_attributes_custom: NotRequired[builtins.list[builtins.str]]
    min_audience_size: NotRequired[builtins.int]
    human_review_required: NotRequired[builtins.bool]
    custom_policies: NotRequired[builtins.list[_ExternalGovernancePolicyEntry]]
    approved_sellers: NotRequired[builtins.list[builtins.str] | None]
    delegations: NotRequired[builtins.list[_SyncPlansRequestPlansItemDelegationsItem]]
    portfolio: NotRequired[_SyncPlansRequestPlansItemPortfolio]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _SyncPlansResponsePlansItem(TypedDict, total=False):
    plan_id: Required[builtins.str]
    status: Required[Literal['active', 'error']]
    version: Required[builtins.int]
    categories: NotRequired[builtins.list[_SyncPlansResponsePlansItemCategoriesItem]]
    resolved_policies: NotRequired[builtins.list[_SyncPlansResponsePlansItemResolvedPoliciesItem]]

class _TasksGetResponseProgress(TypedDict, total=False):
    percentage: NotRequired[builtins.float]
    current_step: NotRequired[builtins.str]
    total_steps: NotRequired[builtins.int]
    step_number: NotRequired[builtins.int]

class _TasksGetResponseError(TypedDict, total=False):
    code: Required[builtins.str]
    message: Required[builtins.str]
    details: NotRequired[_TasksGetResponseErrorDetails]

class _TasksGetResponseHistoryItem(TypedDict, total=False):
    timestamp: Required[builtins.str]
    type: Required[Literal['request', 'response']]
    data: Required[builtins.dict[builtins.str, Any]]

class _TasksListRequestFilters(TypedDict, total=False):
    protocol: NotRequired[Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']]
    protocols: NotRequired[builtins.list[Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']]]
    status: NotRequired[Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']]
    statuses: NotRequired[builtins.list[Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']]]
    task_type: NotRequired[Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']]
    task_types: NotRequired[builtins.list[Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']]]
    created_after: NotRequired[builtins.str]
    created_before: NotRequired[builtins.str]
    updated_after: NotRequired[builtins.str]
    updated_before: NotRequired[builtins.str]
    task_ids: NotRequired[builtins.list[builtins.str]]
    context_contains: NotRequired[builtins.str]
    has_webhook: NotRequired[builtins.bool]

class _TasksListRequestSort(TypedDict, total=False):
    field: NotRequired[Literal['created_at', 'updated_at', 'status', 'task_type', 'protocol']]
    direction: NotRequired[Literal['asc', 'desc']]

class _TasksListRequestPagination(TypedDict, total=False):
    max_results: NotRequired[builtins.int]
    cursor: NotRequired[builtins.str]

class _TasksListResponseQuerySummary(TypedDict, total=False):
    total_matching: NotRequired[builtins.int]
    returned: NotRequired[builtins.int]
    domain_breakdown: NotRequired[_TasksListResponseQuerySummaryDomainBreakdown]
    status_breakdown: NotRequired[builtins.dict[builtins.str, builtins.int]]
    filters_applied: NotRequired[builtins.list[builtins.str]]
    sort_applied: NotRequired[_TasksListResponseQuerySummarySortApplied]

class _TasksListResponseTasksItem(TypedDict, total=False):
    task_id: Required[builtins.str]
    task_type: Required[Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']]
    domain: Required[Literal['media-buy', 'signals']]
    status: Required[Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']]
    created_at: Required[builtins.str]
    updated_at: Required[builtins.str]
    completed_at: NotRequired[builtins.str]
    has_webhook: NotRequired[builtins.bool]

class _TasksListResponsePagination(TypedDict, total=False):
    has_more: Required[builtins.bool]
    cursor: NotRequired[builtins.str]
    total_count: NotRequired[builtins.int]

class _UpdateCollectionListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _UpdateCollectionListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _UpdateCollectionListRequestBaseCollectionsItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['distribution_ids']]
    identifiers: Required[builtins.list[_UpdateCollectionListRequestBaseCollectionsItemVariant1IdentifiersItem]]

class _UpdateCollectionListRequestBaseCollectionsItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_collections']]
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

class _UpdateCollectionListRequestBaseCollectionsItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['publisher_genres']]
    publisher_domain: Required[builtins.str]
    genres: Required[builtins.list[builtins.str]]
    genre_taxonomy: Required[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]

class _UpdateContentStandardsRequestScope(TypedDict, total=False):
    countries_all: NotRequired[builtins.list[builtins.str]]
    channels_any: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    languages_any: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant1 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2]]

class _UpdateMediaBuyRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _UpdateMediaBuyRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalMediaBuyPackageUpdate(TypedDict, total=False):
    package_id: Required[builtins.str]
    budget: NotRequired[builtins.float]
    pacing: NotRequired[Literal['even', 'asap', 'front_loaded']]
    bid_price: NotRequired[builtins.float]
    impressions: NotRequired[builtins.float]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    canceled: NotRequired[Literal[True]]
    cancellation_reason: NotRequired[builtins.str]
    catalogs: NotRequired[builtins.list[_ExternalCoreCatalog]]
    optimization_goals: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2]]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    keyword_targets_add: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateKeywordTargetsAddItem]]
    keyword_targets_remove: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateKeywordTargetsRemoveItem]]
    negative_keywords_add: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateNegativeKeywordsAddItem]]
    negative_keywords_remove: NotRequired[builtins.list[_ExternalMediaBuyPackageUpdateNegativeKeywordsRemoveItem]]
    creative_assignments: NotRequired[builtins.list[_ExternalCoreCreativeAssignment]]
    creatives: NotRequired[builtins.list[_ExternalCoreCreativeAsset]]
    context: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _UpdatePropertyListRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _UpdatePropertyListRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _UpdatePropertyListRequestBasePropertiesItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['publisher_tags']]
    publisher_domain: Required[builtins.str]
    tags: Required[builtins.list[builtins.str]]

class _UpdatePropertyListRequestBasePropertiesItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_ids']]
    publisher_domain: Required[builtins.str]
    property_ids: Required[builtins.list[builtins.str]]

class _UpdatePropertyListRequestBasePropertiesItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['identifiers']]
    identifiers: Required[builtins.list[_ExternalCoreIdentifier]]

class _ValidateContentDeliveryRequestRecordsItem(TypedDict, total=False):
    record_id: Required[builtins.str]
    media_buy_id: NotRequired[builtins.str]
    timestamp: NotRequired[builtins.str]
    artifact: Required[_ExternalContentStandardsArtifact]
    country: NotRequired[builtins.str]
    channel: NotRequired[builtins.str]
    brand_context: NotRequired[_ValidateContentDeliveryRequestRecordsItemBrandContext]

class _ValidateContentDeliveryResponseSummary(TypedDict, total=False):
    total_records: Required[builtins.int]
    passed_records: Required[builtins.int]
    failed_records: Required[builtins.int]

class _ValidateContentDeliveryResponseResultsItem(TypedDict, total=False):
    record_id: Required[builtins.str]
    verdict: Required[Literal['pass', 'fail']]
    features: NotRequired[builtins.list[_ValidateContentDeliveryResponseResultsItemFeaturesItem]]

class _ValidatePropertyDeliveryRequestAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ValidatePropertyDeliveryRequestAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalPropertyDeliveryRecord(TypedDict, total=False):
    identifier: Required[_ExternalCoreIdentifier]
    impressions: Required[builtins.int]
    record_id: NotRequired[builtins.str]
    sales_agent_url: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ValidatePropertyDeliveryResponseSummary(TypedDict, total=False):
    total_records: Required[builtins.int]
    total_impressions: Required[builtins.int]
    compliant_records: Required[builtins.int]
    compliant_impressions: Required[builtins.int]
    non_compliant_records: Required[builtins.int]
    non_compliant_impressions: Required[builtins.int]
    not_covered_records: Required[builtins.int]
    not_covered_impressions: Required[builtins.int]
    unidentified_records: Required[builtins.int]
    unidentified_impressions: Required[builtins.int]

class _ValidatePropertyDeliveryResponseAggregate(TypedDict, total=False):
    score: NotRequired[builtins.float]
    grade: NotRequired[builtins.str]
    label: NotRequired[builtins.str]
    methodology_url: NotRequired[builtins.str]

class _ValidatePropertyDeliveryResponseAuthorizationSummary(TypedDict, total=False):
    records_checked: Required[builtins.int]
    impressions_checked: Required[builtins.int]
    authorized_records: Required[builtins.int]
    authorized_impressions: Required[builtins.int]
    unauthorized_records: Required[builtins.int]
    unauthorized_impressions: Required[builtins.int]
    unknown_records: Required[builtins.int]
    unknown_impressions: Required[builtins.int]

class _ExternalPropertyValidationResult(TypedDict, total=False):
    identifier: Required[_ExternalCoreIdentifier]
    record_id: NotRequired[builtins.str]
    status: Required[Literal['compliant', 'non_compliant', 'not_covered', 'unidentified']]
    impressions: Required[builtins.int]
    features: NotRequired[builtins.list[_ExternalPropertyValidationResultFeaturesItem]]
    authorization: NotRequired[_ExternalPropertyAuthorizationResult]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreBrandRefDataSubjectContestation(TypedDict, total=False):
    url: NotRequired[builtins.str]
    email: NotRequired[builtins.str]
    languages: NotRequired[builtins.list[builtins.str]]

class _ExternalCorePushNotificationConfigAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

class _ExternalBrandRightsTermsExclusivity(TypedDict, total=False):
    scope: NotRequired[builtins.str]
    countries: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreRightsConstraintRightsAgent(TypedDict, total=False):
    url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCoreErrorIssuesItem(TypedDict, total=False):
    pointer: Required[builtins.str]
    message: Required[builtins.str]
    keyword: Required[builtins.str]
    schemaPath: NotRequired[builtins.str]

class _ActivateSignalResponseDeploymentsItemVariant1ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

class _ActivateSignalResponseDeploymentsItemVariant1ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

class _ActivateSignalResponseDeploymentsItemVariant2ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

class _ActivateSignalResponseDeploymentsItemVariant2ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

class _ExternalCoreIndustryIdentifier(TypedDict, total=False):
    type: Required[Literal['ad_id', 'isci', 'clearcast_clock']]
    value: Required[builtins.str]

class _ExternalCoreProvenance(TypedDict, total=False):
    digital_source_type: NotRequired[Literal['digital_capture', 'digital_creation', 'trained_algorithmic_media', 'composite_with_trained_algorithmic_media', 'algorithmic_media', 'composite_capture', 'composite_synthetic', 'human_edits', 'data_driven_media']]
    ai_tool: NotRequired[_ExternalCoreProvenanceAiTool]
    human_oversight: NotRequired[Literal['none', 'prompt_only', 'selected', 'edited', 'directed']]
    declared_by: NotRequired[_ExternalCoreProvenanceDeclaredBy]
    declared_at: NotRequired[builtins.str]
    created_time: NotRequired[builtins.str]
    c2pa: NotRequired[_ExternalCoreProvenanceC2pa]
    disclosure: NotRequired[_ExternalCoreProvenanceDisclosure]
    verification: NotRequired[builtins.list[_ExternalCoreProvenanceVerificationItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _BuildCreativeResponsePreviewPreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_BuildCreativeResponsePreviewPreviewsItemRendersItemVariant1 | _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant2 | _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant3]]
    input: Required[_BuildCreativeResponsePreviewPreviewsItemInput]

class _BuildCreativeResponsePreview2PreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    format_id: Required[_ExternalCoreFormatId]
    renders: Required[builtins.list[_BuildCreativeResponsePreview2PreviewsItemRendersItemVariant1 | _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant2 | _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant3]]
    input: Required[_BuildCreativeResponsePreview2PreviewsItemInput]

class _ExternalContentStandardsArtifactAssetsItemVariant1(TypedDict, total=False):
    type: Required[Literal['text']]
    role: NotRequired[Literal['title', 'paragraph', 'heading', 'caption', 'quote', 'list_item', 'description']]
    content: Required[builtins.str]
    content_format: NotRequired[Literal['text/plain', 'text/markdown', 'text/html', 'application/json']]
    language: NotRequired[builtins.str]
    heading_level: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

class _ExternalContentStandardsArtifactAssetsItemVariant2(TypedDict, total=False):
    type: Required[Literal['image']]
    url: Required[builtins.str]
    access: NotRequired[_ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant3]
    alt_text: NotRequired[builtins.str]
    caption: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

class _ExternalContentStandardsArtifactAssetsItemVariant3(TypedDict, total=False):
    type: Required[Literal['video']]
    url: Required[builtins.str]
    access: NotRequired[_ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'subtitles', 'closed_captions', 'dub', 'generated']]
    thumbnail_url: NotRequired[builtins.str]
    provenance: NotRequired[_ExternalCoreProvenance]

class _ExternalContentStandardsArtifactAssetsItemVariant4(TypedDict, total=False):
    type: Required[Literal['audio']]
    url: Required[builtins.str]
    access: NotRequired[_ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant1 | _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant2 | _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'closed_captions', 'generated']]
    provenance: NotRequired[_ExternalCoreProvenance]

class _ExternalContentStandardsArtifactMetadata(TypedDict, total=False):
    canonical: NotRequired[builtins.str]
    author: NotRequired[builtins.str]
    keywords: NotRequired[builtins.str]
    open_graph: NotRequired[builtins.dict[builtins.str, Any]]
    twitter_card: NotRequired[builtins.dict[builtins.str, Any]]
    json_ld: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]

class _ExternalContentStandardsArtifactIdentifiers(TypedDict, total=False):
    apple_podcast_id: NotRequired[builtins.str]
    spotify_collection_id: NotRequired[builtins.str]
    podcast_guid: NotRequired[builtins.str]
    youtube_video_id: NotRequired[builtins.str]
    rss_url: NotRequired[builtins.str]

class _ExternalCorePlannedDeliveryGeo(TypedDict, total=False):
    countries: NotRequired[builtins.list[builtins.str]]
    regions: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreFrequencyCap(TypedDict, total=False):
    suppress: NotRequired[_ExternalCoreFrequencyCapSuppress]
    suppress_minutes: NotRequired[builtins.float]
    max_impressions: NotRequired[builtins.int]
    per: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    window: NotRequired[_ExternalCoreFrequencyCapWindow]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant1 | _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant4(TypedDict, total=False):
    type: Required[Literal['description']]
    description: Required[builtins.str]
    category: NotRequired[builtins.str]

class _CheckGovernanceRequestDeliveryMetricsReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

class _CheckGovernanceRequestDeliveryMetricsAudienceDistribution(TypedDict, total=False):
    baseline: Required[Literal['census', 'platform', 'custom']]
    baseline_description: NotRequired[builtins.str]
    indices: Required[builtins.dict[builtins.str, builtins.float]]
    cumulative_indices: NotRequired[builtins.dict[builtins.str, builtins.float]]

class _ExternalCoreBusinessEntityAddress(TypedDict, total=False):
    street: Required[builtins.str]
    city: Required[builtins.str]
    postal_code: Required[builtins.str]
    region: NotRequired[builtins.str]
    country: Required[builtins.str]

class _ExternalCoreBusinessEntityContactsItem(TypedDict, total=False):
    role: Required[Literal['billing', 'legal', 'creative', 'general']]
    name: NotRequired[builtins.str]
    email: NotRequired[builtins.str]
    phone: NotRequired[builtins.str]

class _ExternalCoreBusinessEntityBank(TypedDict, total=False):
    account_holder: Required[builtins.str]
    iban: NotRequired[builtins.str]
    bic: NotRequired[builtins.str]
    routing_number: NotRequired[builtins.str]
    account_number: NotRequired[builtins.str]

class _ComplyTestControllerRequestParamsReportedSpend(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

class _ContextMatchRequestGeoMetro(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    value: Required[builtins.str]

class _ExternalCoreSellerAgentRef(TypedDict, total=False):
    agent_url: Required[builtins.str]
    id: NotRequired[builtins.str]

class _ExternalTmpOfferPrice(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: NotRequired[builtins.str]
    model: Required[Literal['cpm', 'cpc', 'cpcv', 'cpa', 'flat']]

class _ContextMatchResponseSignalsTargetingKvsItem(TypedDict, total=False):
    key: Required[builtins.str]
    value: Required[builtins.str]

class _CreateCollectionListRequestBaseCollectionsItemVariant1IdentifiersItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

class _ExternalCoreContentRating(TypedDict, total=False):
    system: Required[Literal['tv_parental', 'mpaa', 'podcast', 'esrb', 'bbfc', 'fsk', 'acb', 'chvrs', 'csa', 'pegi', 'custom']]
    rating: Required[builtins.str]

class _ExternalCollectionCollectionListFiltersExcludeDistributionIdsItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

class _ExternalCollectionCollectionListAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ExternalCollectionCollectionListAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalCollectionCollectionListBaseCollectionsItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['distribution_ids']]
    identifiers: Required[builtins.list[_ExternalCollectionCollectionListBaseCollectionsItemVariant1IdentifiersItem]]

class _ExternalCollectionCollectionListBaseCollectionsItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_collections']]
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

class _ExternalCollectionCollectionListBaseCollectionsItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['publisher_genres']]
    publisher_domain: Required[builtins.str]
    genres: Required[builtins.list[builtins.str]]
    genre_taxonomy: Required[Literal['iab_content_3.0', 'iab_content_2.2', 'gracenote', 'eidr', 'apple_genres', 'google_genres', 'roku', 'amazon_genres', 'custom']]

class _ExternalGovernancePolicyEntryExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_Exemplar]]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant1(TypedDict, total=False):
    type: Required[Literal['url']]
    value: Required[builtins.str]
    language: NotRequired[builtins.str]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2(TypedDict, total=False):
    property_rid: Required[builtins.str]
    artifact_id: Required[builtins.str]
    variant_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    url: NotRequired[builtins.str]
    published_time: NotRequired[builtins.str]
    last_update_time: NotRequired[builtins.str]
    assets: Required[builtins.list[_CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant1 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4]]
    metadata: NotRequired[_CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2Metadata]
    provenance: NotRequired[_ExternalCoreProvenance]
    identifiers: NotRequired[_CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2Identifiers]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant1 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant1 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant2 | _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

class _ExternalCoreReportingWebhookAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

class _CreateMediaBuyRequestArtifactWebhookAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

class _ExternalCoreAccountCreditLimit(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

class _ExternalCoreAccountSetup(TypedDict, total=False):
    url: NotRequired[builtins.str]
    message: Required[builtins.str]
    expires_at: NotRequired[builtins.str]

class _ExternalCoreAccountGovernanceAgentsItem(TypedDict, total=False):
    url: Required[builtins.str]
    categories: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreAccountReportingBucket(TypedDict, total=False):
    protocol: Required[Literal['s3', 'gcs', 'azure_blob']]
    bucket: Required[builtins.str]
    prefix: NotRequired[builtins.str]
    region: NotRequired[builtins.str]
    format: NotRequired[Literal['jsonl', 'csv', 'parquet', 'avro', 'orc']]
    compression: NotRequired[Literal['gzip', 'none']]
    file_retention_days: Required[builtins.int]
    setup_instructions: NotRequired[builtins.str]

class _ExternalPricingOptionsPriceBreakdown(TypedDict, total=False):
    list_price: Required[builtins.float]
    adjustments: Required[builtins.list[_ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant1 | _ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant2]]

class _ExternalCorePackageOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant1 | _ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

class _ExternalCorePackageOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_ExternalCorePackageOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant1 | _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant2 | _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

class _ExternalCorePackageCancellation(TypedDict, total=False):
    canceled_at: Required[builtins.str]
    canceled_by: Required[Literal['buyer', 'seller']]
    reason: NotRequired[builtins.str]
    acknowledged_at: NotRequired[builtins.str]

class _ExternalCoreFeatureRequirement(TypedDict, total=False):
    feature_id: Required[builtins.str]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]
    allowed_values: NotRequired[builtins.list[Any]]
    if_not_covered: NotRequired[Literal['exclude', 'include']]
    policy_id: NotRequired[builtins.str]

class _ExternalPropertyPropertyListAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ExternalPropertyPropertyListAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalPropertyPropertyListBasePropertiesItemVariant1(TypedDict, total=False):
    selection_type: Required[Literal['publisher_tags']]
    publisher_domain: Required[builtins.str]
    tags: Required[builtins.list[builtins.str]]

class _ExternalPropertyPropertyListBasePropertiesItemVariant2(TypedDict, total=False):
    selection_type: Required[Literal['publisher_ids']]
    publisher_domain: Required[builtins.str]
    property_ids: Required[builtins.list[builtins.str]]

class _ExternalPropertyPropertyListBasePropertiesItemVariant3(TypedDict, total=False):
    selection_type: Required[Literal['identifiers']]
    identifiers: Required[builtins.list[_ExternalCoreIdentifier]]

class _ExternalPropertyPropertyListPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalPropertyPropertyListPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalPropertyPropertyListPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalPropertyPropertyListPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalPropertyPropertyListPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ExternalPropertyPropertyListPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetAccountFinancialsResponseBalanceLastTopUp(TypedDict, total=False):
    amount: Required[builtins.float]
    date: Required[builtins.str]

class _GetAdcpCapabilitiesResponseAdcpIdempotencyVariant1(TypedDict, total=False):
    supported: Required[Literal[True]]
    replay_ttl_seconds: Required[builtins.int]
    account_id_is_opaque: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseAdcpIdempotencyVariant2(TypedDict, total=False):
    supported: Required[Literal[False]]

class _ExternalCoreMediaBuyFeatures(TypedDict, total=False):
    inline_creative_management: NotRequired[builtins.bool]
    property_list_filtering: NotRequired[builtins.bool]
    catalog_management: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseMediaBuyExecution(TypedDict, total=False):
    trusted_match: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTrustedMatch]
    axe_integrations: NotRequired[builtins.list[builtins.str]]
    creative_specs: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionCreativeSpecs]
    targeting: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTargeting]

class _GetAdcpCapabilitiesResponseMediaBuyAudienceTargeting(TypedDict, total=False):
    supported_identifier_types: Required[builtins.list[Literal['hashed_email', 'hashed_phone']]]
    supports_platform_customer_id: NotRequired[builtins.bool]
    supported_uid_types: NotRequired[builtins.list[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]]
    minimum_audience_size: Required[builtins.int]
    matching_latency_hours: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyAudienceTargetingMatchingLatencyHours]

class _GetAdcpCapabilitiesResponseMediaBuyConversionTracking(TypedDict, total=False):
    multi_source_event_dedup: NotRequired[builtins.bool]
    supported_event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    supported_uid_types: NotRequired[builtins.list[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]]
    supported_hashed_identifiers: NotRequired[builtins.list[Literal['hashed_email', 'hashed_phone']]]
    supported_action_sources: NotRequired[builtins.list[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]]
    attribution_windows: NotRequired[builtins.list[_GetAdcpCapabilitiesResponseMediaBuyConversionTrackingAttributionWindowsItem]]

class _GetAdcpCapabilitiesResponseMediaBuyContentStandards(TypedDict, total=False):
    supports_local_evaluation: NotRequired[builtins.bool]
    supported_channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    supports_webhook_delivery: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseMediaBuyPortfolio(TypedDict, total=False):
    publisher_domains: Required[builtins.list[builtins.str]]
    primary_channels: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    primary_countries: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]
    advertising_policies: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseSignalsFeatures(TypedDict, total=False):
    catalog_signals: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseGovernancePropertyFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    type: Required[Literal['binary', 'quantitative', 'categorical']]
    range: NotRequired[_GetAdcpCapabilitiesResponseGovernancePropertyFeaturesItemRange]
    categories: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]
    methodology_url: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseGovernanceCreativeFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    type: Required[Literal['binary', 'quantitative', 'categorical']]
    range: NotRequired[_GetAdcpCapabilitiesResponseGovernanceCreativeFeaturesItemRange]
    categories: NotRequired[builtins.list[builtins.str]]
    description: NotRequired[builtins.str]
    methodology_url: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseSponsoredIntelligenceEndpoint(TypedDict, total=False):
    transports: Required[builtins.list[_GetAdcpCapabilitiesResponseSponsoredIntelligenceEndpointTransportsItem]]
    preferred: NotRequired[Literal['mcp', 'a2a']]

class _GetAdcpCapabilitiesResponseIdentityKeyOrigins(TypedDict, total=False):
    governance_signing: NotRequired[builtins.str]
    request_signing: NotRequired[builtins.str]
    webhook_signing: NotRequired[builtins.str]
    tmp_signing: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseIdentityCompromiseNotification(TypedDict, total=False):
    emits: NotRequired[builtins.bool]
    accepts: NotRequired[builtins.bool]

class _FontRoleVariant2(TypedDict, total=False):
    family: Required[builtins.str]
    files: NotRequired[builtins.list[_FontRoleVariant2FilesItem]]
    opentype_features: NotRequired[builtins.list[builtins.str]]
    fallbacks: NotRequired[builtins.list[builtins.str]]

class _GetCollectionListResponseCollectionsItemDistributionIdsItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

class _GetContentStandardsResponsePricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

class _ExternalCoreDeliveryMetrics(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: NotRequired[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_ExternalCoreDeliveryMetricsByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_ExternalCoreDeliveryMetricsQuartileData]
    dooh_metrics: NotRequired[_ExternalCoreDeliveryMetricsDoohMetrics]
    viewability: NotRequired[_ExternalCoreDeliveryMetricsViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_ExternalCoreDeliveryMetricsByActionSourceItem]]

class _ExternalCoreCreativeVariant(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: NotRequired[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_ExternalCoreCreativeVariantByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_ExternalCoreCreativeVariantQuartileData]
    dooh_metrics: NotRequired[_ExternalCoreCreativeVariantDoohMetrics]
    viewability: NotRequired[_ExternalCoreCreativeVariantViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_ExternalCoreCreativeVariantByActionSourceItem]]
    variant_id: Required[builtins.str]
    manifest: NotRequired[_ExternalCoreCreativeManifest]
    generation_context: NotRequired[_ExternalCoreCreativeVariantGenerationContext]

class _GetMediaBuyArtifactsResponseArtifactsItemBrandContext(TypedDict, total=False):
    brand_id: NotRequired[builtins.str]
    sku_id: NotRequired[builtins.str]

class _GetMediaBuyDeliveryRequestAttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _GetMediaBuyDeliveryRequestAttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _GetMediaBuyDeliveryRequestReportingDimensionsGeo(TypedDict, total=False):
    geo_level: Required[Literal['country', 'region', 'metro', 'postal_area']]
    system: NotRequired[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom', 'us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

class _GetMediaBuyDeliveryRequestReportingDimensionsDeviceType(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

class _GetMediaBuyDeliveryRequestReportingDimensionsDevicePlatform(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

class _GetMediaBuyDeliveryRequestReportingDimensionsAudience(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

class _GetMediaBuyDeliveryRequestReportingDimensionsPlacement(TypedDict, total=False):
    limit: NotRequired[builtins.int]
    sort_by: NotRequired[Literal['impressions', 'spend', 'clicks', 'ctr', 'views', 'completed_views', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'leads', 'grps', 'reach', 'frequency', 'engagements', 'follows', 'saves', 'profile_visits', 'engagement_rate', 'cost_per_click']]

class _ExternalCoreAttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCoreAttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotals(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsByActionSourceItem]]
    effective_rate: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItem(TypedDict, total=False):
    impressions: NotRequired[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByActionSourceItem]]
    package_id: Required[builtins.str]
    pacing_index: NotRequired[builtins.float]
    pricing_model: Required[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]
    rate: Required[builtins.float]
    currency: Required[builtins.str]
    delivery_status: NotRequired[Literal['delivering', 'completed', 'budget_exhausted', 'flight_ended', 'goal_met']]
    paused: NotRequired[builtins.bool]
    is_final: NotRequired[builtins.bool]
    measurement_window: NotRequired[builtins.str]
    supersedes_window: NotRequired[builtins.str]
    by_catalog_item: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItem]]
    by_creative: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItem]]
    by_keyword: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItem]]
    by_geo: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItem]]
    by_geo_truncated: NotRequired[builtins.bool]
    by_device_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItem]]
    by_device_type_truncated: NotRequired[builtins.bool]
    by_device_platform: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItem]]
    by_device_platform_truncated: NotRequired[builtins.bool]
    by_audience: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItem]]
    by_audience_truncated: NotRequired[builtins.bool]
    by_placement: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItem]]
    by_placement_truncated: NotRequired[builtins.bool]
    daily_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemDailyBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemDailyBreakdownItem(TypedDict, total=False):
    date: Required[builtins.str]
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]

class _GetMediaBuysResponseMediaBuysItemCancellation(TypedDict, total=False):
    canceled_at: Required[builtins.str]
    canceled_by: Required[Literal['buyer', 'seller']]
    reason: NotRequired[builtins.str]

class _GetMediaBuysResponseMediaBuysItemHistoryItem(TypedDict, total=False):
    revision: Required[builtins.int]
    timestamp: Required[builtins.str]
    actor: NotRequired[builtins.str]
    action: Required[builtins.str]
    summary: NotRequired[builtins.str]
    package_id: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetMediaBuysResponseMediaBuysItemPackagesItem(TypedDict, total=False):
    package_id: Required[builtins.str]
    product_id: NotRequired[builtins.str]
    budget: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]
    bid_price: NotRequired[builtins.float]
    impressions: NotRequired[builtins.float]
    targeting_overlay: NotRequired[_ExternalCoreTargeting]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    paused: NotRequired[builtins.bool]
    canceled: NotRequired[builtins.bool]
    cancellation: NotRequired[_GetMediaBuysResponseMediaBuysItemPackagesItemCancellation]
    creative_deadline: NotRequired[builtins.str]
    creative_approvals: NotRequired[builtins.list[_GetMediaBuysResponseMediaBuysItemPackagesItemCreativeApprovalsItem]]
    format_ids_pending: NotRequired[builtins.list[_ExternalCoreFormatId]]
    snapshot_unavailable_reason: NotRequired[Literal['SNAPSHOT_UNSUPPORTED', 'SNAPSHOT_TEMPORARILY_UNAVAILABLE', 'SNAPSHOT_PERMISSION_DENIED']]
    snapshot: NotRequired[_GetMediaBuysResponseMediaBuysItemPackagesItemSnapshot]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetPlanAuditLogsResponsePlansItemBudget(TypedDict, total=False):
    authorized: NotRequired[builtins.float]
    committed: NotRequired[builtins.float]
    remaining: NotRequired[builtins.float]
    utilization_pct: NotRequired[builtins.float]

class _GetPlanAuditLogsResponsePlansItemChannelAllocationValue(TypedDict, total=False):
    committed: NotRequired[builtins.float]
    pct: NotRequired[builtins.float]

class _GetPlanAuditLogsResponsePlansItemSummary(TypedDict, total=False):
    checks_performed: NotRequired[builtins.int]
    outcomes_reported: NotRequired[builtins.int]
    statuses: NotRequired[_GetPlanAuditLogsResponsePlansItemSummaryStatuses]
    findings_count: NotRequired[builtins.int]
    escalations: NotRequired[builtins.list[_GetPlanAuditLogsResponsePlansItemSummaryEscalationsItem]]
    drift_metrics: NotRequired[_GetPlanAuditLogsResponsePlansItemSummaryDriftMetrics]

class _GetPlanAuditLogsResponsePlansItemEntriesItem(TypedDict, total=False):
    id: Required[builtins.str]
    type: Required[Literal['check', 'outcome']]
    timestamp: Required[builtins.str]
    plan_id: NotRequired[builtins.str]
    caller: NotRequired[builtins.str]
    tool: NotRequired[builtins.str]
    status: NotRequired[Literal['approved', 'denied', 'conditions']]
    check_type: NotRequired[Literal['intent', 'execution']]
    mode: NotRequired[Literal['audit', 'advisory', 'enforce']]
    explanation: NotRequired[builtins.str]
    policies_evaluated: NotRequired[builtins.list[builtins.str]]
    categories_evaluated: NotRequired[builtins.list[builtins.str]]
    findings: NotRequired[builtins.list[_GetPlanAuditLogsResponsePlansItemEntriesItemFindingsItem]]
    outcome: NotRequired[Literal['completed', 'failed', 'delivery']]
    committed_budget: NotRequired[builtins.float]
    governance_context: NotRequired[builtins.str]
    plan_hash: NotRequired[builtins.str]
    purchase_type: NotRequired[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']]
    outcome_status: NotRequired[builtins.str]

class _GetPlanAuditLogsResponsePlansItemGovernedActionsItem(TypedDict, total=False):
    governance_context: Required[builtins.str]
    purchase_type: Required[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']]
    status: Required[Literal['active', 'suspended', 'completed']]
    committed: Required[builtins.float]
    check_count: Required[builtins.int]
    seller_reference: NotRequired[builtins.str]

class _ExternalCoreProductPublisherPropertiesItemVariant1(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    selection_type: Required[Literal['all']]

class _ExternalCoreProductPublisherPropertiesItemVariant2(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    selection_type: Required[Literal['by_id']]
    property_ids: Required[builtins.list[builtins.str]]

class _ExternalCoreProductPublisherPropertiesItemVariant3(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    selection_type: Required[Literal['by_tag']]
    property_tags: Required[builtins.list[builtins.str]]

class _ExternalCorePlacement(TypedDict, total=False):
    placement_id: Required[builtins.str]
    name: Required[builtins.str]
    description: NotRequired[builtins.str]
    tags: NotRequired[builtins.list[builtins.str]]
    format_ids: NotRequired[builtins.list[_ExternalCoreFormatId]]

class _ExternalCoreProductPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpm']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['vcpm']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpc']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpcv']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpv']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    max_bid: NotRequired[builtins.bool]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: Required[_ExternalCoreProductPricingOptionsItemVariant5Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant6(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpp']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: Required[_ExternalCoreProductPricingOptionsItemVariant6Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant7(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['cpa']]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    event_source_id: NotRequired[builtins.str]
    currency: Required[builtins.str]
    fixed_price: Required[builtins.float]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant8(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['flat_rate']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: NotRequired[_ExternalCoreProductPricingOptionsItemVariant8Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreProductPricingOptionsItemVariant9(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    pricing_model: Required[Literal['time']]
    currency: Required[builtins.str]
    fixed_price: NotRequired[builtins.float]
    floor_price: NotRequired[builtins.float]
    price_guidance: NotRequired[_ExternalPricingOptionsPriceGuidance]
    parameters: Required[_ExternalCoreProductPricingOptionsItemVariant9Parameters]
    min_spend_per_package: NotRequired[builtins.float]
    price_breakdown: NotRequired[_ExternalPricingOptionsPriceBreakdown]
    eligible_adjustments: NotRequired[builtins.list[Literal['fee', 'discount', 'commission', 'settlement']]]

class _ExternalCoreDeliveryForecast(TypedDict, total=False):
    points: Required[builtins.list[_ExternalCoreForecastPoint]]
    forecast_range_unit: NotRequired[Literal['spend', 'availability', 'reach_freq', 'weekly', 'daily', 'clicks', 'conversions', 'package']]
    method: Required[Literal['estimate', 'modeled', 'guaranteed']]
    currency: Required[builtins.str]
    demographic_system: NotRequired[Literal['nielsen', 'barb', 'agf', 'oztam', 'mediametrie', 'custom']]
    demographic: NotRequired[builtins.str]
    measurement_source: NotRequired[builtins.str]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    generated_at: NotRequired[builtins.str]
    valid_until: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreOutcomeMeasurement(TypedDict, total=False):
    type: Required[builtins.str]
    attribution: Required[builtins.str]
    window: NotRequired[_ExternalCoreOutcomeMeasurementWindow]
    reporting: Required[builtins.str]

class _ExternalCoreProductDeliveryMeasurement(TypedDict, total=False):
    provider: Required[builtins.str]
    notes: NotRequired[builtins.str]

class _ExternalCoreCancellationPolicy(TypedDict, total=False):
    notice_period: Required[_ExternalCoreDuration]
    cancellation_fee: Required[_ExternalCoreCancellationPolicyCancellationFee]

class _ExternalCoreReportingCapabilities(TypedDict, total=False):
    available_reporting_frequencies: Required[builtins.list[Literal['hourly', 'daily', 'monthly']]]
    expected_delay_minutes: Required[builtins.int]
    timezone: Required[builtins.str]
    supports_webhooks: Required[builtins.bool]
    available_metrics: Required[builtins.list[Literal['impressions', 'spend', 'clicks', 'ctr', 'video_completions', 'completion_rate', 'conversions', 'conversion_value', 'roas', 'cost_per_acquisition', 'new_to_brand_rate', 'viewability', 'engagement_rate', 'views', 'completed_views', 'leads', 'reach', 'frequency', 'grps', 'quartile_data', 'dooh_metrics', 'cost_per_click']]]
    supports_creative_breakdown: NotRequired[builtins.bool]
    supports_keyword_breakdown: NotRequired[builtins.bool]
    supports_geo_breakdown: NotRequired[_ExternalCoreGeoBreakdownSupport]
    supports_device_type_breakdown: NotRequired[builtins.bool]
    supports_device_platform_breakdown: NotRequired[builtins.bool]
    supports_audience_breakdown: NotRequired[builtins.bool]
    supports_placement_breakdown: NotRequired[builtins.bool]
    date_range_support: Required[Literal['date_range', 'lifetime_only']]
    measurement_windows: NotRequired[builtins.list[_ExternalCoreMeasurementWindow]]

class _ExternalCoreCreativePolicy(TypedDict, total=False):
    co_branding: Required[Literal['required', 'optional', 'none']]
    landing_page: Required[Literal['any', 'retailer_site_only', 'must_include_retailer']]
    templates_available: Required[builtins.bool]
    provenance_required: NotRequired[builtins.bool]

class _ExternalCoreProductDataProviderSignalsItemVariant1(TypedDict, total=False):
    data_provider_domain: Required[builtins.str]
    selection_type: Required[Literal['all']]

class _ExternalCoreProductDataProviderSignalsItemVariant2(TypedDict, total=False):
    data_provider_domain: Required[builtins.str]
    selection_type: Required[Literal['by_id']]
    signal_ids: Required[builtins.list[builtins.str]]

class _ExternalCoreProductDataProviderSignalsItemVariant3(TypedDict, total=False):
    data_provider_domain: Required[builtins.str]
    selection_type: Required[Literal['by_tag']]
    signal_tags: Required[builtins.list[builtins.str]]

class _ExternalCoreProductMetricOptimization(TypedDict, total=False):
    supported_metrics: Required[builtins.list[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]]
    supported_reach_units: NotRequired[builtins.list[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]]
    supported_view_durations: NotRequired[builtins.list[builtins.float]]
    supported_targets: NotRequired[builtins.list[Literal['cost_per', 'threshold_rate']]]

class _ExternalCoreMeasurementReadiness(TypedDict, total=False):
    status: Required[Literal['insufficient', 'minimum', 'good', 'excellent']]
    required_event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    missing_event_types: NotRequired[builtins.list[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]]
    issues: NotRequired[builtins.list[_ExternalCoreDiagnosticIssue]]
    notes: NotRequired[builtins.str]

class _ExternalCoreProductConversionTracking(TypedDict, total=False):
    action_sources: NotRequired[builtins.list[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]]
    supported_targets: NotRequired[builtins.list[Literal['cost_per', 'per_ad_spend', 'maximize_value']]]
    platform_managed: NotRequired[builtins.bool]

class _ExternalCoreProductCatalogMatch(TypedDict, total=False):
    matched_gtins: NotRequired[builtins.list[builtins.str]]
    matched_ids: NotRequired[builtins.list[builtins.str]]
    matched_count: NotRequired[builtins.int]
    submitted_count: Required[builtins.int]

class _ExternalCoreProductProductCard(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

class _ExternalCoreProductProductCardDetailed(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

class _ExternalCoreCollectionSelector(TypedDict, total=False):
    publisher_domain: Required[builtins.str]
    collection_ids: Required[builtins.list[builtins.str]]

class _ExternalCoreInstallment(TypedDict, total=False):
    installment_id: Required[builtins.str]
    collection_id: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    season: NotRequired[builtins.str]
    installment_number: NotRequired[builtins.str]
    scheduled_at: NotRequired[builtins.str]
    status: NotRequired[Literal['scheduled', 'tentative', 'live', 'postponed', 'cancelled', 'aired', 'published']]
    duration_seconds: NotRequired[builtins.int]
    flexible_end: NotRequired[builtins.bool]
    valid_until: NotRequired[builtins.str]
    content_rating: NotRequired[_ExternalCoreContentRating]
    topics: NotRequired[builtins.list[builtins.str]]
    special: NotRequired[_ExternalCoreSpecial]
    guest_talent: NotRequired[builtins.list[_ExternalCoreTalent]]
    ad_inventory: NotRequired[_ExternalCoreAdInventoryConfig]
    deadlines: NotRequired[_ExternalCoreInstallmentDeadlines]
    derivative_of: NotRequired[_ExternalCoreInstallmentDerivativeOf]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreProductTrustedMatch(TypedDict, total=False):
    context_match: Required[builtins.bool]
    identity_match: NotRequired[builtins.bool]
    response_types: NotRequired[builtins.list[Literal['activation', 'catalog_items', 'creative', 'deal']]]
    dynamic_brands: NotRequired[builtins.bool]
    providers: NotRequired[builtins.list[_ExternalCoreProductTrustedMatchProvidersItem]]

class _ExternalCoreProductMaterialSubmission(TypedDict, total=False):
    url: NotRequired[builtins.str]
    email: NotRequired[builtins.str]
    instructions: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreCatalogFieldMapping(TypedDict, total=False):
    feed_field: NotRequired[builtins.str]
    catalog_field: NotRequired[builtins.str]
    asset_group_id: NotRequired[builtins.str]
    value: NotRequired[Any]
    transform: NotRequired[Literal['date', 'divide', 'boolean', 'split']]
    format: NotRequired[builtins.str]
    timezone: NotRequired[builtins.str]
    by: NotRequired[builtins.float]
    separator: NotRequired[builtins.str]
    default: NotRequired[Any]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreProductFiltersBudgetRange(TypedDict, total=False):
    min: NotRequired[builtins.float]
    max: NotRequired[builtins.float]
    currency: Required[builtins.str]

class _ExternalCoreProductFiltersMetrosItem(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    code: Required[builtins.str]

class _ExternalCoreProductFiltersTrustedMatch(TypedDict, total=False):
    providers: NotRequired[builtins.list[_ExternalCoreProductFiltersTrustedMatchProvidersItem]]
    response_types: NotRequired[builtins.list[Literal['activation', 'catalog_items', 'creative', 'deal']]]

class _ExternalCoreProductFiltersRequiredGeoTargetingItem(TypedDict, total=False):
    level: Required[Literal['country', 'region', 'metro', 'postal_area']]
    system: NotRequired[builtins.str]

class _ExternalCoreProductFiltersSignalTargetingItemVariant1(TypedDict, total=False):
    signal_id: Required[_ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

class _ExternalCoreProductFiltersSignalTargetingItemVariant2(TypedDict, total=False):
    signal_id: Required[_ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

class _ExternalCoreProductFiltersSignalTargetingItemVariant3(TypedDict, total=False):
    signal_id: Required[_ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant1 | _ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

class _ExternalCoreProductFiltersPostalAreasItem(TypedDict, total=False):
    system: Required[Literal['us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    values: Required[builtins.list[builtins.str]]

class _ExternalCoreProductFiltersGeoProximityItemVariant1(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: Required[_ExternalCoreProductFiltersGeoProximityItemVariant1TravelTime]
    transport_mode: Required[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant1Radius]
    geometry: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant1Geometry]

class _ExternalCoreProductFiltersGeoProximityItemVariant2(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant2TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: Required[_ExternalCoreProductFiltersGeoProximityItemVariant2Radius]
    geometry: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant2Geometry]

class _ExternalCoreProductFiltersGeoProximityItemVariant3(TypedDict, total=False):
    lat: NotRequired[builtins.float]
    lng: NotRequired[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant3TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreProductFiltersGeoProximityItemVariant3Radius]
    geometry: Required[_ExternalCoreProductFiltersGeoProximityItemVariant3Geometry]

class _ExternalCoreProductFiltersKeywordsItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: NotRequired[Literal['broad', 'phrase', 'exact']]

class _ExternalCoreProductAllocation(TypedDict, total=False):
    product_id: Required[builtins.str]
    allocation_percentage: Required[builtins.float]
    pricing_option_id: NotRequired[builtins.str]
    rationale: NotRequired[builtins.str]
    sequence: NotRequired[builtins.int]
    tags: NotRequired[builtins.list[builtins.str]]
    start_time: NotRequired[builtins.str]
    end_time: NotRequired[builtins.str]
    daypart_targets: NotRequired[builtins.list[_ExternalCoreDaypartTarget]]
    forecast: NotRequired[_ExternalCoreDeliveryForecast]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreInsertionOrder(TypedDict, total=False):
    io_id: Required[builtins.str]
    terms: NotRequired[_ExternalCoreInsertionOrderTerms]
    terms_url: NotRequired[builtins.str]
    signing_url: NotRequired[builtins.str]
    requires_signature: Required[builtins.bool]

class _ExternalCoreProposalTotalBudgetGuidance(TypedDict, total=False):
    min: NotRequired[builtins.float]
    recommended: NotRequired[builtins.float]
    max: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]

class _GetProductsResponseIncompleteItemEstimatedWait(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _GetRightsResponseRightsItemExclusivityStatus(TypedDict, total=False):
    available: NotRequired[builtins.bool]
    existing_exclusives: NotRequired[builtins.list[builtins.str]]

class _ExternalBrandRightsPricingOption(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm', 'vcpm', 'cpc', 'cpcv', 'cpv', 'cpp', 'cpa', 'flat_rate', 'time']]
    price: Required[builtins.float]
    currency: Required[builtins.str]
    uses: Required[builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]]
    period: NotRequired[Literal['daily', 'weekly', 'monthly', 'quarterly', 'annual', 'one_time']]
    impression_cap: NotRequired[builtins.int]
    overage_cpm: NotRequired[builtins.float]
    description: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetRightsResponseRightsItemPreviewAssetsItem(TypedDict, total=False):
    url: Required[builtins.str]
    usage: NotRequired[builtins.str]

class _GetSignalsResponseSignalsItemSignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _GetSignalsResponseSignalsItemSignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _GetSignalsResponseSignalsItemRange(TypedDict, total=False):
    min: Required[builtins.float]
    max: Required[builtins.float]

class _GetSignalsResponseSignalsItemDeploymentsItemVariant1(TypedDict, total=False):
    type: Required[Literal['platform']]
    platform: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_GetSignalsResponseSignalsItemDeploymentsItemVariant1ActivationKeyVariant1 | _GetSignalsResponseSignalsItemDeploymentsItemVariant1ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

class _GetSignalsResponseSignalsItemDeploymentsItemVariant2(TypedDict, total=False):
    type: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    account: NotRequired[builtins.str]
    is_live: Required[builtins.bool]
    activation_key: NotRequired[_GetSignalsResponseSignalsItemDeploymentsItemVariant2ActivationKeyVariant1 | _GetSignalsResponseSignalsItemDeploymentsItemVariant2ActivationKeyVariant2]
    estimated_activation_duration_minutes: NotRequired[builtins.float]
    deployed_at: NotRequired[builtins.str]

class _GetSignalsResponseSignalsItemPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetSignalsResponseSignalsItemPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetSignalsResponseSignalsItemPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetSignalsResponseSignalsItemPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetSignalsResponseSignalsItemPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_GetSignalsResponseSignalsItemPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsContentStandardsCalibrationExemplars(TypedDict, total=False):
    fail: NotRequired[builtins.list[_ExternalContentStandardsArtifact]]

class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ExternalContentStandardsContentStandardsPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatRendersItemVariant1(TypedDict, total=False):
    role: Required[builtins.str]
    parameters_from_format_id: NotRequired[builtins.bool]
    dimensions: Required[_ExternalCoreFormatRendersItemVariant1Dimensions]

class _ExternalCoreFormatRendersItemVariant2(TypedDict, total=False):
    role: Required[builtins.str]
    parameters_from_format_id: Required[Literal[True]]
    dimensions: NotRequired[_ExternalCoreFormatRendersItemVariant2Dimensions]

class _ExternalCoreFormatAssetsItemVariant1(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['image']]
    requirements: NotRequired[_ExternalCoreRequirementsImageAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant2(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['video']]
    requirements: NotRequired[_ExternalCoreRequirementsVideoAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant3(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['audio']]
    requirements: NotRequired[_ExternalCoreRequirementsAudioAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant4(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['text']]
    requirements: NotRequired[_ExternalCoreRequirementsTextAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant5(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['markdown']]
    requirements: NotRequired[_ExternalCoreRequirementsMarkdownAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant6(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['html']]
    requirements: NotRequired[_ExternalCoreRequirementsHtmlAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant7(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['css']]
    requirements: NotRequired[_ExternalCoreRequirementsCssAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant8(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['javascript']]
    requirements: NotRequired[_ExternalCoreRequirementsJavascriptAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant9(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['vast']]
    requirements: NotRequired[_ExternalCoreRequirementsVastAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant10(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['daast']]
    requirements: NotRequired[_ExternalCoreRequirementsDaastAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant11(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['url']]
    requirements: NotRequired[_ExternalCoreRequirementsUrlAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant12(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['webhook']]
    requirements: NotRequired[_ExternalCoreRequirementsWebhookAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant13(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['brief']]

class _ExternalCoreFormatAssetsItemVariant14(TypedDict, total=False):
    item_type: Required[Literal['individual']]
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['catalog']]
    requirements: NotRequired[_ExternalCoreRequirementsCatalogRequirements]

class _ExternalCoreFormatAssetsItemVariant15(TypedDict, total=False):
    item_type: Required[Literal['repeatable_group']]
    asset_group_id: Required[builtins.str]
    required: Required[builtins.bool]
    min_count: Required[builtins.int]
    max_count: Required[builtins.int]
    selection_mode: NotRequired[Literal['sequential', 'optimize']]
    assets: Required[builtins.list[_ExternalCoreFormatAssetsItemVariant15AssetsItemVariant1 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant2 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant3 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant4 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant5 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant6 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant7 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant8 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant9 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant10 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant11 | _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant12]]

class _ExternalCoreFormatFormatCard(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatAccessibility(TypedDict, total=False):
    wcag_level: Required[Literal['A', 'AA', 'AAA']]
    requires_accessible_assets: NotRequired[builtins.bool]

class _ExternalCoreFormatDisclosureCapabilitiesItem(TypedDict, total=False):
    position: Required[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']]
    persistence: Required[builtins.list[Literal['continuous', 'initial', 'flexible']]]

class _ExternalCoreFormatFormatCardDetailed(TypedDict, total=False):
    format_id: Required[_ExternalCoreFormatId]
    manifest: Required[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreFormatPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ExternalCoreFormatPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreCreativeFiltersAccountsItemVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ExternalCoreCreativeFiltersAccountsItemVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ListCreativesResponseQuerySummarySortApplied(TypedDict, total=False):
    field: NotRequired[builtins.str]
    direction: NotRequired[Literal['asc', 'desc']]

class _ExternalCoreCreativeVariable(TypedDict, total=False):
    variable_id: Required[builtins.str]
    name: Required[builtins.str]
    variable_type: Required[Literal['text', 'image', 'video', 'audio', 'url', 'number', 'boolean', 'color', 'date']]
    default_value: NotRequired[builtins.str]
    required: NotRequired[builtins.bool]

class _ListCreativesResponseCreativesItemAssignments(TypedDict, total=False):
    assignment_count: Required[builtins.int]
    assigned_packages: NotRequired[builtins.list[_ListCreativesResponseCreativesItemAssignmentsAssignedPackagesItem]]

class _ListCreativesResponseCreativesItemSnapshot(TypedDict, total=False):
    as_of: Required[builtins.str]
    staleness_seconds: Required[builtins.int]
    impressions: Required[builtins.int]
    last_served: NotRequired[builtins.str]

class _ListCreativesResponseCreativesItemItemsItemVariant1(TypedDict, total=False):
    asset_kind: Required[Literal['media']]
    asset_type: Required[builtins.str]
    asset_id: Required[builtins.str]
    content_uri: Required[builtins.str]

class _ListCreativesResponseCreativesItemItemsItemVariant2(TypedDict, total=False):
    asset_kind: Required[Literal['text']]
    asset_type: Required[builtins.str]
    asset_id: Required[builtins.str]
    content: Required[builtins.str | builtins.list[builtins.str]]

class _ListCreativesResponseCreativesItemPricingOptionsItemVariant1(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['cpm']]
    cpm: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ListCreativesResponseCreativesItemPricingOptionsItemVariant2(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['percent_of_media']]
    percent: Required[builtins.float]
    max_cpm: NotRequired[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ListCreativesResponseCreativesItemPricingOptionsItemVariant3(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['flat_fee']]
    amount: Required[builtins.float]
    period: Required[Literal['monthly', 'quarterly', 'annual', 'campaign']]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ListCreativesResponseCreativesItemPricingOptionsItemVariant4(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['per_unit']]
    unit: Required[builtins.str]
    unit_price: Required[builtins.float]
    currency: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ListCreativesResponseCreativesItemPricingOptionsItemVariant5(TypedDict, total=False):
    pricing_option_id: Required[builtins.str]
    model: Required[Literal['custom']]
    description: Required[builtins.str]
    metadata: Required[_ListCreativesResponseCreativesItemPricingOptionsItemVariant5Metadata]
    currency: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreUserMatch(TypedDict, total=False):
    uids: NotRequired[builtins.list[_ExternalCoreUserMatchUidsItem]]
    hashed_email: NotRequired[builtins.str]
    hashed_phone: NotRequired[builtins.str]
    click_id: NotRequired[builtins.str]
    click_id_type: NotRequired[builtins.str]
    client_ip: NotRequired[builtins.str]
    client_user_agent: NotRequired[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreEventCustomData(TypedDict, total=False):
    value: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]
    order_id: NotRequired[builtins.str]
    content_ids: NotRequired[builtins.list[builtins.str]]
    content_type: NotRequired[builtins.str]
    content_name: NotRequired[builtins.str]
    content_category: NotRequired[builtins.str]
    num_items: NotRequired[builtins.int]
    search_string: NotRequired[builtins.str]
    contents: NotRequired[builtins.list[_ExternalCoreEventCustomDataContentsItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _PackageRequestOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_PackageRequestOptimizationGoalsItemVariant1TargetFrequencyWindow]

class _PackageRequestOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _PackageRequestOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

class _PackageRequestOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

class _PackageRequestOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _PackageRequestOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

class _PackageRequestOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

class _PackageRequestOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_PackageRequestOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_PackageRequestOptimizationGoalsItemVariant2AttributionWindowPostView]

class _ExternalCoreTargetingGeoMetrosItem(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    values: Required[builtins.list[builtins.str]]

class _ExternalCoreTargetingGeoMetrosExcludeItem(TypedDict, total=False):
    system: Required[Literal['nielsen_dma', 'uk_itl1', 'uk_itl2', 'eurostat_nuts2', 'custom']]
    values: Required[builtins.list[builtins.str]]

class _ExternalCoreTargetingGeoPostalAreasItem(TypedDict, total=False):
    system: Required[Literal['us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    values: Required[builtins.list[builtins.str]]

class _ExternalCoreTargetingGeoPostalAreasExcludeItem(TypedDict, total=False):
    system: Required[Literal['us_zip', 'us_zip_plus_four', 'gb_outward', 'gb_full', 'ca_fsa', 'ca_full', 'de_plz', 'fr_code_postal', 'au_postcode', 'ch_plz', 'at_plz']]
    values: Required[builtins.list[builtins.str]]

class _ExternalCoreDaypartTarget(TypedDict, total=False):
    days: Required[builtins.list[Literal['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']]]
    start_hour: Required[builtins.int]
    end_hour: Required[builtins.int]
    label: NotRequired[builtins.str]

class _ExternalCoreCollectionListRef(TypedDict, total=False):
    agent_url: Required[builtins.str]
    list_id: Required[builtins.str]
    auth_token: NotRequired[builtins.str]

class _ExternalCoreTargetingAgeRestriction(TypedDict, total=False):
    min: Required[builtins.int]
    verification_required: NotRequired[builtins.bool]
    accepted_methods: NotRequired[builtins.list[Literal['facial_age_estimation', 'id_document', 'digital_id', 'credit_card', 'world_id']]]

class _ExternalCoreTargetingStoreCatchmentsItem(TypedDict, total=False):
    catalog_id: Required[builtins.str]
    store_ids: NotRequired[builtins.list[builtins.str]]
    catchment_ids: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreTargetingGeoProximityItemVariant1(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: Required[_ExternalCoreTargetingGeoProximityItemVariant1TravelTime]
    transport_mode: Required[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant1Radius]
    geometry: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant1Geometry]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreTargetingGeoProximityItemVariant2(TypedDict, total=False):
    lat: Required[builtins.float]
    lng: Required[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant2TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: Required[_ExternalCoreTargetingGeoProximityItemVariant2Radius]
    geometry: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant2Geometry]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreTargetingGeoProximityItemVariant3(TypedDict, total=False):
    lat: NotRequired[builtins.float]
    lng: NotRequired[builtins.float]
    label: NotRequired[builtins.str]
    travel_time: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant3TravelTime]
    transport_mode: NotRequired[Literal['walking', 'cycling', 'driving', 'public_transport']]
    radius: NotRequired[_ExternalCoreTargetingGeoProximityItemVariant3Radius]
    geometry: Required[_ExternalCoreTargetingGeoProximityItemVariant3Geometry]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreTargetingKeywordTargetsItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]
    bid_price: NotRequired[builtins.float]

class _ExternalCoreTargetingNegativeKeywordsItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

class _ExternalCoreMeasurementTermsBillingMeasurement(TypedDict, total=False):
    vendor: Required[_ExternalCoreBrandRef]
    max_variance_percent: NotRequired[builtins.float]
    measurement_window: NotRequired[builtins.str]

class _ExternalCoreMeasurementTermsMakegoodPolicy(TypedDict, total=False):
    available_remedies: Required[builtins.list[Literal['additional_delivery', 'credit', 'invoice_adjustment']]]

class _ExternalCoreCreativeAssetInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _PreviewCreativeRequestRequestsItemInputsItem(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponsePreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponsePreviewsItemRendersItemVariant1Embedding]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponsePreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponsePreviewsItemRendersItemVariant2Embedding]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponsePreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponsePreviewsItemRendersItemVariant3Embedding]

class _PreviewCreativeResponsePreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant1Response(TypedDict, total=False):
    previews: Required[builtins.list[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

class _PreviewCreativeResponseResultsItemVariant2Response(TypedDict, total=False):
    previews: Required[builtins.list[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItem]]
    interactive_url: NotRequired[builtins.str]
    expires_at: Required[builtins.str]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponsePreviewsItem2RendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponsePreviewsItem2RendersItemVariant1Embedding]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponsePreviewsItem2RendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponsePreviewsItem2RendersItemVariant2Embedding]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponsePreviewsItem2RendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponsePreviewsItem2RendersItemVariant3Embedding]

class _ReportPlanOutcomeRequestSellerResponsePackagesItem(TypedDict, total=False):
    budget: NotRequired[builtins.float]

class _ReportPlanOutcomeRequestDeliveryReportingPeriod(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

class _ReportUsageRequestUsageItemAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _ReportUsageRequestUsageItemAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _ExternalSponsoredIntelligenceSiIdentityPrivacyPolicyAcknowledged(TypedDict, total=False):
    brand_policy_url: NotRequired[builtins.str]
    brand_policy_version: NotRequired[builtins.str]

class _ExternalSponsoredIntelligenceSiIdentityUser(TypedDict, total=False):
    email: NotRequired[builtins.str]
    name: NotRequired[builtins.str]
    locale: NotRequired[builtins.str]
    phone: NotRequired[builtins.str]
    shipping_address: NotRequired[_ExternalSponsoredIntelligenceSiIdentityUserShippingAddress]

class _ExternalSponsoredIntelligenceSiCapabilitiesModalities(TypedDict, total=False):
    conversational: NotRequired[builtins.bool]
    voice: NotRequired[builtins.bool | _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVoiceVariant2]
    video: NotRequired[builtins.bool | _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVideoVariant2]
    avatar: NotRequired[builtins.bool | _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesAvatarVariant2]

class _ExternalSponsoredIntelligenceSiCapabilitiesComponents(TypedDict, total=False):
    standard: NotRequired[builtins.list[Literal['text', 'link', 'image', 'product_card', 'carousel', 'action_button']]]
    extensions: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalSponsoredIntelligenceSiCapabilitiesCommerce(TypedDict, total=False):
    acp_checkout: NotRequired[builtins.bool]

class _ExternalSponsoredIntelligenceSiCapabilitiesA2ui(TypedDict, total=False):
    supported: NotRequired[builtins.bool]
    catalogs: NotRequired[builtins.list[builtins.str]]

class _ExternalSponsoredIntelligenceSiUiElement(TypedDict, total=False):
    type: Required[Literal['text', 'link', 'image', 'product_card', 'carousel', 'action_button', 'app_handoff', 'integration_actions']]
    data: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalA2uiSurface(TypedDict, total=False):
    surfaceId: Required[builtins.str]
    catalogId: NotRequired[builtins.str]
    components: Required[builtins.list[_ExternalA2uiComponent]]
    rootId: NotRequired[builtins.str]
    dataModel: NotRequired[builtins.dict[builtins.str, Any]]

class _SiSendMessageResponseHandoffIntent(TypedDict, total=False):
    action: NotRequired[builtins.str]
    product: NotRequired[builtins.dict[builtins.str, Any]]
    price: NotRequired[_SiSendMessageResponseHandoffIntentPrice]

class _SiSendMessageResponseHandoffContextForCheckout(TypedDict, total=False):
    conversation_summary: NotRequired[builtins.str]
    applied_offers: NotRequired[builtins.list[builtins.str]]

class _SiTerminateSessionRequestTerminationContextTransactionIntent(TypedDict, total=False):
    action: NotRequired[Literal['purchase', 'subscribe']]
    product: NotRequired[builtins.dict[builtins.str, Any]]

class _SyncAccountsResponseAccountsItemSetup(TypedDict, total=False):
    url: NotRequired[builtins.str]
    message: Required[builtins.str]
    expires_at: NotRequired[builtins.str]

class _SyncAccountsResponseAccountsItemCreditLimit(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

class _ExternalCoreAudienceMember(TypedDict, total=False):
    external_id: Required[builtins.str]
    hashed_email: NotRequired[builtins.str]
    hashed_phone: NotRequired[builtins.str]
    uids: NotRequired[builtins.list[_ExternalCoreAudienceMemberUidsItem]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _SyncAudiencesResponseAudiencesItemMatchBreakdownItem(TypedDict, total=False):
    id_type: Required[Literal['hashed_email', 'hashed_phone', 'rampid', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'other']]
    submitted: Required[builtins.int]
    matched: Required[builtins.int]
    match_rate: Required[builtins.float]

class _SyncCatalogsResponseCatalogsItemItemIssuesItem(TypedDict, total=False):
    item_id: Required[builtins.str]
    status: Required[Literal['approved', 'pending', 'rejected', 'warning']]
    reasons: NotRequired[builtins.list[builtins.str]]

class _SyncEventSourcesResponseEventSourcesItemSetup(TypedDict, total=False):
    snippet: NotRequired[builtins.str]
    snippet_type: NotRequired[Literal['javascript', 'html', 'pixel_url', 'server_only']]
    instructions: NotRequired[builtins.str]

class _ExternalCoreEventSourceHealth(TypedDict, total=False):
    status: Required[Literal['insufficient', 'minimum', 'good', 'excellent']]
    detail: NotRequired[_ExternalCoreEventSourceHealthDetail]
    match_rate: NotRequired[builtins.float]
    last_event_at: NotRequired[builtins.str]
    evaluated_at: NotRequired[builtins.str]
    events_received_24h: NotRequired[builtins.int]
    issues: NotRequired[builtins.list[_ExternalCoreDiagnosticIssue]]

class _SyncGovernanceRequestAccountsItemAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _SyncGovernanceRequestAccountsItemAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _SyncGovernanceRequestAccountsItemGovernanceAgentsItem(TypedDict, total=False):
    url: Required[builtins.str]
    authentication: Required[_SyncGovernanceRequestAccountsItemGovernanceAgentsItemAuthentication]
    categories: NotRequired[builtins.list[builtins.str]]

class _SyncGovernanceResponseAccountsItemAccountVariant1(TypedDict, total=False):
    account_id: Required[builtins.str]

class _SyncGovernanceResponseAccountsItemAccountVariant2(TypedDict, total=False):
    brand: Required[_ExternalCoreBrandRef]
    operator: Required[builtins.str]
    sandbox: NotRequired[builtins.bool]

class _SyncGovernanceResponseAccountsItemGovernanceAgentsItem(TypedDict, total=False):
    url: Required[builtins.str]
    categories: NotRequired[builtins.list[builtins.str]]

class _SyncPlansRequestPlansItemBudgetVariant1(TypedDict, total=False):
    total: Required[builtins.float]
    currency: Required[builtins.str]
    per_seller_max_pct: NotRequired[builtins.float]
    reallocation_threshold: Required[builtins.float]
    reallocation_unlimited: NotRequired[builtins.bool]
    allocations: NotRequired[builtins.dict[builtins.str, _SyncPlansRequestPlansItemBudgetVariant1AllocationsValue]]

class _SyncPlansRequestPlansItemBudgetVariant2(TypedDict, total=False):
    total: Required[builtins.float]
    currency: Required[builtins.str]
    per_seller_max_pct: NotRequired[builtins.float]
    reallocation_threshold: NotRequired[builtins.float]
    reallocation_unlimited: Required[Literal[True]]
    allocations: NotRequired[builtins.dict[builtins.str, _SyncPlansRequestPlansItemBudgetVariant2AllocationsValue]]

class _SyncPlansRequestPlansItemChannels(TypedDict, total=False):
    required: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    allowed: NotRequired[builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']]]
    mix_targets: NotRequired[builtins.dict[builtins.str, _SyncPlansRequestPlansItemChannelsMixTargetsValue]]

class _SyncPlansRequestPlansItemFlight(TypedDict, total=False):
    start: Required[builtins.str]
    end: Required[builtins.str]

class _ExternalGovernanceAudienceConstraints(TypedDict, total=False):
    include: NotRequired[builtins.list[_ExternalGovernanceAudienceConstraintsIncludeItemVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant2 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant3 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant4]]
    exclude: NotRequired[builtins.list[_ExternalGovernanceAudienceConstraintsExcludeItemVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant2 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant3 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant4]]

class _SyncPlansRequestPlansItemDelegationsItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    authority: Required[Literal['full', 'execute_only', 'propose_only']]
    budget_limit: NotRequired[_SyncPlansRequestPlansItemDelegationsItemBudgetLimit]
    markets: NotRequired[builtins.list[builtins.str]]
    expires_at: NotRequired[builtins.str]

class _SyncPlansRequestPlansItemPortfolio(TypedDict, total=False):
    member_plan_ids: Required[builtins.list[builtins.str]]
    total_budget_cap: NotRequired[_SyncPlansRequestPlansItemPortfolioTotalBudgetCap]
    shared_policy_ids: NotRequired[builtins.list[builtins.str]]
    shared_exclusions: NotRequired[builtins.list[_ExternalGovernancePolicyEntry]]

class _SyncPlansResponsePlansItemCategoriesItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    status: Required[Literal['active', 'inactive']]

class _SyncPlansResponsePlansItemResolvedPoliciesItem(TypedDict, total=False):
    policy_id: Required[builtins.str]
    source: Required[Literal['explicit', 'auto_applied']]
    enforcement: Required[Literal['must', 'should', 'may']]
    reason: NotRequired[builtins.str]

class _TasksGetResponseErrorDetails(TypedDict, total=False):
    protocol: NotRequired[Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']]
    operation: NotRequired[builtins.str]
    specific_context: NotRequired[builtins.dict[builtins.str, Any]]

class _TasksListResponseQuerySummaryDomainBreakdown(TypedDict, total=False):
    signals: NotRequired[builtins.int]

class _TasksListResponseQuerySummarySortApplied(TypedDict, total=False):
    field: Required[builtins.str]
    direction: Required[Literal['asc', 'desc']]

class _UpdateCollectionListRequestBaseCollectionsItemVariant1IdentifiersItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant1(TypedDict, total=False):
    type: Required[Literal['url']]
    value: Required[builtins.str]
    language: NotRequired[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2(TypedDict, total=False):
    property_rid: Required[builtins.str]
    artifact_id: Required[builtins.str]
    variant_id: NotRequired[builtins.str]
    format_id: NotRequired[_ExternalCoreFormatId]
    url: NotRequired[builtins.str]
    published_time: NotRequired[builtins.str]
    last_update_time: NotRequired[builtins.str]
    assets: Required[builtins.list[_UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant1 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4]]
    metadata: NotRequired[_UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2Metadata]
    provenance: NotRequired[_ExternalCoreProvenance]
    identifiers: NotRequired[_UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2Identifiers]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['metric']]
    metric: Required[Literal['clicks', 'views', 'completed_views', 'viewed_seconds', 'attention_seconds', 'attention_score', 'engagements', 'follows', 'saves', 'profile_visits', 'reach']]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    target_frequency: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequency]
    view_duration_seconds: NotRequired[builtins.float]
    target: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant1 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant2]
    priority: NotRequired[builtins.int]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['event']]
    event_sources: Required[builtins.list[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2EventSourcesItem]]
    target: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant1 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant2 | _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant3]
    attribution_window: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindow]
    priority: NotRequired[builtins.int]

class _ExternalMediaBuyPackageUpdateKeywordTargetsAddItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]
    bid_price: NotRequired[builtins.float]

class _ExternalMediaBuyPackageUpdateKeywordTargetsRemoveItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

class _ExternalMediaBuyPackageUpdateNegativeKeywordsAddItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

class _ExternalMediaBuyPackageUpdateNegativeKeywordsRemoveItem(TypedDict, total=False):
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

class _ValidateContentDeliveryRequestRecordsItemBrandContext(TypedDict, total=False):
    brand_id: NotRequired[builtins.str]
    sku_id: NotRequired[builtins.str]

class _ValidateContentDeliveryResponseResultsItemFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    status: Required[Literal['passed', 'failed', 'warning', 'unevaluated']]
    policy_id: NotRequired[builtins.str]
    explanation: NotRequired[builtins.str]
    confidence: NotRequired[builtins.float]

class _ExternalPropertyValidationResultFeaturesItem(TypedDict, total=False):
    feature_id: Required[builtins.str]
    status: Required[Literal['passed', 'failed', 'warning', 'unevaluated']]
    policy_id: NotRequired[builtins.str]
    explanation: NotRequired[builtins.str]
    requirement: NotRequired[_ExternalPropertyValidationResultFeaturesItemRequirement]
    confidence: NotRequired[builtins.float]

class _ExternalPropertyAuthorizationResult(TypedDict, total=False):
    status: Required[Literal['authorized', 'unauthorized', 'unknown']]
    publisher_domain: NotRequired[builtins.str]
    sales_agent_url: NotRequired[builtins.str]
    violation: NotRequired[_ExternalPropertyAuthorizationResultViolation]

class _ExternalCoreProvenanceAiTool(TypedDict, total=False):
    name: Required[builtins.str]
    version: NotRequired[builtins.str]
    provider: NotRequired[builtins.str]

class _ExternalCoreProvenanceDeclaredBy(TypedDict, total=False):
    agent_url: NotRequired[builtins.str]
    role: Required[Literal['creator', 'advertiser', 'agency', 'platform', 'tool']]

class _ExternalCoreProvenanceC2pa(TypedDict, total=False):
    manifest_url: Required[builtins.str]

class _ExternalCoreProvenanceDisclosure(TypedDict, total=False):
    required: Required[builtins.bool]
    jurisdictions: NotRequired[builtins.list[_ExternalCoreProvenanceDisclosureJurisdictionsItem]]

class _ExternalCoreProvenanceVerificationItem(TypedDict, total=False):
    verified_by: Required[builtins.str]
    verified_time: NotRequired[builtins.str]
    result: Required[Literal['authentic', 'ai_generated', 'ai_modified', 'inconclusive']]
    confidence: NotRequired[builtins.float]
    details_url: NotRequired[builtins.str]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponsePreviewPreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_BuildCreativeResponsePreviewPreviewsItemRendersItemVariant1Embedding]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponsePreviewPreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_BuildCreativeResponsePreviewPreviewsItemRendersItemVariant2Embedding]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponsePreviewPreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_BuildCreativeResponsePreviewPreviewsItemRendersItemVariant3Embedding]

class _BuildCreativeResponsePreviewPreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponsePreview2PreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_BuildCreativeResponsePreview2PreviewsItemRendersItemVariant1Embedding]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponsePreview2PreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_BuildCreativeResponsePreview2PreviewsItemRendersItemVariant2Embedding]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_BuildCreativeResponsePreview2PreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_BuildCreativeResponsePreview2PreviewsItemRendersItemVariant3Embedding]

class _BuildCreativeResponsePreview2PreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsArtifactAssetsItemVariant2AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsArtifactAssetsItemVariant3AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalContentStandardsArtifactAssetsItemVariant4AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _ExternalCoreFrequencyCapSuppress(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCoreFrequencyCapWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCorePlannedDeliveryAudienceTargetingItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCollectionCollectionListBaseCollectionsItemVariant1IdentifiersItem(TypedDict, total=False):
    type: Required[Literal['apple_podcast_id', 'spotify_collection_id', 'rss_url', 'podcast_guid', 'amazon_music_id', 'iheart_id', 'podcast_index_id', 'youtube_channel_id', 'youtube_playlist_id', 'amazon_title_id', 'roku_channel_id', 'pluto_channel_id', 'tubi_id', 'peacock_id', 'tiktok_id', 'twitch_channel', 'imdb_id', 'gracenote_id', 'eidr_id', 'domain', 'substack_id']]
    value: Required[builtins.str]

class _Exemplar(TypedDict, total=False):
    scenario: Required[builtins.str]
    explanation: Required[builtins.str]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant1(TypedDict, total=False):
    type: Required[Literal['text']]
    role: NotRequired[Literal['title', 'paragraph', 'heading', 'caption', 'quote', 'list_item', 'description']]
    content: Required[builtins.str]
    content_format: NotRequired[Literal['text/plain', 'text/markdown', 'text/html', 'application/json']]
    language: NotRequired[builtins.str]
    heading_level: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2(TypedDict, total=False):
    type: Required[Literal['image']]
    url: Required[builtins.str]
    access: NotRequired[_CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3]
    alt_text: NotRequired[builtins.str]
    caption: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3(TypedDict, total=False):
    type: Required[Literal['video']]
    url: Required[builtins.str]
    access: NotRequired[_CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'subtitles', 'closed_captions', 'dub', 'generated']]
    thumbnail_url: NotRequired[builtins.str]
    provenance: NotRequired[_ExternalCoreProvenance]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4(TypedDict, total=False):
    type: Required[Literal['audio']]
    url: Required[builtins.str]
    access: NotRequired[_CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2 | _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'closed_captions', 'generated']]
    provenance: NotRequired[_ExternalCoreProvenance]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2Metadata(TypedDict, total=False):
    canonical: NotRequired[builtins.str]
    author: NotRequired[builtins.str]
    keywords: NotRequired[builtins.str]
    open_graph: NotRequired[builtins.dict[builtins.str, Any]]
    twitter_card: NotRequired[builtins.dict[builtins.str, Any]]
    json_ld: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2Identifiers(TypedDict, total=False):
    apple_podcast_id: NotRequired[builtins.str]
    spotify_collection_id: NotRequired[builtins.str]
    podcast_guid: NotRequired[builtins.str]
    youtube_video_id: NotRequired[builtins.str]
    rss_url: NotRequired[builtins.str]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequencyWindow]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostView]

class _ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['fee', 'discount', 'commission', 'settlement']]
    name: Required[builtins.str]
    rate: Required[builtins.float]
    amount: NotRequired[builtins.float]
    description: NotRequired[builtins.str]
    beneficiary: NotRequired[builtins.str]

class _ExternalPricingOptionsPriceBreakdownAdjustmentsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['fee', 'discount', 'commission', 'settlement']]
    name: Required[builtins.str]
    rate: NotRequired[builtins.float]
    amount: Required[builtins.float]
    description: NotRequired[builtins.str]
    beneficiary: NotRequired[builtins.str]

class _ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequencyWindow]

class _ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _ExternalCorePackageOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

class _ExternalCorePackageOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

class _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

class _ExternalCorePackageOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

class _ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostView]

class _ExternalPropertyPropertyListPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTrustedMatch(TypedDict, total=False):
    surfaces: NotRequired[builtins.list[Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'streaming_audio', 'ai_assistant']]]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionCreativeSpecs(TypedDict, total=False):
    vast_versions: NotRequired[builtins.list[builtins.str]]
    mraid_versions: NotRequired[builtins.list[builtins.str]]
    vpaid: NotRequired[builtins.bool]
    simid: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTargeting(TypedDict, total=False):
    geo_countries: NotRequired[builtins.bool]
    geo_regions: NotRequired[builtins.bool]
    geo_metros: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingGeoMetros]
    geo_postal_areas: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingGeoPostalAreas]
    age_restriction: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingAgeRestriction]
    language: NotRequired[builtins.bool]
    keyword_targets: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingKeywordTargets]
    negative_keywords: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingNegativeKeywords]
    geo_proximity: NotRequired[_GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingGeoProximity]

class _GetAdcpCapabilitiesResponseMediaBuyAudienceTargetingMatchingLatencyHours(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]

class _GetAdcpCapabilitiesResponseMediaBuyConversionTrackingAttributionWindowsItem(TypedDict, total=False):
    event_type: NotRequired[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    post_click: Required[builtins.list[_ExternalCoreDuration]]
    post_view: NotRequired[builtins.list[_ExternalCoreDuration]]

class _GetAdcpCapabilitiesResponseGovernancePropertyFeaturesItemRange(TypedDict, total=False):
    min: Required[builtins.float]
    max: Required[builtins.float]

class _GetAdcpCapabilitiesResponseGovernanceCreativeFeaturesItemRange(TypedDict, total=False):
    min: Required[builtins.float]
    max: Required[builtins.float]

class _GetAdcpCapabilitiesResponseSponsoredIntelligenceEndpointTransportsItem(TypedDict, total=False):
    type: Required[Literal['mcp', 'a2a']]
    url: Required[builtins.str]

class _FontRoleVariant2FilesItem(TypedDict, total=False):
    url: Required[builtins.str]
    weight: NotRequired[builtins.int]
    weight_range: NotRequired[builtins.list[builtins.int]]
    style: NotRequired[Literal['normal', 'italic', 'oblique']]

class _ExternalCoreDeliveryMetricsByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _ExternalCoreDeliveryMetricsQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _ExternalCoreDeliveryMetricsDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_ExternalCoreDeliveryMetricsDoohMetricsVenueBreakdownItem]]

class _ExternalCoreDeliveryMetricsViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _ExternalCoreDeliveryMetricsByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _ExternalCoreCreativeVariantByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _ExternalCoreCreativeVariantQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _ExternalCoreCreativeVariantDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_ExternalCoreCreativeVariantDoohMetricsVenueBreakdownItem]]

class _ExternalCoreCreativeVariantViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _ExternalCoreCreativeVariantByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _ExternalCoreCreativeVariantGenerationContext(TypedDict, total=False):
    context_type: NotRequired[builtins.str]
    artifact: NotRequired[_ExternalCoreCreativeVariantGenerationContextArtifact]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByActionSourceItem]]
    content_id: Required[builtins.str]
    content_id_type: NotRequired[Literal['sku', 'gtin', 'offering_id', 'job_id', 'hotel_id', 'flight_id', 'vehicle_id', 'listing_id', 'store_id', 'program_id', 'destination_id', 'app_id']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemByActionSourceItem]]
    creative_id: Required[builtins.str]
    weight: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemByActionSourceItem]]
    keyword: Required[builtins.str]
    match_type: Required[Literal['broad', 'phrase', 'exact']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemByActionSourceItem]]
    geo_level: Required[Literal['country', 'region', 'metro', 'postal_area']]
    system: NotRequired[builtins.str]
    geo_code: Required[builtins.str]
    geo_name: NotRequired[builtins.str]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByActionSourceItem]]
    device_type: Required[Literal['desktop', 'mobile', 'tablet', 'ctv', 'dooh', 'unknown']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByActionSourceItem]]
    device_platform: Required[Literal['ios', 'android', 'windows', 'macos', 'linux', 'chromeos', 'tvos', 'tizen', 'webos', 'fire_os', 'roku_os', 'unknown']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemByActionSourceItem]]
    audience_id: Required[builtins.str]
    audience_source: Required[Literal['synced', 'platform', 'third_party', 'lookalike', 'retargeting', 'unknown']]
    audience_name: NotRequired[builtins.str]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItem(TypedDict, total=False):
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    clicks: NotRequired[builtins.float]
    ctr: NotRequired[builtins.float]
    views: NotRequired[builtins.float]
    completed_views: NotRequired[builtins.float]
    completion_rate: NotRequired[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    cost_per_acquisition: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]
    leads: NotRequired[builtins.float]
    by_event_type: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemByEventTypeItem]]
    grps: NotRequired[builtins.float]
    reach: NotRequired[builtins.float]
    reach_unit: NotRequired[Literal['individuals', 'households', 'devices', 'accounts', 'cookies', 'custom']]
    frequency: NotRequired[builtins.float]
    quartile_data: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemQuartileData]
    dooh_metrics: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetrics]
    viewability: NotRequired[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemViewability]
    engagements: NotRequired[builtins.float]
    follows: NotRequired[builtins.float]
    saves: NotRequired[builtins.float]
    profile_visits: NotRequired[builtins.float]
    engagement_rate: NotRequired[builtins.float]
    cost_per_click: NotRequired[builtins.float]
    by_action_source: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemByActionSourceItem]]
    placement_id: Required[builtins.str]
    placement_name: NotRequired[builtins.str]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemDailyBreakdownItem(TypedDict, total=False):
    date: Required[builtins.str]
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    conversions: NotRequired[builtins.float]
    conversion_value: NotRequired[builtins.float]
    roas: NotRequired[builtins.float]
    new_to_brand_rate: NotRequired[builtins.float]

class _GetMediaBuysResponseMediaBuysItemPackagesItemCancellation(TypedDict, total=False):
    canceled_at: Required[builtins.str]
    canceled_by: Required[Literal['buyer', 'seller']]
    reason: NotRequired[builtins.str]

class _GetMediaBuysResponseMediaBuysItemPackagesItemCreativeApprovalsItem(TypedDict, total=False):
    creative_id: Required[builtins.str]
    approval_status: Required[Literal['pending_review', 'approved', 'rejected']]
    rejection_reason: NotRequired[builtins.str]

class _GetMediaBuysResponseMediaBuysItemPackagesItemSnapshot(TypedDict, total=False):
    as_of: Required[builtins.str]
    staleness_seconds: Required[builtins.int]
    impressions: Required[builtins.float]
    spend: Required[builtins.float]
    currency: NotRequired[builtins.str]
    clicks: NotRequired[builtins.float]
    pacing_index: NotRequired[builtins.float]
    delivery_status: NotRequired[Literal['delivering', 'not_delivering', 'completed', 'budget_exhausted', 'flight_ended', 'goal_met']]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetPlanAuditLogsResponsePlansItemSummaryStatuses(TypedDict, total=False):
    approved: NotRequired[builtins.int]
    denied: NotRequired[builtins.int]
    conditions: NotRequired[builtins.int]
    human_reviewed: NotRequired[builtins.int]

class _GetPlanAuditLogsResponsePlansItemSummaryEscalationsItem(TypedDict, total=False):
    check_id: Required[builtins.str]
    reason: Required[builtins.str]
    resolution: NotRequired[builtins.str]
    resolved_at: NotRequired[builtins.str]

class _GetPlanAuditLogsResponsePlansItemSummaryDriftMetrics(TypedDict, total=False):
    escalation_rate: NotRequired[builtins.float]
    escalation_rate_trend: NotRequired[Literal['increasing', 'stable', 'declining']]
    auto_approval_rate: NotRequired[builtins.float]
    human_override_rate: NotRequired[builtins.float]
    mean_confidence: NotRequired[builtins.float]
    thresholds: NotRequired[_GetPlanAuditLogsResponsePlansItemSummaryDriftMetricsThresholds]

class _GetPlanAuditLogsResponsePlansItemEntriesItemFindingsItem(TypedDict, total=False):
    category_id: Required[builtins.str]
    policy_id: NotRequired[builtins.str]
    severity: Required[Literal['info', 'warning', 'critical']]
    explanation: Required[builtins.str]
    confidence: NotRequired[builtins.float]

class _ExternalPricingOptionsPriceGuidance(TypedDict, total=False):
    p25: NotRequired[builtins.float]
    p50: NotRequired[builtins.float]
    p75: NotRequired[builtins.float]
    p90: NotRequired[builtins.float]

class _ExternalCoreProductPricingOptionsItemVariant5Parameters(TypedDict, total=False):
    view_threshold: Required[builtins.float | _ExternalCoreProductPricingOptionsItemVariant5ParametersViewThresholdVariant2]

class _ExternalCoreProductPricingOptionsItemVariant6Parameters(TypedDict, total=False):
    demographic_system: NotRequired[Literal['nielsen', 'barb', 'agf', 'oztam', 'mediametrie', 'custom']]
    demographic: Required[builtins.str]
    min_points: NotRequired[builtins.float]

class _ExternalCoreProductPricingOptionsItemVariant8Parameters(TypedDict, total=False):
    type: Required[Literal['dooh']]
    sov_percentage: NotRequired[builtins.float]
    loop_duration_seconds: NotRequired[builtins.int]
    min_plays_per_hour: NotRequired[builtins.int]
    venue_package: NotRequired[builtins.str]
    duration_hours: NotRequired[builtins.float]
    daypart: NotRequired[builtins.str]
    estimated_impressions: NotRequired[builtins.int]

class _ExternalCoreProductPricingOptionsItemVariant9Parameters(TypedDict, total=False):
    time_unit: Required[Literal['hour', 'day', 'week', 'month']]
    min_duration: NotRequired[builtins.int]
    max_duration: NotRequired[builtins.int]

class _ExternalCoreForecastPoint(TypedDict, total=False):
    label: NotRequired[builtins.str]
    budget: NotRequired[builtins.float]
    metrics: Required[_ExternalCoreForecastPointMetrics]

class _ExternalCoreOutcomeMeasurementWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCoreDuration(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCoreCancellationPolicyCancellationFee(TypedDict, total=False):
    type: Required[Literal['percent_remaining', 'full_commitment', 'fixed_fee', 'none']]
    rate: NotRequired[builtins.float]
    amount: NotRequired[builtins.float]

class _ExternalCoreGeoBreakdownSupport(TypedDict, total=False):
    country: NotRequired[builtins.bool]
    region: NotRequired[builtins.bool]
    metro: NotRequired[builtins.dict[builtins.str, builtins.bool]]
    postal_area: NotRequired[builtins.dict[builtins.str, builtins.bool]]

class _ExternalCoreMeasurementWindow(TypedDict, total=False):
    window_id: Required[builtins.str]
    description: NotRequired[builtins.str]
    duration_days: Required[builtins.int]
    expected_availability_days: NotRequired[builtins.int]
    is_guarantee_basis: NotRequired[builtins.bool]

class _ExternalCoreDiagnosticIssue(TypedDict, total=False):
    severity: Required[Literal['error', 'warning', 'info']]
    message: Required[builtins.str]

class _ExternalCoreSpecial(TypedDict, total=False):
    name: Required[builtins.str]
    category: NotRequired[Literal['awards', 'championship', 'concert', 'conference', 'election', 'festival', 'gala', 'holiday', 'premiere', 'product_launch', 'reunion', 'tribute']]
    starts: NotRequired[builtins.str]
    ends: NotRequired[builtins.str]

class _ExternalCoreTalent(TypedDict, total=False):
    role: Required[Literal['host', 'guest', 'creator', 'cast', 'narrator', 'producer', 'correspondent', 'commentator', 'analyst']]
    name: Required[builtins.str]
    brand_url: NotRequired[builtins.str]

class _ExternalCoreAdInventoryConfig(TypedDict, total=False):
    expected_breaks: Required[builtins.int]
    total_ad_seconds: NotRequired[builtins.int]
    max_ad_duration_seconds: NotRequired[builtins.int]
    unplanned_breaks: NotRequired[builtins.bool]
    supported_formats: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreInstallmentDeadlines(TypedDict, total=False):
    booking_deadline: NotRequired[builtins.str]
    cancellation_deadline: NotRequired[builtins.str]
    material_deadlines: NotRequired[builtins.list[_ExternalCoreMaterialDeadline]]

class _ExternalCoreInstallmentDerivativeOf(TypedDict, total=False):
    installment_id: Required[builtins.str]
    type: Required[Literal['clip', 'highlight', 'recap', 'trailer', 'bonus']]

class _ExternalCoreProductTrustedMatchProvidersItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    context_match: NotRequired[builtins.bool]
    identity_match: NotRequired[builtins.bool]
    countries: NotRequired[builtins.list[builtins.str]]
    uid_types: NotRequired[builtins.list[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]]

class _ExternalCoreProductFiltersTrustedMatchProvidersItem(TypedDict, total=False):
    agent_url: Required[builtins.str]
    context_match: NotRequired[builtins.bool]
    identity_match: NotRequired[builtins.bool]

class _ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCoreProductFiltersSignalTargetingItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCoreProductFiltersSignalTargetingItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCoreProductFiltersSignalTargetingItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalCoreProductFiltersGeoProximityItemVariant1TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

class _ExternalCoreProductFiltersGeoProximityItemVariant1Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

class _ExternalCoreProductFiltersGeoProximityItemVariant1Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

class _ExternalCoreProductFiltersGeoProximityItemVariant2TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

class _ExternalCoreProductFiltersGeoProximityItemVariant2Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

class _ExternalCoreProductFiltersGeoProximityItemVariant2Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

class _ExternalCoreProductFiltersGeoProximityItemVariant3TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

class _ExternalCoreProductFiltersGeoProximityItemVariant3Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

class _ExternalCoreProductFiltersGeoProximityItemVariant3Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

class _ExternalCoreInsertionOrderTerms(TypedDict, total=False):
    advertiser: NotRequired[builtins.str]
    publisher: NotRequired[builtins.str]
    total_budget: NotRequired[_ExternalCoreInsertionOrderTermsTotalBudget]
    flight_start: NotRequired[builtins.str]
    flight_end: NotRequired[builtins.str]
    payment_terms: NotRequired[Literal['net_30', 'net_60', 'net_90', 'prepaid', 'due_on_receipt']]

class _GetSignalsResponseSignalsItemDeploymentsItemVariant1ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

class _GetSignalsResponseSignalsItemDeploymentsItemVariant1ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

class _GetSignalsResponseSignalsItemDeploymentsItemVariant2ActivationKeyVariant1(TypedDict, total=False):
    type: Required[Literal['segment_id']]
    segment_id: Required[builtins.str]

class _GetSignalsResponseSignalsItemDeploymentsItemVariant2ActivationKeyVariant2(TypedDict, total=False):
    type: Required[Literal['key_value']]
    key: Required[builtins.str]
    value: Required[builtins.str]

class _GetSignalsResponseSignalsItemPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

class _ExternalContentStandardsContentStandardsPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

class _ExternalCoreFormatRendersItemVariant1Dimensions(TypedDict, total=False):
    width: NotRequired[builtins.float]
    height: NotRequired[builtins.float]
    min_width: NotRequired[builtins.float]
    min_height: NotRequired[builtins.float]
    max_width: NotRequired[builtins.float]
    max_height: NotRequired[builtins.float]
    unit: NotRequired[Literal['px', 'dp', 'inches', 'cm', 'mm', 'pt']]
    responsive: NotRequired[_ExternalCoreFormatRendersItemVariant1DimensionsResponsive]
    aspect_ratio: NotRequired[builtins.str]

class _ExternalCoreFormatRendersItemVariant2Dimensions(TypedDict, total=False):
    width: NotRequired[builtins.float]
    height: NotRequired[builtins.float]
    min_width: NotRequired[builtins.float]
    min_height: NotRequired[builtins.float]
    max_width: NotRequired[builtins.float]
    max_height: NotRequired[builtins.float]
    unit: NotRequired[Literal['px', 'dp', 'inches', 'cm', 'mm', 'pt']]
    responsive: NotRequired[_ExternalCoreFormatRendersItemVariant2DimensionsResponsive]
    aspect_ratio: NotRequired[builtins.str]

class _ExternalCoreOverlay(TypedDict, total=False):
    id: Required[builtins.str]
    description: NotRequired[builtins.str]
    visual: NotRequired[_ExternalCoreOverlayVisual]
    bounds: Required[_ExternalCoreOverlayBounds]

class _ExternalCoreRequirementsImageAssetRequirements(TypedDict, total=False):
    min_width: NotRequired[builtins.float]
    max_width: NotRequired[builtins.float]
    min_height: NotRequired[builtins.float]
    max_height: NotRequired[builtins.float]
    unit: NotRequired[Literal['px', 'dp', 'inches', 'cm', 'mm', 'pt']]
    aspect_ratio: NotRequired[builtins.str]
    formats: NotRequired[builtins.list[Literal['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'avif', 'tiff', 'pdf', 'eps']]]
    min_dpi: NotRequired[builtins.int]
    bleed: NotRequired[_ExternalCoreRequirementsImageAssetRequirementsBleedVariant1 | _ExternalCoreRequirementsImageAssetRequirementsBleedVariant2]
    color_space: NotRequired[Literal['rgb', 'cmyk', 'grayscale']]
    max_file_size_kb: NotRequired[builtins.int]
    transparency_required: NotRequired[builtins.bool]
    animation_allowed: NotRequired[builtins.bool]
    max_animation_duration_ms: NotRequired[builtins.int]
    max_weight_grams: NotRequired[builtins.int]

class _ExternalCoreRequirementsVideoAssetRequirements(TypedDict, total=False):
    min_width: NotRequired[builtins.int]
    max_width: NotRequired[builtins.int]
    min_height: NotRequired[builtins.int]
    max_height: NotRequired[builtins.int]
    aspect_ratio: NotRequired[builtins.str]
    min_duration_ms: NotRequired[builtins.int]
    max_duration_ms: NotRequired[builtins.int]
    containers: NotRequired[builtins.list[Literal['mp4', 'webm', 'mov', 'avi', 'mkv']]]
    codecs: NotRequired[builtins.list[Literal['h264', 'h265', 'vp8', 'vp9', 'av1', 'prores']]]
    max_file_size_kb: NotRequired[builtins.int]
    min_bitrate_kbps: NotRequired[builtins.int]
    max_bitrate_kbps: NotRequired[builtins.int]
    frame_rates: NotRequired[builtins.list[builtins.float]]
    audio_required: NotRequired[builtins.bool]
    frame_rate_type: NotRequired[Literal['constant', 'variable']]
    scan_type: NotRequired[Literal['progressive', 'interlaced']]
    gop_type: NotRequired[Literal['closed', 'open']]
    min_gop_interval_seconds: NotRequired[builtins.float]
    max_gop_interval_seconds: NotRequired[builtins.float]
    moov_atom_position: NotRequired[Literal['start', 'end']]
    audio_codecs: NotRequired[builtins.list[Literal['aac', 'pcm', 'ac3', 'eac3', 'mp3', 'opus', 'vorbis', 'flac']]]
    audio_sample_rates: NotRequired[builtins.list[builtins.int]]
    audio_channels: NotRequired[builtins.list[Literal['mono', 'stereo', '5.1', '7.1']]]
    loudness_lufs: NotRequired[builtins.float]
    loudness_tolerance_db: NotRequired[builtins.float]
    true_peak_dbfs: NotRequired[builtins.float]

class _ExternalCoreRequirementsAudioAssetRequirements(TypedDict, total=False):
    min_duration_ms: NotRequired[builtins.int]
    max_duration_ms: NotRequired[builtins.int]
    formats: NotRequired[builtins.list[Literal['mp3', 'aac', 'wav', 'ogg', 'flac']]]
    max_file_size_kb: NotRequired[builtins.int]
    sample_rates: NotRequired[builtins.list[builtins.int]]
    channels: NotRequired[builtins.list[Literal['mono', 'stereo']]]
    min_bitrate_kbps: NotRequired[builtins.int]
    max_bitrate_kbps: NotRequired[builtins.int]

class _ExternalCoreRequirementsTextAssetRequirements(TypedDict, total=False):
    min_length: NotRequired[builtins.int]
    max_length: NotRequired[builtins.int]
    min_lines: NotRequired[builtins.int]
    max_lines: NotRequired[builtins.int]
    character_pattern: NotRequired[builtins.str]
    prohibited_terms: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreRequirementsMarkdownAssetRequirements(TypedDict, total=False):
    max_length: NotRequired[builtins.int]

class _ExternalCoreRequirementsHtmlAssetRequirements(TypedDict, total=False):
    max_file_size_kb: NotRequired[builtins.int]
    sandbox: NotRequired[Literal['none', 'iframe', 'safeframe', 'fencedframe']]
    external_resources_allowed: NotRequired[builtins.bool]
    allowed_external_domains: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreRequirementsCssAssetRequirements(TypedDict, total=False):
    max_file_size_kb: NotRequired[builtins.int]

class _ExternalCoreRequirementsJavascriptAssetRequirements(TypedDict, total=False):
    max_file_size_kb: NotRequired[builtins.int]
    module_type: NotRequired[Literal['script', 'module', 'iife']]
    strict_mode_required: NotRequired[builtins.bool]
    external_resources_allowed: NotRequired[builtins.bool]
    allowed_external_domains: NotRequired[builtins.list[builtins.str]]

class _ExternalCoreRequirementsVastAssetRequirements(TypedDict, total=False):
    vast_version: NotRequired[Literal['2.0', '3.0', '4.0', '4.1', '4.2']]

class _ExternalCoreRequirementsDaastAssetRequirements(TypedDict, total=False):
    daast_version: NotRequired[Literal['1.0']]

class _ExternalCoreRequirementsUrlAssetRequirements(TypedDict, total=False):
    role: NotRequired[Literal['clickthrough', 'landing_page', 'impression_tracker', 'click_tracker', 'viewability_tracker', 'third_party_tracker']]
    protocols: NotRequired[builtins.list[Literal['https', 'http']]]
    allowed_domains: NotRequired[builtins.list[builtins.str]]
    max_length: NotRequired[builtins.int]
    macro_support: NotRequired[builtins.bool]

class _ExternalCoreRequirementsWebhookAssetRequirements(TypedDict, total=False):
    methods: NotRequired[builtins.list[Literal['GET', 'POST']]]

class _ExternalCoreRequirementsCatalogRequirements(TypedDict, total=False):
    catalog_type: Required[Literal['offering', 'product', 'inventory', 'store', 'promotion', 'hotel', 'flight', 'job', 'vehicle', 'real_estate', 'education', 'destination', 'app']]
    required: NotRequired[builtins.bool]
    min_items: NotRequired[builtins.int]
    max_items: NotRequired[builtins.int]
    required_fields: NotRequired[builtins.list[builtins.str]]
    feed_formats: NotRequired[builtins.list[Literal['google_merchant_center', 'facebook_catalog', 'shopify', 'linkedin_jobs', 'custom']]]
    offering_asset_constraints: NotRequired[builtins.list[_ExternalCoreRequirementsOfferingAssetConstraint]]
    field_bindings: NotRequired[builtins.list[_ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant1 | _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant2 | _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3]]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant1(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['image']]
    requirements: NotRequired[_ExternalCoreRequirementsImageAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant2(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['video']]
    requirements: NotRequired[_ExternalCoreRequirementsVideoAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant3(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['audio']]
    requirements: NotRequired[_ExternalCoreRequirementsAudioAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant4(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['text']]
    requirements: NotRequired[_ExternalCoreRequirementsTextAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant5(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['markdown']]
    requirements: NotRequired[_ExternalCoreRequirementsMarkdownAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant6(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['html']]
    requirements: NotRequired[_ExternalCoreRequirementsHtmlAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant7(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['css']]
    requirements: NotRequired[_ExternalCoreRequirementsCssAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant8(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['javascript']]
    requirements: NotRequired[_ExternalCoreRequirementsJavascriptAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant9(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['vast']]
    requirements: NotRequired[_ExternalCoreRequirementsVastAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant10(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['daast']]
    requirements: NotRequired[_ExternalCoreRequirementsDaastAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant11(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['url']]
    requirements: NotRequired[_ExternalCoreRequirementsUrlAssetRequirements]

class _ExternalCoreFormatAssetsItemVariant15AssetsItemVariant12(TypedDict, total=False):
    asset_id: Required[builtins.str]
    asset_role: NotRequired[builtins.str]
    required: Required[builtins.bool]
    overlays: NotRequired[builtins.list[_ExternalCoreOverlay]]
    asset_type: Required[Literal['webhook']]
    requirements: NotRequired[_ExternalCoreRequirementsWebhookAssetRequirements]

class _ExternalCoreFormatPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

class _ListCreativesResponseCreativesItemAssignmentsAssignedPackagesItem(TypedDict, total=False):
    package_id: Required[builtins.str]
    assigned_date: Required[builtins.str]

class _ListCreativesResponseCreativesItemPricingOptionsItemVariant5Metadata(TypedDict, total=False):
    summary_for_operator: NotRequired[builtins.str]

class _ExternalCoreUserMatchUidsItem(TypedDict, total=False):
    type: Required[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]
    value: Required[builtins.str]

class _ExternalCoreEventCustomDataContentsItem(TypedDict, total=False):
    id: Required[builtins.str]
    quantity: NotRequired[builtins.int]
    price: NotRequired[builtins.float]
    brand: NotRequired[builtins.str]

class _PackageRequestOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _PackageRequestOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _PackageRequestOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCoreTargetingGeoProximityItemVariant1TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

class _ExternalCoreTargetingGeoProximityItemVariant1Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

class _ExternalCoreTargetingGeoProximityItemVariant1Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

class _ExternalCoreTargetingGeoProximityItemVariant2TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

class _ExternalCoreTargetingGeoProximityItemVariant2Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

class _ExternalCoreTargetingGeoProximityItemVariant2Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

class _ExternalCoreTargetingGeoProximityItemVariant3TravelTime(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['min', 'hr']]

class _ExternalCoreTargetingGeoProximityItemVariant3Radius(TypedDict, total=False):
    value: Required[builtins.float]
    unit: Required[Literal['km', 'mi', 'm']]

class _ExternalCoreTargetingGeoProximityItemVariant3Geometry(TypedDict, total=False):
    type: Required[Literal['Polygon', 'MultiPolygon']]
    coordinates: Required[builtins.list[Any]]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponsePreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1 | _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2 | _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3]]
    input: Required[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemInput]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItem(TypedDict, total=False):
    preview_id: Required[builtins.str]
    renders: Required[builtins.list[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1 | _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2 | _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3]]
    input: Required[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemInput]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponsePreviewsItem2RendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _ExternalSponsoredIntelligenceSiIdentityUserShippingAddress(TypedDict, total=False):
    street: NotRequired[builtins.str]
    city: NotRequired[builtins.str]
    state: NotRequired[builtins.str]
    postal_code: NotRequired[builtins.str]
    country: NotRequired[builtins.str]

class _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVoiceVariant2(TypedDict, total=False):
    provider: NotRequired[builtins.str]
    voice_id: NotRequired[builtins.str]

class _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesVideoVariant2(TypedDict, total=False):
    formats: NotRequired[builtins.list[builtins.str]]
    max_duration_seconds: NotRequired[builtins.int]

class _ExternalSponsoredIntelligenceSiCapabilitiesModalitiesAvatarVariant2(TypedDict, total=False):
    provider: NotRequired[builtins.str]
    avatar_id: NotRequired[builtins.str]

class _ExternalA2uiComponent(TypedDict, total=False):
    id: Required[builtins.str]
    parentId: NotRequired[builtins.str]
    component: Required[builtins.dict[builtins.str, builtins.dict[builtins.str, Any]]]

class _SiSendMessageResponseHandoffIntentPrice(TypedDict, total=False):
    amount: NotRequired[builtins.float]
    currency: NotRequired[builtins.str]

class _ExternalCoreAudienceMemberUidsItem(TypedDict, total=False):
    type: Required[Literal['rampid', 'rampid_derived', 'id5', 'uid2', 'euid', 'pairid', 'maid', 'hashed_email', 'publisher_first_party', 'other']]
    value: Required[builtins.str]

class _ExternalCoreEventSourceHealthDetail(TypedDict, total=False):
    score: Required[builtins.float]
    max_score: Required[builtins.float]
    label: NotRequired[builtins.str]

class _SyncGovernanceRequestAccountsItemGovernanceAgentsItemAuthentication(TypedDict, total=False):
    schemes: Required[builtins.list[Literal['Bearer', 'HMAC-SHA256']]]
    credentials: Required[builtins.str]

class _SyncPlansRequestPlansItemBudgetVariant1AllocationsValue(TypedDict, total=False):
    amount: NotRequired[builtins.float]
    max_pct: NotRequired[builtins.float]

class _SyncPlansRequestPlansItemBudgetVariant2AllocationsValue(TypedDict, total=False):
    amount: NotRequired[builtins.float]
    max_pct: NotRequired[builtins.float]

class _SyncPlansRequestPlansItemChannelsMixTargetsValue(TypedDict, total=False):
    min_pct: NotRequired[builtins.float]
    max_pct: NotRequired[builtins.float]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant1(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant2(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant3(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant4(TypedDict, total=False):
    type: Required[Literal['description']]
    description: Required[builtins.str]
    category: NotRequired[builtins.str]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant1(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant2]
    value_type: Required[Literal['binary']]
    value: Required[builtins.bool]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant2(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant2]
    value_type: Required[Literal['categorical']]
    values: Required[builtins.list[builtins.str]]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant3(TypedDict, total=False):
    type: Required[Literal['signal']]
    signal_id: Required[_ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant1 | _ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant2]
    value_type: Required[Literal['numeric']]
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant4(TypedDict, total=False):
    type: Required[Literal['description']]
    description: Required[builtins.str]
    category: NotRequired[builtins.str]

class _SyncPlansRequestPlansItemDelegationsItemBudgetLimit(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

class _SyncPlansRequestPlansItemPortfolioTotalBudgetCap(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant1(TypedDict, total=False):
    type: Required[Literal['text']]
    role: NotRequired[Literal['title', 'paragraph', 'heading', 'caption', 'quote', 'list_item', 'description']]
    content: Required[builtins.str]
    content_format: NotRequired[Literal['text/plain', 'text/markdown', 'text/html', 'application/json']]
    language: NotRequired[builtins.str]
    heading_level: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2(TypedDict, total=False):
    type: Required[Literal['image']]
    url: Required[builtins.str]
    access: NotRequired[_UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3]
    alt_text: NotRequired[builtins.str]
    caption: NotRequired[builtins.str]
    width: NotRequired[builtins.int]
    height: NotRequired[builtins.int]
    provenance: NotRequired[_ExternalCoreProvenance]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3(TypedDict, total=False):
    type: Required[Literal['video']]
    url: Required[builtins.str]
    access: NotRequired[_UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'subtitles', 'closed_captions', 'dub', 'generated']]
    thumbnail_url: NotRequired[builtins.str]
    provenance: NotRequired[_ExternalCoreProvenance]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4(TypedDict, total=False):
    type: Required[Literal['audio']]
    url: Required[builtins.str]
    access: NotRequired[_UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2 | _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3]
    duration_ms: NotRequired[builtins.int]
    transcript: NotRequired[builtins.str]
    transcript_format: NotRequired[Literal['text/plain', 'text/markdown', 'application/json']]
    transcript_source: NotRequired[Literal['original_script', 'closed_captions', 'generated']]
    provenance: NotRequired[_ExternalCoreProvenance]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2Metadata(TypedDict, total=False):
    canonical: NotRequired[builtins.str]
    author: NotRequired[builtins.str]
    keywords: NotRequired[builtins.str]
    open_graph: NotRequired[builtins.dict[builtins.str, Any]]
    twitter_card: NotRequired[builtins.dict[builtins.str, Any]]
    json_ld: NotRequired[builtins.list[builtins.dict[builtins.str, Any]]]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2Identifiers(TypedDict, total=False):
    apple_podcast_id: NotRequired[builtins.str]
    spotify_collection_id: NotRequired[builtins.str]
    podcast_guid: NotRequired[builtins.str]
    youtube_video_id: NotRequired[builtins.str]
    rss_url: NotRequired[builtins.str]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequency(TypedDict, total=False):
    min: NotRequired[builtins.int]
    max: NotRequired[builtins.int]
    window: Required[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequencyWindow]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['threshold_rate']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2EventSourcesItem(TypedDict, total=False):
    event_source_id: Required[builtins.str]
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    custom_event_name: NotRequired[builtins.str]
    value_field: NotRequired[builtins.str]
    value_factor: NotRequired[builtins.float]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant1(TypedDict, total=False):
    kind: Required[Literal['cost_per']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant2(TypedDict, total=False):
    kind: Required[Literal['per_ad_spend']]
    value: Required[builtins.float]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2TargetVariant3(TypedDict, total=False):
    kind: Required[Literal['maximize_value']]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindow(TypedDict, total=False):
    post_click: Required[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostClick]
    post_view: NotRequired[_ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostView]

class _ExternalPropertyValidationResultFeaturesItemRequirement(TypedDict, total=False):
    min_value: NotRequired[builtins.float]
    max_value: NotRequired[builtins.float]
    allowed_values: NotRequired[builtins.list[Any]]

class _ExternalPropertyAuthorizationResultViolation(TypedDict, total=False):
    code: Required[builtins.str]
    message: Required[builtins.str]

class _ExternalCoreProvenanceDisclosureJurisdictionsItem(TypedDict, total=False):
    country: Required[builtins.str]
    region: NotRequired[builtins.str]
    regulation: Required[builtins.str]
    label_text: NotRequired[builtins.str]
    render_guidance: NotRequired[_ExternalCoreProvenanceDisclosureJurisdictionsItemRenderGuidance]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _BuildCreativeResponsePreviewPreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _BuildCreativeResponsePreview2PreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _CreateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalMediaBuyPackageRequestOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCorePackageOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCorePackageOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingGeoMetros(TypedDict, total=False):
    nielsen_dma: NotRequired[builtins.bool]
    uk_itl1: NotRequired[builtins.bool]
    uk_itl2: NotRequired[builtins.bool]
    eurostat_nuts2: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingGeoPostalAreas(TypedDict, total=False):
    us_zip: NotRequired[builtins.bool]
    us_zip_plus_four: NotRequired[builtins.bool]
    gb_outward: NotRequired[builtins.bool]
    gb_full: NotRequired[builtins.bool]
    ca_fsa: NotRequired[builtins.bool]
    ca_full: NotRequired[builtins.bool]
    de_plz: NotRequired[builtins.bool]
    fr_code_postal: NotRequired[builtins.bool]
    au_postcode: NotRequired[builtins.bool]
    ch_plz: NotRequired[builtins.bool]
    at_plz: NotRequired[builtins.bool]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingAgeRestriction(TypedDict, total=False):
    supported: NotRequired[builtins.bool]
    verification_methods: NotRequired[builtins.list[Literal['facial_age_estimation', 'id_document', 'digital_id', 'credit_card', 'world_id']]]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingKeywordTargets(TypedDict, total=False):
    supported_match_types: Required[builtins.list[Literal['broad', 'phrase', 'exact']]]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingNegativeKeywords(TypedDict, total=False):
    supported_match_types: Required[builtins.list[Literal['broad', 'phrase', 'exact']]]

class _GetAdcpCapabilitiesResponseMediaBuyExecutionTargetingGeoProximity(TypedDict, total=False):
    radius: NotRequired[builtins.bool]
    travel_time: NotRequired[builtins.bool]
    geometry: NotRequired[builtins.bool]
    transport_modes: NotRequired[builtins.list[Literal['walking', 'cycling', 'driving', 'public_transport']]]

class _ExternalCoreDeliveryMetricsDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _ExternalCoreCreativeVariantDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _ExternalCoreCreativeVariantGenerationContextArtifact(TypedDict, total=False):
    property_id: Required[_ExternalCoreIdentifier]
    artifact_id: Required[builtins.str]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemTotalsDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemByEventTypeItem(TypedDict, total=False):
    event_type: Required[Literal['page_view', 'view_content', 'select_content', 'select_item', 'search', 'share', 'add_to_cart', 'remove_from_cart', 'viewed_cart', 'add_to_wishlist', 'initiate_checkout', 'add_payment_info', 'purchase', 'refund', 'lead', 'qualify_lead', 'close_convert_lead', 'disqualify_lead', 'complete_registration', 'subscribe', 'start_trial', 'app_install', 'app_launch', 'contact', 'schedule', 'donate', 'submit_application', 'custom']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemQuartileData(TypedDict, total=False):
    q1_views: NotRequired[builtins.float]
    q2_views: NotRequired[builtins.float]
    q3_views: NotRequired[builtins.float]
    q4_views: NotRequired[builtins.float]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetrics(TypedDict, total=False):
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]
    screen_time_seconds: NotRequired[builtins.int]
    sov_achieved: NotRequired[builtins.float]
    calculation_notes: NotRequired[builtins.str]
    venue_breakdown: NotRequired[builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetricsVenueBreakdownItem]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemViewability(TypedDict, total=False):
    measurable_impressions: NotRequired[builtins.float]
    viewable_impressions: NotRequired[builtins.float]
    viewable_rate: NotRequired[builtins.float]
    standard: NotRequired[Literal['mrc', 'groupm']]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemByActionSourceItem(TypedDict, total=False):
    action_source: Required[Literal['website', 'app', 'offline', 'phone_call', 'chat', 'email', 'in_store', 'system_generated', 'other']]
    event_source_id: NotRequired[builtins.str]
    count: Required[builtins.float]
    value: NotRequired[builtins.float]

class _GetPlanAuditLogsResponsePlansItemSummaryDriftMetricsThresholds(TypedDict, total=False):
    escalation_rate_max: NotRequired[builtins.float]
    escalation_rate_min: NotRequired[builtins.float]
    auto_approval_rate_max: NotRequired[builtins.float]
    human_override_rate_max: NotRequired[builtins.float]

class _ExternalCoreProductPricingOptionsItemVariant5ParametersViewThresholdVariant2(TypedDict, total=False):
    duration_seconds: Required[builtins.int]

class _ExternalCoreForecastPointMetrics(TypedDict, total=False):
    audience_size: NotRequired[_ExternalCoreForecastRange]
    reach: NotRequired[_ExternalCoreForecastRange]
    frequency: NotRequired[_ExternalCoreForecastRange]
    impressions: NotRequired[_ExternalCoreForecastRange]
    clicks: NotRequired[_ExternalCoreForecastRange]
    spend: NotRequired[_ExternalCoreForecastRange]
    views: NotRequired[_ExternalCoreForecastRange]
    completed_views: NotRequired[_ExternalCoreForecastRange]
    grps: NotRequired[_ExternalCoreForecastRange]
    engagements: NotRequired[_ExternalCoreForecastRange]
    follows: NotRequired[_ExternalCoreForecastRange]
    saves: NotRequired[_ExternalCoreForecastRange]
    profile_visits: NotRequired[_ExternalCoreForecastRange]
    measured_impressions: NotRequired[_ExternalCoreForecastRange]
    downloads: NotRequired[_ExternalCoreForecastRange]
    plays: NotRequired[_ExternalCoreForecastRange]

class _ExternalCoreMaterialDeadline(TypedDict, total=False):
    stage: Required[builtins.str]
    due_at: Required[builtins.str]
    label: NotRequired[builtins.str]

class _ExternalCoreInsertionOrderTermsTotalBudget(TypedDict, total=False):
    amount: Required[builtins.float]
    currency: Required[builtins.str]

class _ExternalCoreFormatRendersItemVariant1DimensionsResponsive(TypedDict, total=False):
    width: Required[builtins.bool]
    height: Required[builtins.bool]

class _ExternalCoreFormatRendersItemVariant2DimensionsResponsive(TypedDict, total=False):
    width: Required[builtins.bool]
    height: Required[builtins.bool]

class _ExternalCoreOverlayVisual(TypedDict, total=False):
    url: NotRequired[builtins.str]
    light: NotRequired[builtins.str]
    dark: NotRequired[builtins.str]

class _ExternalCoreOverlayBounds(TypedDict, total=False):
    x: Required[builtins.float]
    y: Required[builtins.float]
    width: Required[builtins.float]
    height: Required[builtins.float]
    unit: Required[Literal['px', 'fraction', 'inches', 'cm', 'mm', 'pt']]

class _ExternalCoreRequirementsImageAssetRequirementsBleedVariant1(TypedDict, total=False):
    uniform: Required[builtins.float]

class _ExternalCoreRequirementsImageAssetRequirementsBleedVariant2(TypedDict, total=False):
    top: Required[builtins.float]
    right: Required[builtins.float]
    bottom: Required[builtins.float]
    left: Required[builtins.float]

class _ExternalCoreRequirementsOfferingAssetConstraint(TypedDict, total=False):
    asset_group_id: Required[builtins.str]
    asset_type: Required[Literal['image', 'video', 'audio', 'text', 'markdown', 'html', 'css', 'javascript', 'vast', 'daast', 'url', 'webhook', 'brief', 'catalog']]
    required: NotRequired[builtins.bool]
    min_count: NotRequired[builtins.int]
    max_count: NotRequired[builtins.int]
    asset_requirements: NotRequired[builtins.dict[builtins.str, Any]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['scalar']]
    asset_id: Required[builtins.str]
    catalog_field: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['asset_pool']]
    asset_id: Required[builtins.str]
    asset_group_id: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3(TypedDict, total=False):
    kind: Required[Literal['catalog_group']]
    format_group_id: Required[builtins.str]
    catalog_item: Required[Literal[True]]
    per_item_bindings: NotRequired[builtins.list[_ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant1 | _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant2]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Embedding]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Embedding]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Embedding]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['url']]
    preview_url: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Embedding]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['html']]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Embedding]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3(TypedDict, total=False):
    render_id: Required[builtins.str]
    output_format: Required[Literal['both']]
    preview_url: Required[builtins.str]
    preview_html: Required[builtins.str]
    role: Required[builtins.str]
    dimensions: NotRequired[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Dimensions]
    embedding: NotRequired[_PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Embedding]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemInput(TypedDict, total=False):
    name: Required[builtins.str]
    macros: NotRequired[builtins.dict[builtins.str, builtins.str]]
    context_description: NotRequired[builtins.str]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsIncludeItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant1SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant2SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant1(TypedDict, total=False):
    source: Required[Literal['catalog']]
    data_provider_domain: Required[builtins.str]
    id: Required[builtins.str]

class _ExternalGovernanceAudienceConstraintsExcludeItemVariant3SignalIdVariant2(TypedDict, total=False):
    source: Required[Literal['agent']]
    agent_url: Required[builtins.str]
    id: Required[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant2AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant3AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant1(TypedDict, total=False):
    method: Required[Literal['bearer_token']]
    token: Required[builtins.str]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant2(TypedDict, total=False):
    method: Required[Literal['service_account']]
    provider: Required[Literal['gcp', 'aws']]
    credentials: NotRequired[builtins.dict[builtins.str, Any]]

class _UpdateContentStandardsRequestCalibrationExemplarsFailItemVariant2AssetsItemVariant4AccessVariant3(TypedDict, total=False):
    method: Required[Literal['signed_url']]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant1TargetFrequencyWindow(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostClick(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalMediaBuyPackageUpdateOptimizationGoalsItemVariant2AttributionWindowPostView(TypedDict, total=False):
    interval: Required[builtins.int]
    unit: Required[Literal['seconds', 'minutes', 'hours', 'days', 'campaign']]

class _ExternalCoreProvenanceDisclosureJurisdictionsItemRenderGuidance(TypedDict, total=False):
    persistence: NotRequired[Literal['continuous', 'initial', 'flexible']]
    min_duration_ms: NotRequired[builtins.int]
    positions: NotRequired[builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']]]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCatalogItemItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByCreativeItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByKeywordItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByGeoItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDeviceTypeItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByDevicePlatformItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByAudienceItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _GetMediaBuyDeliveryResponseMediaBuyDeliveriesItemByPackageItemByPlacementItemDoohMetricsVenueBreakdownItem(TypedDict, total=False):
    venue_id: Required[builtins.str]
    venue_name: NotRequired[builtins.str]
    venue_type: NotRequired[builtins.str]
    impressions: Required[builtins.int]
    loop_plays: NotRequired[builtins.int]
    screens_used: NotRequired[builtins.int]

class _ExternalCoreForecastRange(TypedDict, total=False):
    low: NotRequired[builtins.float]
    mid: NotRequired[builtins.float]
    high: NotRequired[builtins.float]

class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant1(TypedDict, total=False):
    kind: Required[Literal['scalar']]
    asset_id: Required[builtins.str]
    catalog_field: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _ExternalCoreRequirementsCatalogRequirementsFieldBindingsItemVariant3PerItemBindingsItemVariant2(TypedDict, total=False):
    kind: Required[Literal['asset_pool']]
    asset_id: Required[builtins.str]
    asset_group_id: Required[builtins.str]
    ext: NotRequired[builtins.dict[builtins.str, Any]]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponseResultsItemVariant1ResponsePreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant1Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant2Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Dimensions(TypedDict, total=False):
    width: Required[builtins.float]
    height: Required[builtins.float]

class _PreviewCreativeResponseResultsItemVariant2ResponsePreviewsItemRendersItemVariant3Embedding(TypedDict, total=False):
    recommended_sandbox: NotRequired[builtins.str]
    requires_https: NotRequired[builtins.bool]
    supports_fullscreen: NotRequired[builtins.bool]
    csp_policy: NotRequired[builtins.str]

class AcquireRightsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    rights_id: builtins.str
    pricing_option_id: builtins.str
    buyer: _ExternalCoreBrandRef
    campaign: _AcquireRightsRequestCampaign
    revocation_webhook: _ExternalCorePushNotificationConfig
    push_notification_config: _ExternalCorePushNotificationConfig | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights_id: builtins.str,
        pricing_option_id: builtins.str,
        buyer: _ExternalCoreBrandRef,
        campaign: _AcquireRightsRequestCampaign,
        revocation_webhook: _ExternalCorePushNotificationConfig,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        push_notification_config: _ExternalCorePushNotificationConfig = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class AcquireRightsResponse(VersionedSchemaModel):
    rights_id: builtins.str | None
    status: Literal['acquired'] | Literal['pending_approval'] | Literal['rejected'] | None
    brand_id: builtins.str | None
    terms: _ExternalBrandRightsTerms | None
    generation_credentials: builtins.list[_ExternalCoreGenerationCredential] | None
    restrictions: builtins.list[builtins.str] | None
    disclosure: _AcquireRightsResponseDisclosure | None
    approval_webhook: _ExternalCorePushNotificationConfig | None
    usage_reporting_url: builtins.str | None
    rights_constraint: _ExternalCoreRightsConstraint | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    detail: builtins.str | None
    estimated_response_time: builtins.str | None
    reason: builtins.str | None
    suggestions: builtins.list[builtins.str] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights_id: builtins.str,
        status: Literal['acquired'],
        brand_id: builtins.str,
        terms: _ExternalBrandRightsTerms,
        generation_credentials: builtins.list[_ExternalCoreGenerationCredential],
        rights_constraint: _ExternalCoreRightsConstraint,
        restrictions: builtins.list[builtins.str] = ...,
        disclosure: _AcquireRightsResponseDisclosure = ...,
        approval_webhook: _ExternalCorePushNotificationConfig = ...,
        usage_reporting_url: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights_id: builtins.str,
        status: Literal['pending_approval'],
        brand_id: builtins.str,
        detail: builtins.str = ...,
        estimated_response_time: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights_id: builtins.str,
        status: Literal['rejected'],
        brand_id: builtins.str,
        reason: builtins.str,
        suggestions: builtins.list[builtins.str] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ActivateSignalRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    action: Literal['activate', 'deactivate']
    signal_agent_segment_id: builtins.str
    destinations: builtins.list[_ActivateSignalRequestDestinationsItemVariant1 | _ActivateSignalRequestDestinationsItemVariant2]
    pricing_option_id: builtins.str | None
    account: _ActivateSignalRequestAccountVariant1 | _ActivateSignalRequestAccountVariant2 | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        signal_agent_segment_id: builtins.str,
        destinations: builtins.list[_ActivateSignalRequestDestinationsItemVariant1 | _ActivateSignalRequestDestinationsItemVariant2],
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        action: Literal['activate', 'deactivate'] = ...,
        pricing_option_id: builtins.str = ...,
        account: _ActivateSignalRequestAccountVariant1 | _ActivateSignalRequestAccountVariant2 = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ActivateSignalResponse(VersionedSchemaModel):
    deployments: builtins.list[_ActivateSignalResponseDeploymentsItemVariant1 | _ActivateSignalResponseDeploymentsItemVariant2] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        deployments: builtins.list[_ActivateSignalResponseDeploymentsItemVariant1 | _ActivateSignalResponseDeploymentsItemVariant2],
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class BuildCreativeInputRequiredResponse(VersionedSchemaModel):
    reason: Literal['APPROVAL_REQUIRED', 'CREATIVE_DIRECTION_NEEDED', 'ASSET_SELECTION_NEEDED'] | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        reason: Literal['APPROVAL_REQUIRED', 'CREATIVE_DIRECTION_NEEDED', 'ASSET_SELECTION_NEEDED'] = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class BuildCreativeRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    message: builtins.str | None
    creative_manifest: _ExternalCoreCreativeManifest | None
    creative_id: builtins.str | None
    concept_id: builtins.str | None
    media_buy_id: builtins.str | None
    package_id: builtins.str | None
    target_format_id: _ExternalCoreFormatId | None
    target_format_ids: builtins.list[_ExternalCoreFormatId] | None
    account: _BuildCreativeRequestAccountVariant1 | _BuildCreativeRequestAccountVariant2 | None
    brand: _ExternalCoreBrandRef | None
    quality: Literal['draft', 'production'] | None
    item_limit: builtins.int | None
    include_preview: builtins.bool | None
    preview_inputs: builtins.list[_BuildCreativeRequestPreviewInputsItem] | None
    preview_quality: Literal['draft', 'production'] | None
    preview_output_format: Literal['url', 'html']
    macro_values: builtins.dict[builtins.str, builtins.str] | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        message: builtins.str = ...,
        creative_manifest: _ExternalCoreCreativeManifest = ...,
        creative_id: builtins.str = ...,
        concept_id: builtins.str = ...,
        media_buy_id: builtins.str = ...,
        package_id: builtins.str = ...,
        target_format_id: _ExternalCoreFormatId = ...,
        target_format_ids: builtins.list[_ExternalCoreFormatId] = ...,
        account: _BuildCreativeRequestAccountVariant1 | _BuildCreativeRequestAccountVariant2 = ...,
        brand: _ExternalCoreBrandRef = ...,
        quality: Literal['draft', 'production'] = ...,
        item_limit: builtins.int = ...,
        include_preview: builtins.bool = ...,
        preview_inputs: builtins.list[_BuildCreativeRequestPreviewInputsItem] = ...,
        preview_quality: Literal['draft', 'production'] = ...,
        preview_output_format: Literal['url', 'html'] = ...,
        macro_values: builtins.dict[builtins.str, builtins.str] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class BuildCreativeSubmittedResponse(VersionedSchemaModel):
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class BuildCreativeResponse(VersionedSchemaModel):
    creative_manifest: _ExternalCoreCreativeManifest | None
    sandbox: builtins.bool | None
    expires_at: builtins.str | None
    preview: _BuildCreativeResponsePreview | _BuildCreativeResponsePreview2 | None
    preview_error: _ExternalCoreError | None
    pricing_option_id: builtins.str | None
    vendor_cost: builtins.float | None
    currency: builtins.str | None
    consumption: _ExternalCoreCreativeConsumption | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    creative_manifests: builtins.list[_ExternalCoreCreativeManifest] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        creative_manifest: _ExternalCoreCreativeManifest,
        sandbox: builtins.bool = ...,
        expires_at: builtins.str = ...,
        preview: _BuildCreativeResponsePreview = ...,
        preview_error: _ExternalCoreError = ...,
        pricing_option_id: builtins.str = ...,
        vendor_cost: builtins.float = ...,
        currency: builtins.str = ...,
        consumption: _ExternalCoreCreativeConsumption = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        creative_manifests: builtins.list[_ExternalCoreCreativeManifest],
        sandbox: builtins.bool = ...,
        expires_at: builtins.str = ...,
        preview: _BuildCreativeResponsePreview2 = ...,
        preview_error: _ExternalCoreError = ...,
        pricing_option_id: builtins.str = ...,
        vendor_cost: builtins.float = ...,
        currency: builtins.str = ...,
        consumption: _ExternalCoreCreativeConsumption = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class BuildCreativeWorkingResponse(VersionedSchemaModel):
    percentage: builtins.float | None
    current_step: builtins.str | None
    total_steps: builtins.int | None
    step_number: builtins.int | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        percentage: builtins.float = ...,
        current_step: builtins.str = ...,
        total_steps: builtins.int = ...,
        step_number: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CalibrateContentRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    standards_id: builtins.str
    artifact: _ExternalContentStandardsArtifact
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        standards_id: builtins.str,
        artifact: _ExternalContentStandardsArtifact,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CalibrateContentResponse(VersionedSchemaModel):
    verdict: Literal['pass', 'fail'] | None
    confidence: builtins.float | None
    explanation: builtins.str | None
    features: builtins.list[_CalibrateContentResponseFeaturesItem] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        verdict: Literal['pass', 'fail'],
        confidence: builtins.float = ...,
        explanation: builtins.str = ...,
        features: builtins.list[_CalibrateContentResponseFeaturesItem] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CheckGovernanceRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    plan_id: builtins.str
    caller: builtins.str
    purchase_type: Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']
    tool: builtins.str | None
    payload: builtins.dict[builtins.str, Any] | None
    governance_context: builtins.str | None
    phase: Literal['purchase', 'modification', 'delivery']
    planned_delivery: _ExternalCorePlannedDelivery | None
    delivery_metrics: _CheckGovernanceRequestDeliveryMetrics | None
    modification_summary: builtins.str | None
    invoice_recipient: _ExternalCoreBusinessEntity | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        plan_id: builtins.str,
        caller: builtins.str,
        adcp_major_version: builtins.int = ...,
        purchase_type: Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services'] = ...,
        tool: builtins.str = ...,
        payload: builtins.dict[builtins.str, Any] = ...,
        governance_context: builtins.str = ...,
        phase: Literal['purchase', 'modification', 'delivery'] = ...,
        planned_delivery: _ExternalCorePlannedDelivery = ...,
        delivery_metrics: _CheckGovernanceRequestDeliveryMetrics = ...,
        modification_summary: builtins.str = ...,
        invoice_recipient: _ExternalCoreBusinessEntity = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CheckGovernanceResponse(VersionedSchemaModel):
    check_id: builtins.str
    status: Literal['approved', 'denied', 'conditions']
    plan_id: builtins.str
    explanation: builtins.str
    findings: builtins.list[_CheckGovernanceResponseFindingsItem] | None
    conditions: builtins.list[_CheckGovernanceResponseConditionsItem] | None
    expires_at: builtins.str | None
    next_check: builtins.str | None
    categories_evaluated: builtins.list[builtins.str] | None
    policies_evaluated: builtins.list[builtins.str] | None
    mode: Literal['audit', 'advisory', 'enforce'] | None
    governance_context: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        check_id: builtins.str,
        status: Literal['approved', 'denied', 'conditions'],
        plan_id: builtins.str,
        explanation: builtins.str,
        findings: builtins.list[_CheckGovernanceResponseFindingsItem] = ...,
        conditions: builtins.list[_CheckGovernanceResponseConditionsItem] = ...,
        expires_at: builtins.str = ...,
        next_check: builtins.str = ...,
        categories_evaluated: builtins.list[builtins.str] = ...,
        policies_evaluated: builtins.list[builtins.str] = ...,
        mode: Literal['audit', 'advisory', 'enforce'] = ...,
        governance_context: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ComplyTestControllerRequest(VersionedSchemaModel):
    scenario: Literal['list_scenarios', 'force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format']
    params: _ComplyTestControllerRequestParams | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        scenario: Literal['list_scenarios', 'force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format'],
        params: _ComplyTestControllerRequestParams = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ComplyTestControllerResponse(VersionedSchemaModel):
    success: Literal[True] | Literal[False]
    scenarios: builtins.list[Literal['force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format']] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    previous_state: builtins.str | None
    current_state: builtins.str | builtins.str | None
    message: builtins.str | None
    simulated: builtins.dict[builtins.str, Any] | None
    cumulative: builtins.dict[builtins.str, Any] | None
    forced: _ComplyTestControllerResponseForced | None
    error: Literal['INVALID_TRANSITION', 'INVALID_STATE', 'NOT_FOUND', 'UNKNOWN_SCENARIO', 'INVALID_PARAMS', 'FORBIDDEN', 'INTERNAL_ERROR'] | None
    error_detail: builtins.str | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[True],
        scenarios: builtins.list[Literal['force_creative_status', 'force_account_status', 'force_media_buy_status', 'force_create_media_buy_arm', 'force_task_completion', 'force_session_status', 'simulate_delivery', 'simulate_budget_spend', 'seed_product', 'seed_pricing_option', 'seed_creative', 'seed_plan', 'seed_media_buy', 'seed_creative_format']],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[True],
        previous_state: builtins.str,
        current_state: builtins.str,
        message: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[True],
        simulated: builtins.dict[builtins.str, Any],
        cumulative: builtins.dict[builtins.str, Any] = ...,
        message: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[True],
        forced: _ComplyTestControllerResponseForced,
        message: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[True],
        message: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[False],
        error: Literal['INVALID_TRANSITION', 'INVALID_STATE', 'NOT_FOUND', 'UNKNOWN_SCENARIO', 'INVALID_PARAMS', 'FORBIDDEN', 'INTERNAL_ERROR'],
        error_detail: builtins.str = ...,
        current_state: builtins.str | None = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ContextMatchRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    type: Literal['context_match_request']
    protocol_version: builtins.str
    request_id: builtins.str
    property_rid: builtins.str
    property_id: builtins.str | None
    property_type: Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'linear_tv', 'streaming_audio', 'ai_assistant']
    placement_id: builtins.str
    seller_agent_url: builtins.str
    artifact: _ExternalContentStandardsArtifact | None
    artifact_refs: builtins.list[_ContextMatchRequestArtifactRefsItem] | None
    geo: _ContextMatchRequestGeo | None
    context_signals: _ContextMatchRequestContextSignals | None
    package_ids: builtins.list[builtins.str] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        type: Literal['context_match_request'],
        request_id: builtins.str,
        property_rid: builtins.str,
        property_type: Literal['website', 'mobile_app', 'ctv_app', 'desktop_app', 'dooh', 'podcast', 'radio', 'linear_tv', 'streaming_audio', 'ai_assistant'],
        placement_id: builtins.str,
        seller_agent_url: builtins.str,
        adcp_major_version: builtins.int = ...,
        protocol_version: builtins.str = ...,
        property_id: builtins.str = ...,
        artifact: _ExternalContentStandardsArtifact = ...,
        artifact_refs: builtins.list[_ContextMatchRequestArtifactRefsItem] = ...,
        geo: _ContextMatchRequestGeo = ...,
        context_signals: _ContextMatchRequestContextSignals = ...,
        package_ids: builtins.list[builtins.str] = ...,
    ) -> None: ...

class ContextMatchResponse(VersionedSchemaModel):
    type: Literal['context_match_response']
    request_id: builtins.str
    offers: builtins.list[_ExternalTmpOffer]
    cache_ttl: builtins.int | None
    signals: _ContextMatchResponseSignals | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        type: Literal['context_match_response'],
        request_id: builtins.str,
        offers: builtins.list[_ExternalTmpOffer],
        cache_ttl: builtins.int = ...,
        signals: _ContextMatchResponseSignals = ...,
    ) -> None: ...

class CreateCollectionListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _CreateCollectionListRequestAccountVariant1 | _CreateCollectionListRequestAccountVariant2 | None
    name: builtins.str
    description: builtins.str | None
    base_collections: builtins.list[_CreateCollectionListRequestBaseCollectionsItemVariant1 | _CreateCollectionListRequestBaseCollectionsItemVariant2 | _CreateCollectionListRequestBaseCollectionsItemVariant3] | None
    filters: _ExternalCollectionCollectionListFilters | None
    brand: _ExternalCoreBrandRef | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        name: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _CreateCollectionListRequestAccountVariant1 | _CreateCollectionListRequestAccountVariant2 = ...,
        description: builtins.str = ...,
        base_collections: builtins.list[_CreateCollectionListRequestBaseCollectionsItemVariant1 | _CreateCollectionListRequestBaseCollectionsItemVariant2 | _CreateCollectionListRequestBaseCollectionsItemVariant3] = ...,
        filters: _ExternalCollectionCollectionListFilters = ...,
        brand: _ExternalCoreBrandRef = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateCollectionListResponse(VersionedSchemaModel):
    list: _ExternalCollectionCollectionList
    auth_token: builtins.str
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list: _ExternalCollectionCollectionList,
        auth_token: builtins.str,
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateContentStandardsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    scope: _CreateContentStandardsRequestScope
    registry_policy_ids: builtins.list[builtins.str] | None
    policies: builtins.list[_ExternalGovernancePolicyEntry] | None
    calibration_exemplars: _CreateContentStandardsRequestCalibrationExemplars | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        scope: _CreateContentStandardsRequestScope,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        registry_policy_ids: builtins.list[builtins.str] = ...,
        policies: builtins.list[_ExternalGovernancePolicyEntry] = ...,
        calibration_exemplars: _CreateContentStandardsRequestCalibrationExemplars = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateContentStandardsResponse(VersionedSchemaModel):
    standards_id: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None
    conflicting_standards_id: builtins.str | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        standards_id: builtins.str,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        conflicting_standards_id: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateMediaBuyInputRequiredResponse(VersionedSchemaModel):
    reason: Literal['APPROVAL_REQUIRED', 'BUDGET_EXCEEDS_LIMIT'] | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        reason: Literal['APPROVAL_REQUIRED', 'BUDGET_EXCEEDS_LIMIT'] = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateMediaBuyRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    plan_id: builtins.str | None
    account: _CreateMediaBuyRequestAccountVariant1 | _CreateMediaBuyRequestAccountVariant2
    proposal_id: builtins.str | None
    total_budget: _CreateMediaBuyRequestTotalBudget | None
    packages: builtins.list[_ExternalMediaBuyPackageRequest] | None
    brand: _ExternalCoreBrandRef
    advertiser_industry: Literal['automotive', 'automotive.electric_vehicles', 'automotive.parts_accessories', 'automotive.luxury', 'beauty_cosmetics', 'beauty_cosmetics.skincare', 'beauty_cosmetics.fragrance', 'beauty_cosmetics.haircare', 'cannabis', 'cpg', 'cpg.personal_care', 'cpg.household', 'dating', 'education', 'education.higher_education', 'education.online_learning', 'education.k12', 'energy_utilities', 'energy_utilities.renewable', 'fashion_apparel', 'fashion_apparel.luxury', 'fashion_apparel.sportswear', 'finance', 'finance.banking', 'finance.insurance', 'finance.investment', 'finance.cryptocurrency', 'food_beverage', 'food_beverage.alcohol', 'food_beverage.restaurants', 'food_beverage.packaged_goods', 'gambling_betting', 'gambling_betting.sports_betting', 'gambling_betting.casino', 'gaming', 'gaming.mobile', 'gaming.console_pc', 'gaming.esports', 'government_nonprofit', 'government_nonprofit.political', 'government_nonprofit.charity', 'healthcare', 'healthcare.pharmaceutical', 'healthcare.medical_devices', 'healthcare.wellness', 'home_garden', 'home_garden.furniture', 'home_garden.home_improvement', 'media_entertainment', 'media_entertainment.podcasts', 'media_entertainment.music', 'media_entertainment.film_tv', 'media_entertainment.publishing', 'media_entertainment.live_events', 'pets', 'professional_services', 'professional_services.legal', 'professional_services.consulting', 'real_estate', 'real_estate.residential', 'real_estate.commercial', 'recruitment_hr', 'retail', 'retail.ecommerce', 'retail.department_stores', 'sports_fitness', 'sports_fitness.equipment', 'sports_fitness.teams_leagues', 'technology', 'technology.software', 'technology.hardware', 'technology.ai_ml', 'telecom', 'telecom.mobile_carriers', 'telecom.internet_providers', 'transportation_logistics', 'travel_hospitality', 'travel_hospitality.airlines', 'travel_hospitality.hotels', 'travel_hospitality.cruise', 'travel_hospitality.tourism'] | None
    invoice_recipient: _ExternalCoreBusinessEntity | None
    io_acceptance: _CreateMediaBuyRequestIoAcceptance | None
    po_number: builtins.str | None
    agency_estimate_number: builtins.str | None
    start_time: Literal['asap'] | builtins.str
    end_time: builtins.str
    push_notification_config: _ExternalCorePushNotificationConfig | None
    reporting_webhook: _ExternalCoreReportingWebhook | None
    artifact_webhook: _CreateMediaBuyRequestArtifactWebhook | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        account: _CreateMediaBuyRequestAccountVariant1 | _CreateMediaBuyRequestAccountVariant2,
        brand: _ExternalCoreBrandRef,
        start_time: Literal['asap'] | builtins.str,
        end_time: builtins.str,
        adcp_major_version: builtins.int = ...,
        plan_id: builtins.str = ...,
        proposal_id: builtins.str = ...,
        total_budget: _CreateMediaBuyRequestTotalBudget = ...,
        packages: builtins.list[_ExternalMediaBuyPackageRequest] = ...,
        advertiser_industry: Literal['automotive', 'automotive.electric_vehicles', 'automotive.parts_accessories', 'automotive.luxury', 'beauty_cosmetics', 'beauty_cosmetics.skincare', 'beauty_cosmetics.fragrance', 'beauty_cosmetics.haircare', 'cannabis', 'cpg', 'cpg.personal_care', 'cpg.household', 'dating', 'education', 'education.higher_education', 'education.online_learning', 'education.k12', 'energy_utilities', 'energy_utilities.renewable', 'fashion_apparel', 'fashion_apparel.luxury', 'fashion_apparel.sportswear', 'finance', 'finance.banking', 'finance.insurance', 'finance.investment', 'finance.cryptocurrency', 'food_beverage', 'food_beverage.alcohol', 'food_beverage.restaurants', 'food_beverage.packaged_goods', 'gambling_betting', 'gambling_betting.sports_betting', 'gambling_betting.casino', 'gaming', 'gaming.mobile', 'gaming.console_pc', 'gaming.esports', 'government_nonprofit', 'government_nonprofit.political', 'government_nonprofit.charity', 'healthcare', 'healthcare.pharmaceutical', 'healthcare.medical_devices', 'healthcare.wellness', 'home_garden', 'home_garden.furniture', 'home_garden.home_improvement', 'media_entertainment', 'media_entertainment.podcasts', 'media_entertainment.music', 'media_entertainment.film_tv', 'media_entertainment.publishing', 'media_entertainment.live_events', 'pets', 'professional_services', 'professional_services.legal', 'professional_services.consulting', 'real_estate', 'real_estate.residential', 'real_estate.commercial', 'recruitment_hr', 'retail', 'retail.ecommerce', 'retail.department_stores', 'sports_fitness', 'sports_fitness.equipment', 'sports_fitness.teams_leagues', 'technology', 'technology.software', 'technology.hardware', 'technology.ai_ml', 'telecom', 'telecom.mobile_carriers', 'telecom.internet_providers', 'transportation_logistics', 'travel_hospitality', 'travel_hospitality.airlines', 'travel_hospitality.hotels', 'travel_hospitality.cruise', 'travel_hospitality.tourism'] = ...,
        invoice_recipient: _ExternalCoreBusinessEntity = ...,
        io_acceptance: _CreateMediaBuyRequestIoAcceptance = ...,
        po_number: builtins.str = ...,
        agency_estimate_number: builtins.str = ...,
        push_notification_config: _ExternalCorePushNotificationConfig = ...,
        reporting_webhook: _ExternalCoreReportingWebhook = ...,
        artifact_webhook: _CreateMediaBuyRequestArtifactWebhook = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateMediaBuySubmittedResponse(VersionedSchemaModel):
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateMediaBuyResponse(VersionedSchemaModel):
    media_buy_id: builtins.str | None
    account: _ExternalCoreAccount | None
    invoice_recipient: _ExternalCoreBusinessEntity | None
    status: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | Literal['submitted'] | None
    confirmed_at: builtins.str | None
    creative_deadline: builtins.str | None
    revision: builtins.int | None
    valid_actions: builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] | None
    packages: builtins.list[_ExternalCorePackage] | None
    planned_delivery: _ExternalCorePlannedDelivery | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None
    task_id: builtins.str | None
    message: builtins.str | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        media_buy_id: builtins.str,
        packages: builtins.list[_ExternalCorePackage],
        account: _ExternalCoreAccount = ...,
        invoice_recipient: _ExternalCoreBusinessEntity = ...,
        status: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] = ...,
        confirmed_at: builtins.str = ...,
        creative_deadline: builtins.str = ...,
        revision: builtins.int = ...,
        valid_actions: builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] = ...,
        planned_delivery: _ExternalCorePlannedDelivery = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        status: Literal['submitted'],
        task_id: builtins.str,
        message: builtins.str = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreateMediaBuyWorkingResponse(VersionedSchemaModel):
    percentage: builtins.float | None
    current_step: builtins.str | None
    total_steps: builtins.int | None
    step_number: builtins.int | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        percentage: builtins.float = ...,
        current_step: builtins.str = ...,
        total_steps: builtins.int = ...,
        step_number: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreatePropertyListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _CreatePropertyListRequestAccountVariant1 | _CreatePropertyListRequestAccountVariant2 | None
    name: builtins.str
    description: builtins.str | None
    base_properties: builtins.list[_CreatePropertyListRequestBasePropertiesItemVariant1 | _CreatePropertyListRequestBasePropertiesItemVariant2 | _CreatePropertyListRequestBasePropertiesItemVariant3] | None
    filters: _ExternalPropertyPropertyListFilters | None
    brand: _ExternalCoreBrandRef | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        name: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _CreatePropertyListRequestAccountVariant1 | _CreatePropertyListRequestAccountVariant2 = ...,
        description: builtins.str = ...,
        base_properties: builtins.list[_CreatePropertyListRequestBasePropertiesItemVariant1 | _CreatePropertyListRequestBasePropertiesItemVariant2 | _CreatePropertyListRequestBasePropertiesItemVariant3] = ...,
        filters: _ExternalPropertyPropertyListFilters = ...,
        brand: _ExternalCoreBrandRef = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreatePropertyListResponse(VersionedSchemaModel):
    list: _ExternalPropertyPropertyList
    auth_token: builtins.str
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list: _ExternalPropertyPropertyList,
        auth_token: builtins.str,
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreativeApprovalRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    rights_id: builtins.str
    creative_id: builtins.str | None
    creative_url: builtins.str
    creative_format: _ExternalCoreFormatId | None
    description: builtins.str | None
    metadata: builtins.dict[builtins.str, Any] | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights_id: builtins.str,
        creative_url: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        creative_id: builtins.str = ...,
        creative_format: _ExternalCoreFormatId = ...,
        description: builtins.str = ...,
        metadata: builtins.dict[builtins.str, Any] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class CreativeApprovalResponse(VersionedSchemaModel):
    status: Literal['approved'] | Literal['rejected'] | Literal['pending_review'] | None
    rights_id: builtins.str | None
    creative_id: builtins.str | None
    creative_url: builtins.str | None
    approved_at: builtins.str | None
    conditions: builtins.list[builtins.str] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    reason: builtins.str | None
    suggestions: builtins.list[builtins.str] | None
    estimated_response_time: builtins.str | None
    status_url: builtins.str | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        status: Literal['approved'],
        rights_id: builtins.str,
        creative_id: builtins.str = ...,
        creative_url: builtins.str = ...,
        approved_at: builtins.str = ...,
        conditions: builtins.list[builtins.str] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        status: Literal['rejected'],
        rights_id: builtins.str,
        reason: builtins.str,
        creative_id: builtins.str = ...,
        creative_url: builtins.str = ...,
        suggestions: builtins.list[builtins.str] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        status: Literal['pending_review'],
        rights_id: builtins.str,
        creative_id: builtins.str = ...,
        creative_url: builtins.str = ...,
        estimated_response_time: builtins.str = ...,
        status_url: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class DeleteCollectionListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    list_id: builtins.str
    account: _DeleteCollectionListRequestAccountVariant1 | _DeleteCollectionListRequestAccountVariant2 | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    idempotency_key: builtins.str

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _DeleteCollectionListRequestAccountVariant1 | _DeleteCollectionListRequestAccountVariant2 = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class DeleteCollectionListResponse(VersionedSchemaModel):
    deleted: builtins.bool
    list_id: builtins.str
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        deleted: builtins.bool,
        list_id: builtins.str,
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class DeletePropertyListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    list_id: builtins.str
    account: _DeletePropertyListRequestAccountVariant1 | _DeletePropertyListRequestAccountVariant2 | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    idempotency_key: builtins.str

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _DeletePropertyListRequestAccountVariant1 | _DeletePropertyListRequestAccountVariant2 = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class DeletePropertyListResponse(VersionedSchemaModel):
    deleted: builtins.bool
    list_id: builtins.str
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        deleted: builtins.bool,
        list_id: builtins.str,
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetAccountFinancialsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _GetAccountFinancialsRequestAccountVariant1 | _GetAccountFinancialsRequestAccountVariant2
    period: _ExternalCoreDateRange | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        account: _GetAccountFinancialsRequestAccountVariant1 | _GetAccountFinancialsRequestAccountVariant2,
        adcp_major_version: builtins.int = ...,
        period: _ExternalCoreDateRange = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetAccountFinancialsResponse(VersionedSchemaModel):
    account: _GetAccountFinancialsResponseAccountVariant1 | _GetAccountFinancialsResponseAccountVariant2 | None
    currency: builtins.str | None
    period: _ExternalCoreDateRange | None
    timezone: builtins.str | None
    spend: _GetAccountFinancialsResponseSpend | None
    credit: _GetAccountFinancialsResponseCredit | None
    balance: _GetAccountFinancialsResponseBalance | None
    payment_status: Literal['current', 'past_due', 'suspended'] | None
    payment_terms: Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay'] | None
    invoices: builtins.list[_GetAccountFinancialsResponseInvoicesItem] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        account: _GetAccountFinancialsResponseAccountVariant1 | _GetAccountFinancialsResponseAccountVariant2,
        currency: builtins.str,
        period: _ExternalCoreDateRange,
        timezone: builtins.str,
        spend: _GetAccountFinancialsResponseSpend = ...,
        credit: _GetAccountFinancialsResponseCredit = ...,
        balance: _GetAccountFinancialsResponseBalance = ...,
        payment_status: Literal['current', 'past_due', 'suspended'] = ...,
        payment_terms: Literal['net_15', 'net_30', 'net_45', 'net_60', 'net_90', 'prepay'] = ...,
        invoices: builtins.list[_GetAccountFinancialsResponseInvoicesItem] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetAdcpCapabilitiesRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    protocols: builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative']] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        protocols: builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative']] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetAdcpCapabilitiesResponse(VersionedSchemaModel):
    adcp: _GetAdcpCapabilitiesResponseAdcp
    supported_protocols: builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative', 'brand']]
    account: _GetAdcpCapabilitiesResponseAccount | None
    media_buy: _GetAdcpCapabilitiesResponseMediaBuy | None
    signals: _GetAdcpCapabilitiesResponseSignals | None
    governance: _GetAdcpCapabilitiesResponseGovernance | None
    sponsored_intelligence: _GetAdcpCapabilitiesResponseSponsoredIntelligence | None
    brand: _GetAdcpCapabilitiesResponseBrand | None
    creative: _GetAdcpCapabilitiesResponseCreative | None
    request_signing: _GetAdcpCapabilitiesResponseRequestSigning | None
    webhook_signing: _GetAdcpCapabilitiesResponseWebhookSigning | None
    identity: _GetAdcpCapabilitiesResponseIdentity | None
    compliance_testing: _GetAdcpCapabilitiesResponseComplianceTesting | None
    specialisms: builtins.list[Literal['audience-sync', 'brand-rights', 'collection-lists', 'content-standards', 'creative-ad-server', 'creative-generative', 'creative-template', 'governance-aware-seller', 'governance-delivery-monitor', 'governance-spend-authority', 'property-lists', 'sales-broadcast-tv', 'sales-catalog-driven', 'sales-guaranteed', 'sales-non-guaranteed', 'sales-proposal-mode', 'sales-social', 'signal-marketplace', 'signal-owned', 'signed-requests']] | None
    extensions_supported: builtins.list[builtins.str] | None
    experimental_features: builtins.list[builtins.str] | None
    last_updated: builtins.str | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp: _GetAdcpCapabilitiesResponseAdcp,
        supported_protocols: builtins.list[Literal['media_buy', 'signals', 'governance', 'sponsored_intelligence', 'creative', 'brand']],
        account: _GetAdcpCapabilitiesResponseAccount = ...,
        media_buy: _GetAdcpCapabilitiesResponseMediaBuy = ...,
        signals: _GetAdcpCapabilitiesResponseSignals = ...,
        governance: _GetAdcpCapabilitiesResponseGovernance = ...,
        sponsored_intelligence: _GetAdcpCapabilitiesResponseSponsoredIntelligence = ...,
        brand: _GetAdcpCapabilitiesResponseBrand = ...,
        creative: _GetAdcpCapabilitiesResponseCreative = ...,
        request_signing: _GetAdcpCapabilitiesResponseRequestSigning = ...,
        webhook_signing: _GetAdcpCapabilitiesResponseWebhookSigning = ...,
        identity: _GetAdcpCapabilitiesResponseIdentity = ...,
        compliance_testing: _GetAdcpCapabilitiesResponseComplianceTesting = ...,
        specialisms: builtins.list[Literal['audience-sync', 'brand-rights', 'collection-lists', 'content-standards', 'creative-ad-server', 'creative-generative', 'creative-template', 'governance-aware-seller', 'governance-delivery-monitor', 'governance-spend-authority', 'property-lists', 'sales-broadcast-tv', 'sales-catalog-driven', 'sales-guaranteed', 'sales-non-guaranteed', 'sales-proposal-mode', 'sales-social', 'signal-marketplace', 'signal-owned', 'signed-requests']] = ...,
        extensions_supported: builtins.list[builtins.str] = ...,
        experimental_features: builtins.list[builtins.str] = ...,
        last_updated: builtins.str = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetBrandIdentityRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    brand_id: builtins.str
    fields: builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] | None
    use_case: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        brand_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        fields: builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] = ...,
        use_case: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetBrandIdentityResponse(VersionedSchemaModel):
    brand_id: builtins.str | None
    house: _GetBrandIdentityResponseHouse | None
    names: builtins.list[builtins.dict[builtins.str, builtins.str]] | None
    description: builtins.str | None
    industries: builtins.list[builtins.str] | None
    keller_type: Literal['master', 'sub_brand', 'endorsed', 'independent'] | None
    logos: builtins.list[_GetBrandIdentityResponseLogosItem] | None
    colors: _GetBrandIdentityResponseColors | None
    fonts: _GetBrandIdentityResponseFonts | None
    visual_guidelines: builtins.dict[builtins.str, Any] | None
    tone: _GetBrandIdentityResponseTone | None
    tagline: builtins.str | builtins.list[builtins.dict[builtins.str, builtins.str]] | None
    voice_synthesis: _GetBrandIdentityResponseVoiceSynthesis | None
    assets: builtins.list[_GetBrandIdentityResponseAssetsItem] | None
    rights: _GetBrandIdentityResponseRights | None
    available_fields: builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        brand_id: builtins.str,
        house: _GetBrandIdentityResponseHouse,
        names: builtins.list[builtins.dict[builtins.str, builtins.str]],
        description: builtins.str = ...,
        industries: builtins.list[builtins.str] = ...,
        keller_type: Literal['master', 'sub_brand', 'endorsed', 'independent'] = ...,
        logos: builtins.list[_GetBrandIdentityResponseLogosItem] = ...,
        colors: _GetBrandIdentityResponseColors = ...,
        fonts: _GetBrandIdentityResponseFonts = ...,
        visual_guidelines: builtins.dict[builtins.str, Any] = ...,
        tone: _GetBrandIdentityResponseTone = ...,
        tagline: builtins.str | builtins.list[builtins.dict[builtins.str, builtins.str]] = ...,
        voice_synthesis: _GetBrandIdentityResponseVoiceSynthesis = ...,
        assets: builtins.list[_GetBrandIdentityResponseAssetsItem] = ...,
        rights: _GetBrandIdentityResponseRights = ...,
        available_fields: builtins.list[Literal['description', 'industries', 'keller_type', 'logos', 'colors', 'fonts', 'visual_guidelines', 'tone', 'tagline', 'voice_synthesis', 'assets', 'rights']] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetCollectionListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    list_id: builtins.str
    account: _GetCollectionListRequestAccountVariant1 | _GetCollectionListRequestAccountVariant2 | None
    resolve: builtins.bool
    pagination: _GetCollectionListRequestPagination | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _GetCollectionListRequestAccountVariant1 | _GetCollectionListRequestAccountVariant2 = ...,
        resolve: builtins.bool = ...,
        pagination: _GetCollectionListRequestPagination = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetCollectionListResponse(VersionedSchemaModel):
    list: _ExternalCollectionCollectionList
    collections: builtins.list[_GetCollectionListResponseCollectionsItem] | None
    pagination: _ExternalCorePaginationResponse | None
    resolved_at: builtins.str | None
    cache_valid_until: builtins.str | None
    coverage_gaps: builtins.dict[builtins.str, builtins.list[_GetCollectionListResponseCoverageGapsValueItem]] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list: _ExternalCollectionCollectionList,
        collections: builtins.list[_GetCollectionListResponseCollectionsItem] = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        resolved_at: builtins.str = ...,
        cache_valid_until: builtins.str = ...,
        coverage_gaps: builtins.dict[builtins.str, builtins.list[_GetCollectionListResponseCoverageGapsValueItem]] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetContentStandardsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    standards_id: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        standards_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetContentStandardsResponse(VersionedSchemaModel):
    standards_id: builtins.str | None
    name: builtins.str | None
    countries_all: builtins.list[builtins.str] | None
    channels_any: builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] | None
    languages_any: builtins.list[builtins.str] | None
    policies: builtins.list[_ExternalGovernancePolicyEntry] | None
    calibration_exemplars: _GetContentStandardsResponseCalibrationExemplars | None
    pricing_options: builtins.list[_GetContentStandardsResponsePricingOptionsItemVariant1 | _GetContentStandardsResponsePricingOptionsItemVariant2 | _GetContentStandardsResponsePricingOptionsItemVariant3 | _GetContentStandardsResponsePricingOptionsItemVariant4 | _GetContentStandardsResponsePricingOptionsItemVariant5] | None
    ext: builtins.dict[builtins.str, Any] | None
    context: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        standards_id: builtins.str,
        name: builtins.str = ...,
        countries_all: builtins.list[builtins.str] = ...,
        channels_any: builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] = ...,
        languages_any: builtins.list[builtins.str] = ...,
        policies: builtins.list[_ExternalGovernancePolicyEntry] = ...,
        calibration_exemplars: _GetContentStandardsResponseCalibrationExemplars = ...,
        pricing_options: builtins.list[_GetContentStandardsResponsePricingOptionsItemVariant1 | _GetContentStandardsResponsePricingOptionsItemVariant2 | _GetContentStandardsResponsePricingOptionsItemVariant3 | _GetContentStandardsResponsePricingOptionsItemVariant4 | _GetContentStandardsResponsePricingOptionsItemVariant5] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetCreativeDeliveryRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _GetCreativeDeliveryRequestAccountVariant1 | _GetCreativeDeliveryRequestAccountVariant2 | None
    media_buy_ids: builtins.list[builtins.str] | None
    creative_ids: builtins.list[builtins.str] | None
    start_date: builtins.str | None
    end_date: builtins.str | None
    max_variants: builtins.int | None
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        account: _GetCreativeDeliveryRequestAccountVariant1 | _GetCreativeDeliveryRequestAccountVariant2 = ...,
        media_buy_ids: builtins.list[builtins.str] = ...,
        creative_ids: builtins.list[builtins.str] = ...,
        start_date: builtins.str = ...,
        end_date: builtins.str = ...,
        max_variants: builtins.int = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetCreativeDeliveryResponse(VersionedSchemaModel):
    account_id: builtins.str | None
    media_buy_id: builtins.str | None
    currency: builtins.str
    reporting_period: _GetCreativeDeliveryResponseReportingPeriod
    creatives: builtins.list[_GetCreativeDeliveryResponseCreativesItem]
    pagination: _GetCreativeDeliveryResponsePagination | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        currency: builtins.str,
        reporting_period: _GetCreativeDeliveryResponseReportingPeriod,
        creatives: builtins.list[_GetCreativeDeliveryResponseCreativesItem],
        account_id: builtins.str = ...,
        media_buy_id: builtins.str = ...,
        pagination: _GetCreativeDeliveryResponsePagination = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetCreativeFeaturesRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    creative_manifest: _ExternalCoreCreativeManifest
    feature_ids: builtins.list[builtins.str] | None
    account: _GetCreativeFeaturesRequestAccountVariant1 | _GetCreativeFeaturesRequestAccountVariant2 | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        creative_manifest: _ExternalCoreCreativeManifest,
        adcp_major_version: builtins.int = ...,
        feature_ids: builtins.list[builtins.str] = ...,
        account: _GetCreativeFeaturesRequestAccountVariant1 | _GetCreativeFeaturesRequestAccountVariant2 = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetCreativeFeaturesResponse(VersionedSchemaModel):
    results: builtins.list[_ExternalCreativeCreativeFeatureResult] | None
    detail_url: builtins.str | None
    pricing_option_id: builtins.str | None
    vendor_cost: builtins.float | None
    currency: builtins.str | None
    consumption: _ExternalCoreCreativeConsumption | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        results: builtins.list[_ExternalCreativeCreativeFeatureResult],
        detail_url: builtins.str = ...,
        pricing_option_id: builtins.str = ...,
        vendor_cost: builtins.float = ...,
        currency: builtins.str = ...,
        consumption: _ExternalCoreCreativeConsumption = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetMediaBuyArtifactsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _GetMediaBuyArtifactsRequestAccountVariant1 | _GetMediaBuyArtifactsRequestAccountVariant2 | None
    media_buy_id: builtins.str
    package_ids: builtins.list[builtins.str] | None
    failures_only: builtins.bool
    time_range: _GetMediaBuyArtifactsRequestTimeRange | None
    pagination: _GetMediaBuyArtifactsRequestPagination | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        media_buy_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _GetMediaBuyArtifactsRequestAccountVariant1 | _GetMediaBuyArtifactsRequestAccountVariant2 = ...,
        package_ids: builtins.list[builtins.str] = ...,
        failures_only: builtins.bool = ...,
        time_range: _GetMediaBuyArtifactsRequestTimeRange = ...,
        pagination: _GetMediaBuyArtifactsRequestPagination = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetMediaBuyArtifactsResponse(VersionedSchemaModel):
    media_buy_id: builtins.str | None
    artifacts: builtins.list[_GetMediaBuyArtifactsResponseArtifactsItem] | None
    collection_info: _GetMediaBuyArtifactsResponseCollectionInfo | None
    pagination: _ExternalCorePaginationResponse | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        media_buy_id: builtins.str,
        artifacts: builtins.list[_GetMediaBuyArtifactsResponseArtifactsItem],
        collection_info: _GetMediaBuyArtifactsResponseCollectionInfo = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetMediaBuyDeliveryRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _GetMediaBuyDeliveryRequestAccountVariant1 | _GetMediaBuyDeliveryRequestAccountVariant2 | None
    media_buy_ids: builtins.list[builtins.str] | None
    status_filter: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] | None
    start_date: builtins.str | None
    end_date: builtins.str | None
    include_package_daily_breakdown: builtins.bool
    attribution_window: _GetMediaBuyDeliveryRequestAttributionWindow | None
    reporting_dimensions: _GetMediaBuyDeliveryRequestReportingDimensions | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        account: _GetMediaBuyDeliveryRequestAccountVariant1 | _GetMediaBuyDeliveryRequestAccountVariant2 = ...,
        media_buy_ids: builtins.list[builtins.str] = ...,
        status_filter: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] = ...,
        start_date: builtins.str = ...,
        end_date: builtins.str = ...,
        include_package_daily_breakdown: builtins.bool = ...,
        attribution_window: _GetMediaBuyDeliveryRequestAttributionWindow = ...,
        reporting_dimensions: _GetMediaBuyDeliveryRequestReportingDimensions = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetMediaBuyDeliveryResponse(VersionedSchemaModel):
    notification_type: Literal['scheduled', 'final', 'delayed', 'adjusted', 'window_update'] | None
    partial_data: builtins.bool | None
    unavailable_count: builtins.int | None
    sequence_number: builtins.int | None
    next_expected_at: builtins.str | None
    reporting_period: _GetMediaBuyDeliveryResponseReportingPeriod
    currency: builtins.str
    attribution_window: _ExternalCoreAttributionWindow | None
    aggregated_totals: _GetMediaBuyDeliveryResponseAggregatedTotals | None
    media_buy_deliveries: builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItem]
    errors: builtins.list[_ExternalCoreError] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        reporting_period: _GetMediaBuyDeliveryResponseReportingPeriod,
        currency: builtins.str,
        media_buy_deliveries: builtins.list[_GetMediaBuyDeliveryResponseMediaBuyDeliveriesItem],
        notification_type: Literal['scheduled', 'final', 'delayed', 'adjusted', 'window_update'] = ...,
        partial_data: builtins.bool = ...,
        unavailable_count: builtins.int = ...,
        sequence_number: builtins.int = ...,
        next_expected_at: builtins.str = ...,
        attribution_window: _ExternalCoreAttributionWindow = ...,
        aggregated_totals: _GetMediaBuyDeliveryResponseAggregatedTotals = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetMediaBuysRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _GetMediaBuysRequestAccountVariant1 | _GetMediaBuysRequestAccountVariant2 | None
    media_buy_ids: builtins.list[builtins.str] | None
    status_filter: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] | None
    include_snapshot: builtins.bool
    include_history: builtins.int
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        account: _GetMediaBuysRequestAccountVariant1 | _GetMediaBuysRequestAccountVariant2 = ...,
        media_buy_ids: builtins.list[builtins.str] = ...,
        status_filter: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | builtins.list[Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled']] = ...,
        include_snapshot: builtins.bool = ...,
        include_history: builtins.int = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetMediaBuysResponse(VersionedSchemaModel):
    media_buys: builtins.list[_GetMediaBuysResponseMediaBuysItem]
    errors: builtins.list[_ExternalCoreError] | None
    pagination: _ExternalCorePaginationResponse | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        media_buys: builtins.list[_GetMediaBuysResponseMediaBuysItem],
        errors: builtins.list[_ExternalCoreError] = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetPlanAuditLogsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    plan_ids: builtins.list[builtins.str] | None
    portfolio_plan_ids: builtins.list[builtins.str] | None
    governance_contexts: builtins.list[builtins.str] | None
    purchase_types: builtins.list[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']] | None
    include_entries: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        plan_ids: builtins.list[builtins.str] = ...,
        portfolio_plan_ids: builtins.list[builtins.str] = ...,
        governance_contexts: builtins.list[builtins.str] = ...,
        purchase_types: builtins.list[Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']] = ...,
        include_entries: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetPlanAuditLogsResponse(VersionedSchemaModel):
    plans: builtins.list[_GetPlanAuditLogsResponsePlansItem]
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        plans: builtins.list[_GetPlanAuditLogsResponsePlansItem],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetProductsInputRequiredResponse(VersionedSchemaModel):
    reason: Literal['CLARIFICATION_NEEDED', 'BUDGET_REQUIRED'] | None
    partial_results: builtins.list[_ExternalCoreProduct] | None
    suggestions: builtins.list[builtins.str] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        reason: Literal['CLARIFICATION_NEEDED', 'BUDGET_REQUIRED'] = ...,
        partial_results: builtins.list[_ExternalCoreProduct] = ...,
        suggestions: builtins.list[builtins.str] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetProductsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    buying_mode: Literal['brief', 'wholesale', 'refine']
    brief: builtins.str | None
    refine: builtins.list[_GetProductsRequestRefineItemVariant1 | _GetProductsRequestRefineItemVariant2 | _GetProductsRequestRefineItemVariant3] | None
    brand: _ExternalCoreBrandRef | None
    catalog: _ExternalCoreCatalog | None
    account: _GetProductsRequestAccountVariant1 | _GetProductsRequestAccountVariant2 | None
    preferred_delivery_types: builtins.list[Literal['guaranteed', 'non_guaranteed']] | None
    filters: _ExternalCoreProductFilters | None
    property_list: _ExternalCorePropertyListRef | None
    fields: builtins.list[Literal['product_id', 'name', 'description', 'publisher_properties', 'channels', 'format_ids', 'placements', 'delivery_type', 'exclusivity', 'pricing_options', 'forecast', 'outcome_measurement', 'delivery_measurement', 'reporting_capabilities', 'creative_policy', 'catalog_types', 'metric_optimization', 'conversion_tracking', 'data_provider_signals', 'max_optimization_goals', 'catalog_match', 'collections', 'collection_targeting_allowed', 'installments', 'brief_relevance', 'expires_at', 'product_card', 'product_card_detailed', 'enforced_policies', 'trusted_match']] | None
    time_budget: _GetProductsRequestTimeBudget | None
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    required_policies: builtins.list[builtins.str] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        buying_mode: Literal['brief', 'wholesale', 'refine'],
        adcp_major_version: builtins.int = ...,
        brief: builtins.str = ...,
        refine: builtins.list[_GetProductsRequestRefineItemVariant1 | _GetProductsRequestRefineItemVariant2 | _GetProductsRequestRefineItemVariant3] = ...,
        brand: _ExternalCoreBrandRef = ...,
        catalog: _ExternalCoreCatalog = ...,
        account: _GetProductsRequestAccountVariant1 | _GetProductsRequestAccountVariant2 = ...,
        preferred_delivery_types: builtins.list[Literal['guaranteed', 'non_guaranteed']] = ...,
        filters: _ExternalCoreProductFilters = ...,
        property_list: _ExternalCorePropertyListRef = ...,
        fields: builtins.list[Literal['product_id', 'name', 'description', 'publisher_properties', 'channels', 'format_ids', 'placements', 'delivery_type', 'exclusivity', 'pricing_options', 'forecast', 'outcome_measurement', 'delivery_measurement', 'reporting_capabilities', 'creative_policy', 'catalog_types', 'metric_optimization', 'conversion_tracking', 'data_provider_signals', 'max_optimization_goals', 'catalog_match', 'collections', 'collection_targeting_allowed', 'installments', 'brief_relevance', 'expires_at', 'product_card', 'product_card_detailed', 'enforced_policies', 'trusted_match']] = ...,
        time_budget: _GetProductsRequestTimeBudget = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        required_policies: builtins.list[builtins.str] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetProductsSubmittedResponse(VersionedSchemaModel):
    estimated_completion: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        estimated_completion: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetProductsResponse(VersionedSchemaModel):
    products: builtins.list[_ExternalCoreProduct]
    proposals: builtins.list[_ExternalCoreProposal] | None
    errors: builtins.list[_ExternalCoreError] | None
    property_list_applied: builtins.bool | None
    catalog_applied: builtins.bool | None
    refinement_applied: builtins.list[_GetProductsResponseRefinementAppliedItemVariant1 | _GetProductsResponseRefinementAppliedItemVariant2 | _GetProductsResponseRefinementAppliedItemVariant3] | None
    incomplete: builtins.list[_GetProductsResponseIncompleteItem] | None
    pagination: _ExternalCorePaginationResponse | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        products: builtins.list[_ExternalCoreProduct],
        proposals: builtins.list[_ExternalCoreProposal] = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        property_list_applied: builtins.bool = ...,
        catalog_applied: builtins.bool = ...,
        refinement_applied: builtins.list[_GetProductsResponseRefinementAppliedItemVariant1 | _GetProductsResponseRefinementAppliedItemVariant2 | _GetProductsResponseRefinementAppliedItemVariant3] = ...,
        incomplete: builtins.list[_GetProductsResponseIncompleteItem] = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetProductsWorkingResponse(VersionedSchemaModel):
    percentage: builtins.float | None
    current_step: builtins.str | None
    total_steps: builtins.int | None
    step_number: builtins.int | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        percentage: builtins.float = ...,
        current_step: builtins.str = ...,
        total_steps: builtins.int = ...,
        step_number: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetPropertyListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    list_id: builtins.str
    account: _GetPropertyListRequestAccountVariant1 | _GetPropertyListRequestAccountVariant2 | None
    resolve: builtins.bool
    pagination: _GetPropertyListRequestPagination | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _GetPropertyListRequestAccountVariant1 | _GetPropertyListRequestAccountVariant2 = ...,
        resolve: builtins.bool = ...,
        pagination: _GetPropertyListRequestPagination = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetPropertyListResponse(VersionedSchemaModel):
    list: _ExternalPropertyPropertyList
    identifiers: builtins.list[_ExternalCoreIdentifier] | None
    pagination: _ExternalCorePaginationResponse | None
    resolved_at: builtins.str | None
    cache_valid_until: builtins.str | None
    coverage_gaps: builtins.dict[builtins.str, builtins.list[_ExternalCoreIdentifier]] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list: _ExternalPropertyPropertyList,
        identifiers: builtins.list[_ExternalCoreIdentifier] = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        resolved_at: builtins.str = ...,
        cache_valid_until: builtins.str = ...,
        coverage_gaps: builtins.dict[builtins.str, builtins.list[_ExternalCoreIdentifier]] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetRightsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    query: builtins.str
    uses: builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']]
    buyer_brand: _ExternalCoreBrandRef | None
    countries: builtins.list[builtins.str] | None
    brand_id: builtins.str | None
    right_type: Literal['talent', 'character', 'brand_ip', 'music', 'stock_media'] | None
    include_excluded: builtins.bool
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        query: builtins.str,
        uses: builtins.list[Literal['likeness', 'voice', 'name', 'endorsement', 'motion_capture', 'signature', 'catchphrase', 'sync', 'background_music', 'editorial', 'commercial', 'ai_generated_image']],
        adcp_major_version: builtins.int = ...,
        buyer_brand: _ExternalCoreBrandRef = ...,
        countries: builtins.list[builtins.str] = ...,
        brand_id: builtins.str = ...,
        right_type: Literal['talent', 'character', 'brand_ip', 'music', 'stock_media'] = ...,
        include_excluded: builtins.bool = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetRightsResponse(VersionedSchemaModel):
    rights: builtins.list[_GetRightsResponseRightsItem] | None
    excluded: builtins.list[_GetRightsResponseExcludedItem] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights: builtins.list[_GetRightsResponseRightsItem],
        excluded: builtins.list[_GetRightsResponseExcludedItem] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetSignalsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _GetSignalsRequestAccountVariant1 | _GetSignalsRequestAccountVariant2 | None
    signal_spec: builtins.str | None
    signal_ids: builtins.list[_GetSignalsRequestSignalIdsItemVariant1 | _GetSignalsRequestSignalIdsItemVariant2] | None
    destinations: builtins.list[_GetSignalsRequestDestinationsItemVariant1 | _GetSignalsRequestDestinationsItemVariant2] | None
    countries: builtins.list[builtins.str] | None
    filters: _ExternalCoreSignalFilters | None
    max_results: builtins.int | None
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        account: _GetSignalsRequestAccountVariant1 | _GetSignalsRequestAccountVariant2 = ...,
        signal_spec: builtins.str = ...,
        signal_ids: builtins.list[_GetSignalsRequestSignalIdsItemVariant1 | _GetSignalsRequestSignalIdsItemVariant2] = ...,
        destinations: builtins.list[_GetSignalsRequestDestinationsItemVariant1 | _GetSignalsRequestDestinationsItemVariant2] = ...,
        countries: builtins.list[builtins.str] = ...,
        filters: _ExternalCoreSignalFilters = ...,
        max_results: builtins.int = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class GetSignalsResponse(VersionedSchemaModel):
    signals: builtins.list[_GetSignalsResponseSignalsItem]
    errors: builtins.list[_ExternalCoreError] | None
    pagination: _ExternalCorePaginationResponse | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        signals: builtins.list[_GetSignalsResponseSignalsItem],
        errors: builtins.list[_ExternalCoreError] = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class IdentityMatchRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    type: Literal['identity_match_request']
    protocol_version: builtins.str
    request_id: builtins.str
    seller_agent_url: builtins.str
    identities: builtins.list[_IdentityMatchRequestIdentitiesItem]
    consent: _IdentityMatchRequestConsent | None
    package_ids: builtins.list[builtins.str] | None
    country: builtins.str | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        type: Literal['identity_match_request'],
        request_id: builtins.str,
        seller_agent_url: builtins.str,
        identities: builtins.list[_IdentityMatchRequestIdentitiesItem],
        adcp_major_version: builtins.int = ...,
        protocol_version: builtins.str = ...,
        consent: _IdentityMatchRequestConsent = ...,
        package_ids: builtins.list[builtins.str] = ...,
        country: builtins.str = ...,
    ) -> None: ...

class IdentityMatchResponse(VersionedSchemaModel):
    type: Literal['identity_match_response']
    request_id: builtins.str
    eligible_package_ids: builtins.list[builtins.str]
    serve_window_sec: builtins.int
    tmpx: builtins.str | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        type: Literal['identity_match_response'],
        request_id: builtins.str,
        eligible_package_ids: builtins.list[builtins.str],
        serve_window_sec: builtins.int,
        tmpx: builtins.str = ...,
    ) -> None: ...

class ListAccountsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    status: Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed'] | None
    pagination: _ExternalCorePaginationRequest | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        status: Literal['active', 'pending_approval', 'rejected', 'payment_required', 'suspended', 'closed'] = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListAccountsResponse(VersionedSchemaModel):
    accounts: builtins.list[_ExternalCoreAccount]
    errors: builtins.list[_ExternalCoreError] | None
    pagination: _ExternalCorePaginationResponse | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        accounts: builtins.list[_ExternalCoreAccount],
        errors: builtins.list[_ExternalCoreError] = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListCollectionListsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _ListCollectionListsRequestAccountVariant1 | _ListCollectionListsRequestAccountVariant2 | None
    name_contains: builtins.str | None
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        account: _ListCollectionListsRequestAccountVariant1 | _ListCollectionListsRequestAccountVariant2 = ...,
        name_contains: builtins.str = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListCollectionListsResponse(VersionedSchemaModel):
    lists: builtins.list[_ExternalCollectionCollectionList]
    pagination: _ExternalCorePaginationResponse | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        lists: builtins.list[_ExternalCollectionCollectionList],
        pagination: _ExternalCorePaginationResponse = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListContentStandardsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    channels: builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] | None
    languages: builtins.list[builtins.str] | None
    countries: builtins.list[builtins.str] | None
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        channels: builtins.list[Literal['display', 'olv', 'social', 'search', 'ctv', 'linear_tv', 'radio', 'streaming_audio', 'podcast', 'dooh', 'ooh', 'print', 'cinema', 'email', 'gaming', 'retail_media', 'influencer', 'affiliate', 'product_placement', 'sponsored_intelligence']] = ...,
        languages: builtins.list[builtins.str] = ...,
        countries: builtins.list[builtins.str] = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListContentStandardsResponse(VersionedSchemaModel):
    standards: builtins.list[_ExternalContentStandardsContentStandards] | None
    pagination: _ExternalCorePaginationResponse | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        standards: builtins.list[_ExternalContentStandardsContentStandards],
        pagination: _ExternalCorePaginationResponse = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListCreativeFormatsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    format_ids: builtins.list[_ExternalCoreFormatId] | None
    type: Literal['audio', 'video', 'display', 'dooh'] | None
    asset_types: builtins.list[Literal['image', 'video', 'audio', 'text', 'html', 'javascript', 'url']] | None
    max_width: builtins.int | None
    max_height: builtins.int | None
    min_width: builtins.int | None
    min_height: builtins.int | None
    is_responsive: builtins.bool | None
    name_search: builtins.str | None
    wcag_level: Literal['A', 'AA', 'AAA'] | None
    disclosure_positions: builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']] | None
    disclosure_persistence: builtins.list[Literal['continuous', 'initial', 'flexible']] | None
    output_format_ids: builtins.list[_ExternalCoreFormatId] | None
    input_format_ids: builtins.list[_ExternalCoreFormatId] | None
    include_pricing: builtins.bool
    account: _ListCreativeFormatsRequestAccountVariant1 | _ListCreativeFormatsRequestAccountVariant2 | None
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        format_ids: builtins.list[_ExternalCoreFormatId] = ...,
        type: Literal['audio', 'video', 'display', 'dooh'] = ...,
        asset_types: builtins.list[Literal['image', 'video', 'audio', 'text', 'html', 'javascript', 'url']] = ...,
        max_width: builtins.int = ...,
        max_height: builtins.int = ...,
        min_width: builtins.int = ...,
        min_height: builtins.int = ...,
        is_responsive: builtins.bool = ...,
        name_search: builtins.str = ...,
        wcag_level: Literal['A', 'AA', 'AAA'] = ...,
        disclosure_positions: builtins.list[Literal['prominent', 'footer', 'audio', 'subtitle', 'overlay', 'end_card', 'pre_roll', 'companion']] = ...,
        disclosure_persistence: builtins.list[Literal['continuous', 'initial', 'flexible']] = ...,
        output_format_ids: builtins.list[_ExternalCoreFormatId] = ...,
        input_format_ids: builtins.list[_ExternalCoreFormatId] = ...,
        include_pricing: builtins.bool = ...,
        account: _ListCreativeFormatsRequestAccountVariant1 | _ListCreativeFormatsRequestAccountVariant2 = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListCreativeFormatsResponse(VersionedSchemaModel):
    formats: builtins.list[_ExternalCoreFormat]
    creative_agents: builtins.list[_ListCreativeFormatsResponseCreativeAgentsItem] | None
    errors: builtins.list[_ExternalCoreError] | None
    pagination: _ExternalCorePaginationResponse | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        formats: builtins.list[_ExternalCoreFormat],
        creative_agents: builtins.list[_ListCreativeFormatsResponseCreativeAgentsItem] = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        pagination: _ExternalCorePaginationResponse = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListCreativesRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    filters: _ExternalCoreCreativeFilters | None
    sort: _ListCreativesRequestSort | None
    pagination: _ExternalCorePaginationRequest | None
    include_assignments: builtins.bool
    include_snapshot: builtins.bool
    include_items: builtins.bool
    include_variables: builtins.bool
    include_pricing: builtins.bool
    account: _ListCreativesRequestAccountVariant1 | _ListCreativesRequestAccountVariant2 | None
    fields: builtins.list[Literal['creative_id', 'name', 'format_id', 'status', 'created_date', 'updated_date', 'tags', 'assignments', 'snapshot', 'items', 'variables', 'concept', 'pricing_options']] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        filters: _ExternalCoreCreativeFilters = ...,
        sort: _ListCreativesRequestSort = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        include_assignments: builtins.bool = ...,
        include_snapshot: builtins.bool = ...,
        include_items: builtins.bool = ...,
        include_variables: builtins.bool = ...,
        include_pricing: builtins.bool = ...,
        account: _ListCreativesRequestAccountVariant1 | _ListCreativesRequestAccountVariant2 = ...,
        fields: builtins.list[Literal['creative_id', 'name', 'format_id', 'status', 'created_date', 'updated_date', 'tags', 'assignments', 'snapshot', 'items', 'variables', 'concept', 'pricing_options']] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListCreativesResponse(VersionedSchemaModel):
    query_summary: _ListCreativesResponseQuerySummary
    pagination: _ExternalCorePaginationResponse
    creatives: builtins.list[_ListCreativesResponseCreativesItem]
    format_summary: builtins.dict[builtins.str, Any] | None
    status_summary: _ListCreativesResponseStatusSummary | None
    errors: builtins.list[_ExternalCoreError] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        query_summary: _ListCreativesResponseQuerySummary,
        pagination: _ExternalCorePaginationResponse,
        creatives: builtins.list[_ListCreativesResponseCreativesItem],
        format_summary: builtins.dict[builtins.str, Any] = ...,
        status_summary: _ListCreativesResponseStatusSummary = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListPropertyListsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _ListPropertyListsRequestAccountVariant1 | _ListPropertyListsRequestAccountVariant2 | None
    name_contains: builtins.str | None
    pagination: _ExternalCorePaginationRequest | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        account: _ListPropertyListsRequestAccountVariant1 | _ListPropertyListsRequestAccountVariant2 = ...,
        name_contains: builtins.str = ...,
        pagination: _ExternalCorePaginationRequest = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ListPropertyListsResponse(VersionedSchemaModel):
    lists: builtins.list[_ExternalPropertyPropertyList]
    pagination: _ExternalCorePaginationResponse | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        lists: builtins.list[_ExternalPropertyPropertyList],
        pagination: _ExternalCorePaginationResponse = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class LogEventRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    event_source_id: builtins.str
    test_event_code: builtins.str | None
    events: builtins.list[_ExternalCoreEvent]
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        event_source_id: builtins.str,
        events: builtins.list[_ExternalCoreEvent],
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        test_event_code: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class LogEventResponse(VersionedSchemaModel):
    events_received: builtins.int | None
    events_processed: builtins.int | None
    partial_failures: builtins.list[_LogEventResponsePartialFailuresItem] | None
    warnings: builtins.list[builtins.str] | None
    match_quality: builtins.float | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        events_received: builtins.int,
        events_processed: builtins.int,
        partial_failures: builtins.list[_LogEventResponsePartialFailuresItem] = ...,
        warnings: builtins.list[builtins.str] = ...,
        match_quality: builtins.float = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class PackageRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    product_id: builtins.str
    format_ids: builtins.list[_ExternalCoreFormatId] | None
    budget: builtins.float
    pacing: Literal['even', 'asap', 'front_loaded'] | None
    pricing_option_id: builtins.str
    bid_price: builtins.float | None
    impressions: builtins.float | None
    start_time: builtins.str | None
    end_time: builtins.str | None
    paused: builtins.bool
    catalogs: builtins.list[_ExternalCoreCatalog] | None
    optimization_goals: builtins.list[_PackageRequestOptimizationGoalsItemVariant1 | _PackageRequestOptimizationGoalsItemVariant2] | None
    targeting_overlay: _ExternalCoreTargeting | None
    measurement_terms: _ExternalCoreMeasurementTerms | None
    performance_standards: builtins.list[_ExternalCorePerformanceStandard] | None
    creative_assignments: builtins.list[_ExternalCoreCreativeAssignment] | None
    creatives: builtins.list[_ExternalCoreCreativeAsset] | None
    agency_estimate_number: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        product_id: builtins.str,
        budget: builtins.float,
        pricing_option_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        format_ids: builtins.list[_ExternalCoreFormatId] = ...,
        pacing: Literal['even', 'asap', 'front_loaded'] = ...,
        bid_price: builtins.float = ...,
        impressions: builtins.float = ...,
        start_time: builtins.str = ...,
        end_time: builtins.str = ...,
        paused: builtins.bool = ...,
        catalogs: builtins.list[_ExternalCoreCatalog] = ...,
        optimization_goals: builtins.list[_PackageRequestOptimizationGoalsItemVariant1 | _PackageRequestOptimizationGoalsItemVariant2] = ...,
        targeting_overlay: _ExternalCoreTargeting = ...,
        measurement_terms: _ExternalCoreMeasurementTerms = ...,
        performance_standards: builtins.list[_ExternalCorePerformanceStandard] = ...,
        creative_assignments: builtins.list[_ExternalCoreCreativeAssignment] = ...,
        creatives: builtins.list[_ExternalCoreCreativeAsset] = ...,
        agency_estimate_number: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class PreviewCreativeRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    request_type: Literal['single', 'batch', 'variant']
    creative_manifest: _ExternalCoreCreativeManifest | None
    format_id: _ExternalCoreFormatId | None
    inputs: builtins.list[_PreviewCreativeRequestInputsItem] | None
    template_id: builtins.str | None
    quality: Literal['draft', 'production'] | None
    output_format: Literal['url', 'html']
    item_limit: builtins.int | None
    requests: builtins.list[_PreviewCreativeRequestRequestsItem] | None
    variant_id: builtins.str | None
    creative_id: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        request_type: Literal['single', 'batch', 'variant'],
        adcp_major_version: builtins.int = ...,
        creative_manifest: _ExternalCoreCreativeManifest = ...,
        format_id: _ExternalCoreFormatId = ...,
        inputs: builtins.list[_PreviewCreativeRequestInputsItem] = ...,
        template_id: builtins.str = ...,
        quality: Literal['draft', 'production'] = ...,
        output_format: Literal['url', 'html'] = ...,
        item_limit: builtins.int = ...,
        requests: builtins.list[_PreviewCreativeRequestRequestsItem] = ...,
        variant_id: builtins.str = ...,
        creative_id: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class PreviewCreativeResponse(VersionedSchemaModel):
    response_type: Literal['single'] | Literal['batch'] | Literal['variant']
    previews: builtins.list[_PreviewCreativeResponsePreviewsItem] | builtins.list[_PreviewCreativeResponsePreviewsItem2] | None
    interactive_url: builtins.str | None
    expires_at: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    results: builtins.list[_PreviewCreativeResponseResultsItemVariant1 | _PreviewCreativeResponseResultsItemVariant2] | None
    variant_id: builtins.str | None
    creative_id: builtins.str | None
    manifest: _ExternalCoreCreativeManifest | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        response_type: Literal['single'],
        previews: builtins.list[_PreviewCreativeResponsePreviewsItem],
        expires_at: builtins.str,
        interactive_url: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        response_type: Literal['batch'],
        results: builtins.list[_PreviewCreativeResponseResultsItemVariant1 | _PreviewCreativeResponseResultsItemVariant2],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        response_type: Literal['variant'],
        variant_id: builtins.str,
        previews: builtins.list[_PreviewCreativeResponsePreviewsItem2],
        creative_id: builtins.str = ...,
        manifest: _ExternalCoreCreativeManifest = ...,
        expires_at: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ProvidePerformanceFeedbackRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    media_buy_id: builtins.str
    idempotency_key: builtins.str
    measurement_period: _ExternalCoreDatetimeRange
    performance_index: builtins.float
    package_id: builtins.str | None
    creative_id: builtins.str | None
    metric_type: Literal['overall_performance', 'conversion_rate', 'brand_lift', 'click_through_rate', 'completion_rate', 'viewability', 'brand_safety', 'cost_efficiency']
    feedback_source: Literal['buyer_attribution', 'third_party_measurement', 'platform_analytics', 'verification_partner']
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        media_buy_id: builtins.str,
        idempotency_key: builtins.str,
        measurement_period: _ExternalCoreDatetimeRange,
        performance_index: builtins.float,
        adcp_major_version: builtins.int = ...,
        package_id: builtins.str = ...,
        creative_id: builtins.str = ...,
        metric_type: Literal['overall_performance', 'conversion_rate', 'brand_lift', 'click_through_rate', 'completion_rate', 'viewability', 'brand_safety', 'cost_efficiency'] = ...,
        feedback_source: Literal['buyer_attribution', 'third_party_measurement', 'platform_analytics', 'verification_partner'] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ProvidePerformanceFeedbackResponse(VersionedSchemaModel):
    success: Literal[True] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[True],
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ReportPlanOutcomeRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    plan_id: builtins.str
    check_id: builtins.str | None
    idempotency_key: builtins.str
    purchase_type: Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services']
    outcome: Literal['completed', 'failed', 'delivery']
    seller_response: _ReportPlanOutcomeRequestSellerResponse | None
    delivery: _ReportPlanOutcomeRequestDelivery | None
    error: _ReportPlanOutcomeRequestError | None
    governance_context: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        plan_id: builtins.str,
        idempotency_key: builtins.str,
        outcome: Literal['completed', 'failed', 'delivery'],
        governance_context: builtins.str,
        adcp_major_version: builtins.int = ...,
        check_id: builtins.str = ...,
        purchase_type: Literal['media_buy', 'rights_license', 'signal_activation', 'creative_services'] = ...,
        seller_response: _ReportPlanOutcomeRequestSellerResponse = ...,
        delivery: _ReportPlanOutcomeRequestDelivery = ...,
        error: _ReportPlanOutcomeRequestError = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ReportPlanOutcomeResponse(VersionedSchemaModel):
    outcome_id: builtins.str
    status: Literal['accepted', 'findings']
    committed_budget: builtins.float | None
    findings: builtins.list[_ReportPlanOutcomeResponseFindingsItem] | None
    plan_summary: _ReportPlanOutcomeResponsePlanSummary | None
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        outcome_id: builtins.str,
        status: Literal['accepted', 'findings'],
        committed_budget: builtins.float = ...,
        findings: builtins.list[_ReportPlanOutcomeResponseFindingsItem] = ...,
        plan_summary: _ReportPlanOutcomeResponsePlanSummary = ...,
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ReportUsageRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    reporting_period: _ExternalCoreDatetimeRange
    usage: builtins.list[_ReportUsageRequestUsageItem]
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        reporting_period: _ExternalCoreDatetimeRange,
        usage: builtins.list[_ReportUsageRequestUsageItem],
        adcp_major_version: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ReportUsageResponse(VersionedSchemaModel):
    accepted: builtins.int
    errors: builtins.list[_ExternalCoreError] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        accepted: builtins.int,
        errors: builtins.list[_ExternalCoreError] = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiGetOfferingRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    offering_id: builtins.str
    intent: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    include_products: builtins.bool
    product_limit: builtins.int
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        offering_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        intent: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        include_products: builtins.bool = ...,
        product_limit: builtins.int = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiGetOfferingResponse(VersionedSchemaModel):
    available: builtins.bool
    offering_token: builtins.str | None
    ttl_seconds: builtins.int | None
    checked_at: builtins.str | None
    offering: _SiGetOfferingResponseOffering | None
    matching_products: builtins.list[_SiGetOfferingResponseMatchingProductsItem] | None
    total_matching: builtins.int | None
    unavailable_reason: builtins.str | None
    alternative_offering_ids: builtins.list[builtins.str] | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        available: builtins.bool,
        offering_token: builtins.str = ...,
        ttl_seconds: builtins.int = ...,
        checked_at: builtins.str = ...,
        offering: _SiGetOfferingResponseOffering = ...,
        matching_products: builtins.list[_SiGetOfferingResponseMatchingProductsItem] = ...,
        total_matching: builtins.int = ...,
        unavailable_reason: builtins.str = ...,
        alternative_offering_ids: builtins.list[builtins.str] = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiInitiateSessionRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    intent: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    identity: _ExternalSponsoredIntelligenceSiIdentity
    media_buy_id: builtins.str | None
    placement: builtins.str | None
    offering_id: builtins.str | None
    supported_capabilities: _ExternalSponsoredIntelligenceSiCapabilities | None
    offering_token: builtins.str | None
    idempotency_key: builtins.str
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        intent: builtins.str,
        identity: _ExternalSponsoredIntelligenceSiIdentity,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        media_buy_id: builtins.str = ...,
        placement: builtins.str = ...,
        offering_id: builtins.str = ...,
        supported_capabilities: _ExternalSponsoredIntelligenceSiCapabilities = ...,
        offering_token: builtins.str = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiInitiateSessionResponse(VersionedSchemaModel):
    session_id: builtins.str
    response: _SiInitiateSessionResponseResponse | None
    negotiated_capabilities: _ExternalSponsoredIntelligenceSiCapabilities | None
    session_status: Literal['active', 'pending_handoff', 'complete', 'terminated']
    session_ttl_seconds: builtins.int | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        session_id: builtins.str,
        session_status: Literal['active', 'pending_handoff', 'complete', 'terminated'],
        response: _SiInitiateSessionResponseResponse = ...,
        negotiated_capabilities: _ExternalSponsoredIntelligenceSiCapabilities = ...,
        session_ttl_seconds: builtins.int = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiSendMessageRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    session_id: builtins.str
    message: builtins.str | None
    action_response: _SiSendMessageRequestActionResponse | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        session_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        message: builtins.str = ...,
        action_response: _SiSendMessageRequestActionResponse = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiSendMessageResponse(VersionedSchemaModel):
    session_id: builtins.str
    response: _SiSendMessageResponseResponse | None
    mcp_resource_uri: builtins.str | None
    session_status: Literal['active', 'pending_handoff', 'complete', 'terminated']
    handoff: _SiSendMessageResponseHandoff | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        session_id: builtins.str,
        session_status: Literal['active', 'pending_handoff', 'complete', 'terminated'],
        response: _SiSendMessageResponseResponse = ...,
        mcp_resource_uri: builtins.str = ...,
        handoff: _SiSendMessageResponseHandoff = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiTerminateSessionRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    session_id: builtins.str
    reason: Literal['handoff_transaction', 'handoff_complete', 'user_exit', 'session_timeout', 'host_terminated']
    termination_context: _SiTerminateSessionRequestTerminationContext | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        session_id: builtins.str,
        reason: Literal['handoff_transaction', 'handoff_complete', 'user_exit', 'session_timeout', 'host_terminated'],
        adcp_major_version: builtins.int = ...,
        termination_context: _SiTerminateSessionRequestTerminationContext = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SiTerminateSessionResponse(VersionedSchemaModel):
    session_id: builtins.str
    terminated: builtins.bool
    session_status: Literal['active', 'pending_handoff', 'complete', 'terminated'] | None
    acp_handoff: _SiTerminateSessionResponseAcpHandoff | None
    follow_up: _SiTerminateSessionResponseFollowUp | None
    errors: builtins.list[_ExternalCoreError] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        session_id: builtins.str,
        terminated: builtins.bool,
        session_status: Literal['active', 'pending_handoff', 'complete', 'terminated'] = ...,
        acp_handoff: _SiTerminateSessionResponseAcpHandoff = ...,
        follow_up: _SiTerminateSessionResponseFollowUp = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncAccountsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    accounts: builtins.list[_SyncAccountsRequestAccountsItem]
    delete_missing: builtins.bool
    dry_run: builtins.bool
    push_notification_config: _ExternalCorePushNotificationConfig | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        accounts: builtins.list[_SyncAccountsRequestAccountsItem],
        adcp_major_version: builtins.int = ...,
        delete_missing: builtins.bool = ...,
        dry_run: builtins.bool = ...,
        push_notification_config: _ExternalCorePushNotificationConfig = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncAccountsResponse(VersionedSchemaModel):
    dry_run: builtins.bool | None
    accounts: builtins.list[_SyncAccountsResponseAccountsItem] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        accounts: builtins.list[_SyncAccountsResponseAccountsItem],
        dry_run: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncAudiencesRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    account: _SyncAudiencesRequestAccountVariant1 | _SyncAudiencesRequestAccountVariant2
    audiences: builtins.list[_SyncAudiencesRequestAudiencesItem] | None
    delete_missing: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        account: _SyncAudiencesRequestAccountVariant1 | _SyncAudiencesRequestAccountVariant2,
        adcp_major_version: builtins.int = ...,
        audiences: builtins.list[_SyncAudiencesRequestAudiencesItem] = ...,
        delete_missing: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncAudiencesResponse(VersionedSchemaModel):
    audiences: builtins.list[_SyncAudiencesResponseAudiencesItem] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        audiences: builtins.list[_SyncAudiencesResponseAudiencesItem],
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCatalogsInputRequiredResponse(VersionedSchemaModel):
    reason: Literal['APPROVAL_REQUIRED', 'FEED_VALIDATION', 'ITEM_REVIEW', 'FEED_ACCESS'] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        reason: Literal['APPROVAL_REQUIRED', 'FEED_VALIDATION', 'ITEM_REVIEW', 'FEED_ACCESS'] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCatalogsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    account: _SyncCatalogsRequestAccountVariant1 | _SyncCatalogsRequestAccountVariant2
    catalogs: builtins.list[_ExternalCoreCatalog] | None
    catalog_ids: builtins.list[builtins.str] | None
    delete_missing: builtins.bool
    dry_run: builtins.bool
    validation_mode: Literal['strict', 'lenient']
    push_notification_config: _ExternalCorePushNotificationConfig | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        account: _SyncCatalogsRequestAccountVariant1 | _SyncCatalogsRequestAccountVariant2,
        adcp_major_version: builtins.int = ...,
        catalogs: builtins.list[_ExternalCoreCatalog] = ...,
        catalog_ids: builtins.list[builtins.str] = ...,
        delete_missing: builtins.bool = ...,
        dry_run: builtins.bool = ...,
        validation_mode: Literal['strict', 'lenient'] = ...,
        push_notification_config: _ExternalCorePushNotificationConfig = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCatalogsSubmittedResponse(VersionedSchemaModel):
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCatalogsResponse(VersionedSchemaModel):
    dry_run: builtins.bool | None
    catalogs: builtins.list[_SyncCatalogsResponseCatalogsItem] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        catalogs: builtins.list[_SyncCatalogsResponseCatalogsItem],
        dry_run: builtins.bool = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCatalogsWorkingResponse(VersionedSchemaModel):
    percentage: builtins.float | None
    current_step: builtins.str | None
    total_steps: builtins.int | None
    step_number: builtins.int | None
    catalogs_processed: builtins.int | None
    catalogs_total: builtins.int | None
    items_processed: builtins.int | None
    items_total: builtins.int | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        percentage: builtins.float = ...,
        current_step: builtins.str = ...,
        total_steps: builtins.int = ...,
        step_number: builtins.int = ...,
        catalogs_processed: builtins.int = ...,
        catalogs_total: builtins.int = ...,
        items_processed: builtins.int = ...,
        items_total: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCreativesInputRequiredResponse(VersionedSchemaModel):
    reason: Literal['APPROVAL_REQUIRED', 'ASSET_CONFIRMATION', 'FORMAT_CLARIFICATION'] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        reason: Literal['APPROVAL_REQUIRED', 'ASSET_CONFIRMATION', 'FORMAT_CLARIFICATION'] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCreativesRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _SyncCreativesRequestAccountVariant1 | _SyncCreativesRequestAccountVariant2
    creatives: builtins.list[_ExternalCoreCreativeAsset]
    creative_ids: builtins.list[builtins.str] | None
    assignments: builtins.list[_SyncCreativesRequestAssignmentsItem] | None
    idempotency_key: builtins.str
    delete_missing: builtins.bool
    dry_run: builtins.bool
    validation_mode: Literal['strict', 'lenient']
    push_notification_config: _ExternalCorePushNotificationConfig | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        account: _SyncCreativesRequestAccountVariant1 | _SyncCreativesRequestAccountVariant2,
        creatives: builtins.list[_ExternalCoreCreativeAsset],
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        creative_ids: builtins.list[builtins.str] = ...,
        assignments: builtins.list[_SyncCreativesRequestAssignmentsItem] = ...,
        delete_missing: builtins.bool = ...,
        dry_run: builtins.bool = ...,
        validation_mode: Literal['strict', 'lenient'] = ...,
        push_notification_config: _ExternalCorePushNotificationConfig = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCreativesSubmittedResponse(VersionedSchemaModel):
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCreativesResponse(VersionedSchemaModel):
    dry_run: builtins.bool | None
    creatives: builtins.list[_SyncCreativesResponseCreativesItem] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None
    status: Literal['submitted'] | None
    task_id: builtins.str | None
    message: builtins.str | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        creatives: builtins.list[_SyncCreativesResponseCreativesItem],
        dry_run: builtins.bool = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        status: Literal['submitted'],
        task_id: builtins.str,
        message: builtins.str = ...,
        errors: builtins.list[_ExternalCoreError] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncCreativesWorkingResponse(VersionedSchemaModel):
    percentage: builtins.float | None
    current_step: builtins.str | None
    total_steps: builtins.int | None
    step_number: builtins.int | None
    creatives_processed: builtins.int | None
    creatives_total: builtins.int | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        percentage: builtins.float = ...,
        current_step: builtins.str = ...,
        total_steps: builtins.int = ...,
        step_number: builtins.int = ...,
        creatives_processed: builtins.int = ...,
        creatives_total: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncEventSourcesRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    account: _SyncEventSourcesRequestAccountVariant1 | _SyncEventSourcesRequestAccountVariant2
    event_sources: builtins.list[_SyncEventSourcesRequestEventSourcesItem] | None
    delete_missing: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        account: _SyncEventSourcesRequestAccountVariant1 | _SyncEventSourcesRequestAccountVariant2,
        adcp_major_version: builtins.int = ...,
        event_sources: builtins.list[_SyncEventSourcesRequestEventSourcesItem] = ...,
        delete_missing: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncEventSourcesResponse(VersionedSchemaModel):
    event_sources: builtins.list[_SyncEventSourcesResponseEventSourcesItem] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        event_sources: builtins.list[_SyncEventSourcesResponseEventSourcesItem],
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncGovernanceRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    accounts: builtins.list[_SyncGovernanceRequestAccountsItem]
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        accounts: builtins.list[_SyncGovernanceRequestAccountsItem],
        adcp_major_version: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncGovernanceResponse(VersionedSchemaModel):
    accounts: builtins.list[_SyncGovernanceResponseAccountsItem] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        accounts: builtins.list[_SyncGovernanceResponseAccountsItem],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncPlansRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    idempotency_key: builtins.str
    plans: builtins.list[_SyncPlansRequestPlansItem]
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        idempotency_key: builtins.str,
        plans: builtins.list[_SyncPlansRequestPlansItem],
        adcp_major_version: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class SyncPlansResponse(VersionedSchemaModel):
    plans: builtins.list[_SyncPlansResponsePlansItem]
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        plans: builtins.list[_SyncPlansResponsePlansItem],
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class TasksGetRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    task_id: builtins.str
    include_history: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        task_id: builtins.str,
        adcp_major_version: builtins.int = ...,
        include_history: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class TasksGetResponse(VersionedSchemaModel):
    task_id: builtins.str
    task_type: Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights']
    protocol: Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence']
    status: Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown']
    created_at: builtins.str
    updated_at: builtins.str
    completed_at: builtins.str | None
    has_webhook: builtins.bool | None
    progress: _TasksGetResponseProgress | None
    error: _TasksGetResponseError | None
    history: builtins.list[_TasksGetResponseHistoryItem] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        task_id: builtins.str,
        task_type: Literal['create_media_buy', 'update_media_buy', 'sync_creatives', 'activate_signal', 'get_signals', 'create_property_list', 'update_property_list', 'get_property_list', 'list_property_lists', 'delete_property_list', 'sync_accounts', 'get_account_financials', 'get_creative_delivery', 'sync_event_sources', 'sync_audiences', 'sync_catalogs', 'log_event', 'get_brand_identity', 'get_rights', 'acquire_rights'],
        protocol: Literal['media-buy', 'signals', 'governance', 'creative', 'brand', 'sponsored-intelligence'],
        status: Literal['submitted', 'working', 'input-required', 'completed', 'canceled', 'failed', 'rejected', 'auth-required', 'unknown'],
        created_at: builtins.str,
        updated_at: builtins.str,
        completed_at: builtins.str = ...,
        has_webhook: builtins.bool = ...,
        progress: _TasksGetResponseProgress = ...,
        error: _TasksGetResponseError = ...,
        history: builtins.list[_TasksGetResponseHistoryItem] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class TasksListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    filters: _TasksListRequestFilters | None
    sort: _TasksListRequestSort | None
    pagination: _TasksListRequestPagination | None
    include_history: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        adcp_major_version: builtins.int = ...,
        filters: _TasksListRequestFilters = ...,
        sort: _TasksListRequestSort = ...,
        pagination: _TasksListRequestPagination = ...,
        include_history: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class TasksListResponse(VersionedSchemaModel):
    query_summary: _TasksListResponseQuerySummary
    tasks: builtins.list[_TasksListResponseTasksItem]
    pagination: _TasksListResponsePagination
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        query_summary: _TasksListResponseQuerySummary,
        tasks: builtins.list[_TasksListResponseTasksItem],
        pagination: _TasksListResponsePagination,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateCollectionListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    list_id: builtins.str
    account: _UpdateCollectionListRequestAccountVariant1 | _UpdateCollectionListRequestAccountVariant2 | None
    name: builtins.str | None
    description: builtins.str | None
    base_collections: builtins.list[_UpdateCollectionListRequestBaseCollectionsItemVariant1 | _UpdateCollectionListRequestBaseCollectionsItemVariant2 | _UpdateCollectionListRequestBaseCollectionsItemVariant3] | None
    filters: _ExternalCollectionCollectionListFilters | None
    brand: _ExternalCoreBrandRef | None
    webhook_url: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    idempotency_key: builtins.str

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _UpdateCollectionListRequestAccountVariant1 | _UpdateCollectionListRequestAccountVariant2 = ...,
        name: builtins.str = ...,
        description: builtins.str = ...,
        base_collections: builtins.list[_UpdateCollectionListRequestBaseCollectionsItemVariant1 | _UpdateCollectionListRequestBaseCollectionsItemVariant2 | _UpdateCollectionListRequestBaseCollectionsItemVariant3] = ...,
        filters: _ExternalCollectionCollectionListFilters = ...,
        brand: _ExternalCoreBrandRef = ...,
        webhook_url: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateCollectionListResponse(VersionedSchemaModel):
    list: _ExternalCollectionCollectionList
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list: _ExternalCollectionCollectionList,
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateContentStandardsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    standards_id: builtins.str
    scope: _UpdateContentStandardsRequestScope | None
    registry_policy_ids: builtins.list[builtins.str] | None
    policies: builtins.list[_ExternalGovernancePolicyEntry] | None
    calibration_exemplars: _UpdateContentStandardsRequestCalibrationExemplars | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    idempotency_key: builtins.str

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        standards_id: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        scope: _UpdateContentStandardsRequestScope = ...,
        registry_policy_ids: builtins.list[builtins.str] = ...,
        policies: builtins.list[_ExternalGovernancePolicyEntry] = ...,
        calibration_exemplars: _UpdateContentStandardsRequestCalibrationExemplars = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateContentStandardsResponse(VersionedSchemaModel):
    success: Literal[True] | Literal[False]
    standards_id: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None
    conflicting_standards_id: builtins.str | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[True],
        standards_id: builtins.str,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        success: Literal[False],
        errors: builtins.list[_ExternalCoreError],
        conflicting_standards_id: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateMediaBuyInputRequiredResponse(VersionedSchemaModel):
    reason: Literal['APPROVAL_REQUIRED', 'CHANGE_CONFIRMATION'] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        reason: Literal['APPROVAL_REQUIRED', 'CHANGE_CONFIRMATION'] = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateMediaBuyRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    account: _UpdateMediaBuyRequestAccountVariant1 | _UpdateMediaBuyRequestAccountVariant2
    media_buy_id: builtins.str
    revision: builtins.int | None
    paused: builtins.bool | None
    canceled: Literal[True] | None
    cancellation_reason: builtins.str | None
    start_time: Literal['asap'] | builtins.str | None
    end_time: builtins.str | None
    packages: builtins.list[_ExternalMediaBuyPackageUpdate] | None
    invoice_recipient: _ExternalCoreBusinessEntity | None
    new_packages: builtins.list[_ExternalMediaBuyPackageRequest] | None
    reporting_webhook: _ExternalCoreReportingWebhook | None
    push_notification_config: _ExternalCorePushNotificationConfig | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        account: _UpdateMediaBuyRequestAccountVariant1 | _UpdateMediaBuyRequestAccountVariant2,
        media_buy_id: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        revision: builtins.int = ...,
        paused: builtins.bool = ...,
        canceled: Literal[True] = ...,
        cancellation_reason: builtins.str = ...,
        start_time: Literal['asap'] | builtins.str = ...,
        end_time: builtins.str = ...,
        packages: builtins.list[_ExternalMediaBuyPackageUpdate] = ...,
        invoice_recipient: _ExternalCoreBusinessEntity = ...,
        new_packages: builtins.list[_ExternalMediaBuyPackageRequest] = ...,
        reporting_webhook: _ExternalCoreReportingWebhook = ...,
        push_notification_config: _ExternalCorePushNotificationConfig = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateMediaBuySubmittedResponse(VersionedSchemaModel):
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateMediaBuyResponse(VersionedSchemaModel):
    media_buy_id: builtins.str | None
    status: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] | None
    revision: builtins.int | None
    implementation_date: builtins.str | None
    invoice_recipient: _ExternalCoreBusinessEntity | None
    affected_packages: builtins.list[_ExternalCorePackage] | None
    valid_actions: builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] | None
    sandbox: builtins.bool | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        media_buy_id: builtins.str,
        status: Literal['pending_creatives', 'pending_start', 'active', 'paused', 'completed', 'rejected', 'canceled'] = ...,
        revision: builtins.int = ...,
        implementation_date: builtins.str | None = ...,
        invoice_recipient: _ExternalCoreBusinessEntity = ...,
        affected_packages: builtins.list[_ExternalCorePackage] = ...,
        valid_actions: builtins.list[Literal['pause', 'resume', 'cancel', 'update_budget', 'update_dates', 'update_packages', 'add_packages', 'sync_creatives']] = ...,
        sandbox: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateMediaBuyWorkingResponse(VersionedSchemaModel):
    percentage: builtins.float | None
    current_step: builtins.str | None
    total_steps: builtins.int | None
    step_number: builtins.int | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        percentage: builtins.float = ...,
        current_step: builtins.str = ...,
        total_steps: builtins.int = ...,
        step_number: builtins.int = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdatePropertyListRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    list_id: builtins.str
    account: _UpdatePropertyListRequestAccountVariant1 | _UpdatePropertyListRequestAccountVariant2 | None
    name: builtins.str | None
    description: builtins.str | None
    base_properties: builtins.list[_UpdatePropertyListRequestBasePropertiesItemVariant1 | _UpdatePropertyListRequestBasePropertiesItemVariant2 | _UpdatePropertyListRequestBasePropertiesItemVariant3] | None
    filters: _ExternalPropertyPropertyListFilters | None
    brand: _ExternalCoreBrandRef | None
    webhook_url: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    idempotency_key: builtins.str

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        account: _UpdatePropertyListRequestAccountVariant1 | _UpdatePropertyListRequestAccountVariant2 = ...,
        name: builtins.str = ...,
        description: builtins.str = ...,
        base_properties: builtins.list[_UpdatePropertyListRequestBasePropertiesItemVariant1 | _UpdatePropertyListRequestBasePropertiesItemVariant2 | _UpdatePropertyListRequestBasePropertiesItemVariant3] = ...,
        filters: _ExternalPropertyPropertyListFilters = ...,
        brand: _ExternalCoreBrandRef = ...,
        webhook_url: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdatePropertyListResponse(VersionedSchemaModel):
    list: _ExternalPropertyPropertyList
    replayed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list: _ExternalPropertyPropertyList,
        replayed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateRightsRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    rights_id: builtins.str
    end_date: builtins.str | None
    impression_cap: builtins.int | None
    pricing_option_id: builtins.str | None
    paused: builtins.bool | None
    push_notification_config: _ExternalCorePushNotificationConfig | None
    idempotency_key: builtins.str
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights_id: builtins.str,
        idempotency_key: builtins.str,
        adcp_major_version: builtins.int = ...,
        end_date: builtins.str = ...,
        impression_cap: builtins.int = ...,
        pricing_option_id: builtins.str = ...,
        paused: builtins.bool = ...,
        push_notification_config: _ExternalCorePushNotificationConfig = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class UpdateRightsResponse(VersionedSchemaModel):
    rights_id: builtins.str | None
    terms: _ExternalBrandRightsTerms | None
    generation_credentials: builtins.list[_ExternalCoreGenerationCredential] | None
    rights_constraint: _ExternalCoreRightsConstraint | None
    paused: builtins.bool | None
    implementation_date: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        rights_id: builtins.str,
        terms: _ExternalBrandRightsTerms,
        generation_credentials: builtins.list[_ExternalCoreGenerationCredential] = ...,
        rights_constraint: _ExternalCoreRightsConstraint = ...,
        paused: builtins.bool = ...,
        implementation_date: builtins.str | None = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ValidateContentDeliveryRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    standards_id: builtins.str
    records: builtins.list[_ValidateContentDeliveryRequestRecordsItem]
    feature_ids: builtins.list[builtins.str] | None
    include_passed: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        standards_id: builtins.str,
        records: builtins.list[_ValidateContentDeliveryRequestRecordsItem],
        adcp_major_version: builtins.int = ...,
        feature_ids: builtins.list[builtins.str] = ...,
        include_passed: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ValidateContentDeliveryResponse(VersionedSchemaModel):
    summary: _ValidateContentDeliveryResponseSummary | None
    results: builtins.list[_ValidateContentDeliveryResponseResultsItem] | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None
    errors: builtins.list[_ExternalCoreError] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        summary: _ValidateContentDeliveryResponseSummary,
        results: builtins.list[_ValidateContentDeliveryResponseResultsItem],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        errors: builtins.list[_ExternalCoreError],
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ValidatePropertyDeliveryRequest(VersionedSchemaModel):
    adcp_major_version: builtins.int | None
    list_id: builtins.str
    account: _ValidatePropertyDeliveryRequestAccountVariant1 | _ValidatePropertyDeliveryRequestAccountVariant2 | None
    records: builtins.list[_ExternalPropertyDeliveryRecord]
    include_compliant: builtins.bool
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        records: builtins.list[_ExternalPropertyDeliveryRecord],
        adcp_major_version: builtins.int = ...,
        account: _ValidatePropertyDeliveryRequestAccountVariant1 | _ValidatePropertyDeliveryRequestAccountVariant2 = ...,
        include_compliant: builtins.bool = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

class ValidatePropertyDeliveryResponse(VersionedSchemaModel):
    compliant: builtins.bool | None
    list_id: builtins.str
    summary: _ValidatePropertyDeliveryResponseSummary
    aggregate: _ValidatePropertyDeliveryResponseAggregate | None
    authorization_summary: _ValidatePropertyDeliveryResponseAuthorizationSummary | None
    results: builtins.list[_ExternalPropertyValidationResult]
    validated_at: builtins.str
    list_resolved_at: builtins.str | None
    context: builtins.dict[builtins.str, Any] | None
    ext: builtins.dict[builtins.str, Any] | None

    @overload
    def __init__(self, root: builtins.dict[builtins.str, Any], /) -> None: ...

    @overload
    def __init__(
        self,
        *,
        list_id: builtins.str,
        summary: _ValidatePropertyDeliveryResponseSummary,
        results: builtins.list[_ExternalPropertyValidationResult],
        validated_at: builtins.str,
        compliant: builtins.bool = ...,
        aggregate: _ValidatePropertyDeliveryResponseAggregate = ...,
        authorization_summary: _ValidatePropertyDeliveryResponseAuthorizationSummary = ...,
        list_resolved_at: builtins.str = ...,
        context: builtins.dict[builtins.str, Any] = ...,
        ext: builtins.dict[builtins.str, Any] = ...,
    ) -> None: ...

__all__ = ['AcquireRightsRequest', 'AcquireRightsResponse', 'ActivateSignalRequest', 'ActivateSignalResponse', 'BuildCreativeInputRequiredResponse', 'BuildCreativeRequest', 'BuildCreativeSubmittedResponse', 'BuildCreativeResponse', 'BuildCreativeWorkingResponse', 'CalibrateContentRequest', 'CalibrateContentResponse', 'CheckGovernanceRequest', 'CheckGovernanceResponse', 'ComplyTestControllerRequest', 'ComplyTestControllerResponse', 'ContextMatchRequest', 'ContextMatchResponse', 'CreateCollectionListRequest', 'CreateCollectionListResponse', 'CreateContentStandardsRequest', 'CreateContentStandardsResponse', 'CreateMediaBuyInputRequiredResponse', 'CreateMediaBuyRequest', 'CreateMediaBuySubmittedResponse', 'CreateMediaBuyResponse', 'CreateMediaBuyWorkingResponse', 'CreatePropertyListRequest', 'CreatePropertyListResponse', 'CreativeApprovalRequest', 'CreativeApprovalResponse', 'DeleteCollectionListRequest', 'DeleteCollectionListResponse', 'DeletePropertyListRequest', 'DeletePropertyListResponse', 'GetAccountFinancialsRequest', 'GetAccountFinancialsResponse', 'GetAdcpCapabilitiesRequest', 'GetAdcpCapabilitiesResponse', 'GetBrandIdentityRequest', 'GetBrandIdentityResponse', 'GetCollectionListRequest', 'GetCollectionListResponse', 'GetContentStandardsRequest', 'GetContentStandardsResponse', 'GetCreativeDeliveryRequest', 'GetCreativeDeliveryResponse', 'GetCreativeFeaturesRequest', 'GetCreativeFeaturesResponse', 'GetMediaBuyArtifactsRequest', 'GetMediaBuyArtifactsResponse', 'GetMediaBuyDeliveryRequest', 'GetMediaBuyDeliveryResponse', 'GetMediaBuysRequest', 'GetMediaBuysResponse', 'GetPlanAuditLogsRequest', 'GetPlanAuditLogsResponse', 'GetProductsInputRequiredResponse', 'GetProductsRequest', 'GetProductsSubmittedResponse', 'GetProductsResponse', 'GetProductsWorkingResponse', 'GetPropertyListRequest', 'GetPropertyListResponse', 'GetRightsRequest', 'GetRightsResponse', 'GetSignalsRequest', 'GetSignalsResponse', 'IdentityMatchRequest', 'IdentityMatchResponse', 'ListAccountsRequest', 'ListAccountsResponse', 'ListCollectionListsRequest', 'ListCollectionListsResponse', 'ListContentStandardsRequest', 'ListContentStandardsResponse', 'ListCreativeFormatsRequest', 'ListCreativeFormatsResponse', 'ListCreativesRequest', 'ListCreativesResponse', 'ListPropertyListsRequest', 'ListPropertyListsResponse', 'LogEventRequest', 'LogEventResponse', 'PackageRequest', 'PreviewCreativeRequest', 'PreviewCreativeResponse', 'ProvidePerformanceFeedbackRequest', 'ProvidePerformanceFeedbackResponse', 'ReportPlanOutcomeRequest', 'ReportPlanOutcomeResponse', 'ReportUsageRequest', 'ReportUsageResponse', 'SiGetOfferingRequest', 'SiGetOfferingResponse', 'SiInitiateSessionRequest', 'SiInitiateSessionResponse', 'SiSendMessageRequest', 'SiSendMessageResponse', 'SiTerminateSessionRequest', 'SiTerminateSessionResponse', 'SyncAccountsRequest', 'SyncAccountsResponse', 'SyncAudiencesRequest', 'SyncAudiencesResponse', 'SyncCatalogsInputRequiredResponse', 'SyncCatalogsRequest', 'SyncCatalogsSubmittedResponse', 'SyncCatalogsResponse', 'SyncCatalogsWorkingResponse', 'SyncCreativesInputRequiredResponse', 'SyncCreativesRequest', 'SyncCreativesSubmittedResponse', 'SyncCreativesResponse', 'SyncCreativesWorkingResponse', 'SyncEventSourcesRequest', 'SyncEventSourcesResponse', 'SyncGovernanceRequest', 'SyncGovernanceResponse', 'SyncPlansRequest', 'SyncPlansResponse', 'TasksGetRequest', 'TasksGetResponse', 'TasksListRequest', 'TasksListResponse', 'UpdateCollectionListRequest', 'UpdateCollectionListResponse', 'UpdateContentStandardsRequest', 'UpdateContentStandardsResponse', 'UpdateMediaBuyInputRequiredResponse', 'UpdateMediaBuyRequest', 'UpdateMediaBuySubmittedResponse', 'UpdateMediaBuyResponse', 'UpdateMediaBuyWorkingResponse', 'UpdatePropertyListRequest', 'UpdatePropertyListResponse', 'UpdateRightsRequest', 'UpdateRightsResponse', 'ValidateContentDeliveryRequest', 'ValidateContentDeliveryResponse', 'ValidatePropertyDeliveryRequest', 'ValidatePropertyDeliveryResponse']
