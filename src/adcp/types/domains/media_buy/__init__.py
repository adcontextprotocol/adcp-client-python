"""Types the AdCP ``media_buy`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.media_buy import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.media_buy.<schema>``.
Nothing here is renamed.

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-04 01:19:01 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.media_buy.accept_proposal_async_response_input_required import (
    AcceptProposalInputRequired,
)
from adcp.types.generated_poc.media_buy.accept_proposal_async_response_submitted import (
    AcceptProposalSubmitted,
)
from adcp.types.generated_poc.media_buy.accept_proposal_async_response_working import (
    AcceptProposalWorking,
)
from adcp.types.generated_poc.media_buy.accept_proposal_request import AcceptProposalRequest
from adcp.types.generated_poc.media_buy.accept_proposal_response import (
    AcceptProposalResponse,
    AcceptProposalResponse1,
    AcceptProposalResponse2,
    AcceptProposalResponse3,
    AcceptProposalResponse4,
    AcceptProposalResponse5,
    AcceptProposalResponse6,
    AcceptProposalResponse7,
)
from adcp.types.generated_poc.media_buy.acceptance_context import (
    AcceptanceContext,
    AdvertiserJurisdiction,
    DeliveryJurisdiction,
    Subject,
)
from adcp.types.generated_poc.media_buy.acceptance_policy_catalog import AcceptancePolicyCatalog
from adcp.types.generated_poc.media_buy.acceptance_policy_profile import (
    AcceptancePolicyProfile,
    Coverage,
    PolicyRef,
    RegionAliase,
    SubjectCategory,
)
from adcp.types.generated_poc.media_buy.acceptance_policy_profile_ref import (
    RegistryAcceptancePolicyProfileReference,
)
from adcp.types.generated_poc.media_buy.acceptance_policy_requirement import (
    AcceptancePolicyRequirement,
    AcceptancePolicyRequirement1,
    AcceptancePolicyRequirement10,
    AcceptancePolicyRequirement11,
    AcceptancePolicyRequirement12,
    AcceptancePolicyRequirement13,
    AcceptancePolicyRequirement14,
    AcceptancePolicyRequirement15,
    AcceptancePolicyRequirement16,
    AcceptancePolicyRequirement17,
    AcceptancePolicyRequirement2,
    AcceptancePolicyRequirement3,
    AcceptancePolicyRequirement4,
    AcceptancePolicyRequirement5,
    AcceptancePolicyRequirement6,
    AcceptancePolicyRequirement7,
    AcceptancePolicyRequirement8,
    AcceptancePolicyRequirement9,
    Criterion,
    FormatId,
)
from adcp.types.generated_poc.media_buy.acceptance_policy_rule import (
    AcceptancePolicyRule,
    Disposition,
)
from adcp.types.generated_poc.media_buy.build_creative_async_response_input_required import (
    BuildCreativeInputRequired,
)
from adcp.types.generated_poc.media_buy.build_creative_async_response_submitted import (
    BuildCreativeSubmitted,
)
from adcp.types.generated_poc.media_buy.build_creative_async_response_working import (
    BuildCreativeWorking,
)
from adcp.types.generated_poc.media_buy.build_creative_request import (
    BuildCreativeRequest,
    Dimension,
    KeepMode,
    MaxSpend,
    Mode,
    PreviewInput,
    SignalCondition,
    SignalCondition1,
    SignalCondition2,
    SignalCondition3,
    SignalCondition4,
    SignalCondition5,
    SignalCondition6,
    SignalCondition7,
    TargetCapabilityId,
    VariantAxis,
)
from adcp.types.generated_poc.media_buy.build_creative_response import (
    BuildCreativeResponse,
    BuildCreativeResponse1,
    BuildCreativeResponse2,
    BuildCreativeResponse3,
    BuildCreativeResponse4,
    BuildCreativeResponse5,
    BuildCreativeResponse6,
    CatalogItemRef,
    Estimate,
    Eval,
    Input,
    Input2,
    PerLeaf,
    Preview,
    Preview2,
    Preview3,
    Preview4,
    Variant,
)
from adcp.types.generated_poc.media_buy.buy_products_async_response_input_required import (
    BuyProductsInputRequired,
)
from adcp.types.generated_poc.media_buy.buy_products_async_response_submitted import (
    BuyProductsSubmitted,
)
from adcp.types.generated_poc.media_buy.buy_products_async_response_working import (
    BuyProductsWorking,
)
from adcp.types.generated_poc.media_buy.buy_products_request import BuyProductsRequest
from adcp.types.generated_poc.media_buy.buy_products_response import (
    BuyProductsResponse,
    BuyProductsResponse1,
    BuyProductsResponse2,
    BuyProductsResponse3,
    BuyProductsResponse4,
    BuyProductsResponse5,
    BuyProductsResponse6,
    BuyProductsResponse7,
)
from adcp.types.generated_poc.media_buy.change_term import (
    AllowedStatus,
    Condition,
    MediaBuyChangeTerm,
)
from adcp.types.generated_poc.media_buy.change_term_constraints import (
    MediaBuyChangeTermConstraints,
    MediaBuyChangeTermConstraints1,
    MediaBuyChangeTermConstraints2,
    MediaBuyChangeTermConstraints3,
    MediaBuyChangeTermConstraints4,
    Money,
)
from adcp.types.generated_poc.media_buy.commercial_terms import (
    CancellationTerms,
    CommercialTerms,
    Fee,
    ReportingCommitment,
)
from adcp.types.generated_poc.media_buy.control_media_buy_async_response_input_required import (
    ControlMediaBuyInputRequired,
)
from adcp.types.generated_poc.media_buy.control_media_buy_async_response_submitted import (
    ControlMediaBuySubmitted,
)
from adcp.types.generated_poc.media_buy.control_media_buy_async_response_working import (
    ControlMediaBuyWorking,
)
from adcp.types.generated_poc.media_buy.control_media_buy_request import ControlMediaBuyRequest
from adcp.types.generated_poc.media_buy.control_media_buy_response import (
    AffectedPackageId,
    ControlMediaBuyResponse,
    ControlMediaBuyResponse1,
    ControlMediaBuyResponse2,
    ControlMediaBuyResponse3,
)
from adcp.types.generated_poc.media_buy.create_media_buy_async_response_input_required import (
    CreateMediaBuyInputRequired,
)
from adcp.types.generated_poc.media_buy.create_media_buy_async_response_submitted import (
    CreateMediaBuySubmitted,
)
from adcp.types.generated_poc.media_buy.create_media_buy_async_response_working import (
    CreateMediaBuyWorking,
)
from adcp.types.generated_poc.media_buy.create_media_buy_request import (
    ArtifactWebhook,
    Authentication,
    BatchFrequency,
    CreateMediaBuyRequest,
    DeliveryMode,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse,
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
    CreateMediaBuyResponse3,
)
from adcp.types.generated_poc.media_buy.decline_proposals_async_response_input_required import (
    DeclineProposalsInputRequired,
)
from adcp.types.generated_poc.media_buy.decline_proposals_async_response_submitted import (
    DeclineProposalsSubmitted,
)
from adcp.types.generated_poc.media_buy.decline_proposals_async_response_working import (
    DeclineProposalsWorking,
)
from adcp.types.generated_poc.media_buy.decline_proposals_request import DeclineProposalsRequest
from adcp.types.generated_poc.media_buy.decline_proposals_response import (
    DeclineProposalsResponse,
    DeclineProposalsResponse1,
    DeclineProposalsResponse2,
    Results1,
    Results2,
    Results3,
    Results4,
    Results5,
)
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_request import (
    AttributionWindow,
    CatalogItem,
    Demographic,
    DevicePlatform,
    DeviceType,
    Format,
    Geo,
    GetMediaBuyDeliveryRequest,
    Keyword,
    Placement,
    ReportingDimensions,
    Spot,
)
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
    AggregatedTotals,
    ByAudienceItem,
    ByDemographicItem,
    ByDevicePlatformItem,
    ByDeviceTypeItem,
    ByFormatItem,
    ByPackageItem1,
    BySpotItem,
    DailyBreakdownItem,
    DailyBreakdownItem1,
    GetMediaBuyDeliveryResponse,
    ReportingRevisionBinding,
    Window,
)
from adcp.types.generated_poc.media_buy.get_media_buys_request import GetMediaBuysRequest
from adcp.types.generated_poc.media_buy.get_media_buys_response import (
    Cancellation,
    Cancellation1,
    CreativeApproval,
    GetMediaBuysResponse,
    HistoryItem,
    Indicator,
    Indicator1,
    Indicator2,
    IndicatorTypesEvaluatedEnum,
    IndicatorTypesEvaluatedEnum1,
    IndicatorTypesEvaluatedEnum2,
    MediaBuy,
    Package,
    Snapshot,
)
from adcp.types.generated_poc.media_buy.get_products_async_response_input_required import (
    GetProductsInputRequired,
)
from adcp.types.generated_poc.media_buy.get_products_async_response_submitted import (
    GetProductsSubmitted,
)
from adcp.types.generated_poc.media_buy.get_products_async_response_working import (
    GetProductsWorking,
)
from adcp.types.generated_poc.media_buy.get_products_rejected import GetProductsRejected
from adcp.types.generated_poc.media_buy.get_products_request import (
    Action9,
    BuyingMode,
    Field1,
    Fields,
    GetProductsRequest,
    Refine,
    Refine1,
    Refine2,
    Refine3,
)
from adcp.types.generated_poc.media_buy.get_products_response import (
    ExcludedBy,
    Extensions,
    FilterDiagnostics,
    GetProductsResponse,
    RefinementApplied,
    RefinementApplied1,
    RefinementApplied2,
    RefinementApplied3,
    Semantics,
)
from adcp.types.generated_poc.media_buy.get_products_targeting_resolution import (
    ProductDiscoveryTargetingResolution,
)
from adcp.types.generated_poc.media_buy.get_reporting_status_request import (
    DeliveryConfigId,
    GetReportingStatusRequest,
    Period,
    ReportingStatusView,
)
from adcp.types.generated_poc.media_buy.get_reporting_status_response import (
    DeliveryConfigGeneration,
    GetReportingStatusResponse,
    ObligationCounts,
)
from adcp.types.generated_poc.media_buy.legacy_purchase_continuation_input import (
    AcceptedLoss,
    CompatibilityPurchaseCoordinatorInput,
    SelectedProductId,
)
from adcp.types.generated_poc.media_buy.list_creative_formats_request import (
    ListCreativeFormatsRequest,
)
from adcp.types.generated_poc.media_buy.list_creative_formats_response import (
    CreativeAgent,
    ListCreativeFormatsResponse,
)
from adcp.types.generated_poc.media_buy.list_products_request import ListProductsRequest
from adcp.types.generated_poc.media_buy.list_products_response import (
    IncompleteItem3,
    ListProductsResponse,
    ListProductsResponse1,
    ListProductsResponse2,
)
from adcp.types.generated_poc.media_buy.log_event_request import LogEventRequest
from adcp.types.generated_poc.media_buy.log_event_response import (
    LogEventResponse,
    LogEventResponse1,
    LogEventResponse2,
    PartialFailure,
)
from adcp.types.generated_poc.media_buy.media_buy_commitment_response import (
    MediaBuyCommitmentResponse,
    MediaBuyCommitmentResponse1,
    MediaBuyCommitmentResponse2,
    MediaBuyCommitmentResponse3,
)
from adcp.types.generated_poc.media_buy.media_buy_delivery_webhook_result import (
    MediaBuyDeliveryWebhookResult,
)
from adcp.types.generated_poc.media_buy.outcome_target import Goal, Goal1, OutcomeTarget
from adcp.types.generated_poc.media_buy.package_control import PackageControl
from adcp.types.generated_poc.media_buy.package_request import (
    CommittedMetrics,
    CommittedMetrics1,
    CommittedMetrics2,
    PackageRequest,
    Qualifier,
)
from adcp.types.generated_poc.media_buy.package_update import (
    KeywordTargetsAddItem,
    KeywordTargetsRemoveItem,
    NegativeKeywordsAddItem,
    NegativeKeywordsRemoveItem,
    PackageUpdate,
)
from adcp.types.generated_poc.media_buy.product_discovery_criteria import ProductDiscoveryCriteria
from adcp.types.generated_poc.media_buy.product_fields import (
    ProductResponseField,
    ProductResponseFields,
)
from adcp.types.generated_poc.media_buy.product_purchase import ProductPurchase
from adcp.types.generated_poc.media_buy.product_purchase_input import ProductPurchaseInput
from adcp.types.generated_poc.media_buy.product_refinement import (
    Action11,
    ProductRefinementRequests,
    ProductRefinementRequests1,
    ProductRefinementRequests2,
    ProductRefinementRequests3,
    ProductRefinementRequests4,
)
from adcp.types.generated_poc.media_buy.proposal_budget_constraint import ProposalBudgetConstraint
from adcp.types.generated_poc.media_buy.proposal_decline import ProposalDecline
from adcp.types.generated_poc.media_buy.proposal_refinement import (
    Alternatives,
    ChangeKind,
    Constraints,
    Constraints1,
    Constraints2,
    Constraints3,
    Constraints4,
    Constraints5,
    Constraints6,
    Constraints7,
    Cpm,
    Flight,
    Impressions,
    ProposalRefinement,
    ProposalRefinement2,
    ProposalRefinement3,
    ProposalRefinement4,
    ProposalRefinement5,
    ProposalRefinement6,
    ProposalRefinement7,
    ProposalRefinement8,
    ProposalRefinement9,
)
from adcp.types.generated_poc.media_buy.provide_performance_feedback_request import (
    ProvidePerformanceFeedbackRequest,
)
from adcp.types.generated_poc.media_buy.provide_performance_feedback_response import (
    ProvidePerformanceFeedbackResponse,
    ProvidePerformanceFeedbackResponse1,
    ProvidePerformanceFeedbackResponse2,
)
from adcp.types.generated_poc.media_buy.refine_proposals_async_response_input_required import (
    RefineProposalsInputRequired,
)
from adcp.types.generated_poc.media_buy.refine_proposals_async_response_submitted import (
    RefineProposalsSubmitted,
)
from adcp.types.generated_poc.media_buy.refine_proposals_async_response_working import (
    RefineProposalsWorking,
)
from adcp.types.generated_poc.media_buy.refine_proposals_request import (
    RefineProposalsRequest,
    Refinements,
)
from adcp.types.generated_poc.media_buy.refine_proposals_response import (
    Proposal2,
    Proposal3,
    Proposal4,
    Proposal5,
    Proposal6,
    ProposalKind,
    RefineProposalsResponse,
    RefineProposalsResponse1,
    RefineProposalsResponse2,
    Results10,
    Results11,
    Results12,
    Results14,
    Results15,
    Results16,
    Results17,
    Results18,
    Results8,
    Results9,
    TotalBudgetGuidance,
    UnsatisfiedConstraint,
)
from adcp.types.generated_poc.media_buy.request_proposals_async_response_input_required import (
    RequestProposalsInputRequired,
)
from adcp.types.generated_poc.media_buy.request_proposals_async_response_submitted import (
    RequestProposalsSubmitted,
)
from adcp.types.generated_poc.media_buy.request_proposals_async_response_working import (
    RequestProposalsWorking,
)
from adcp.types.generated_poc.media_buy.request_proposals_request import RequestProposalsRequest
from adcp.types.generated_poc.media_buy.request_proposals_response import (
    IncompleteItem5,
    IncompleteItem6,
    IncompleteItem7,
    Loss,
    Losses,
    PurchaseContinuation,
    PurchaseContinuation1,
    PurchaseContinuation2,
    PurchaseContinuation3,
    PurchaseContinuation4,
    PurchaseContinuation5,
    PurchaseContinuation6,
    PurchaseContinuation7,
    RequestProposalsResponse,
    RequestProposalsResponse1,
    RequestProposalsResponse2,
    RequestProposalsResponse3,
    RequestProposalsResponse4,
    SourceAdcpVersion,
)
from adcp.types.generated_poc.media_buy.sync_audiences_request import (
    AudienceType,
    SyncAudiencesRequest,
    Tag,
)
from adcp.types.generated_poc.media_buy.sync_audiences_response import (
    MatchBreakdown,
    SyncAudiencesResponse,
    SyncAudiencesResponse1,
    SyncAudiencesResponse2,
    SyncAudiencesResponse3,
)
from adcp.types.generated_poc.media_buy.sync_catalogs_async_response_input_required import (
    SyncCatalogsInputRequired,
)
from adcp.types.generated_poc.media_buy.sync_catalogs_async_response_submitted import (
    SyncCatalogsSubmitted,
)
from adcp.types.generated_poc.media_buy.sync_catalogs_async_response_working import (
    SyncCatalogsWorking,
)
from adcp.types.generated_poc.media_buy.sync_catalogs_request import SyncCatalogsRequest
from adcp.types.generated_poc.media_buy.sync_catalogs_response import (
    Catalog,
    ItemIssue,
    SyncCatalogsResponse,
    SyncCatalogsResponse1,
    SyncCatalogsResponse2,
    SyncCatalogsResponse3,
)
from adcp.types.generated_poc.media_buy.sync_event_sources_request import (
    SyncEventSourcesRequest,
    ValueCurrency,
)
from adcp.types.generated_poc.media_buy.sync_event_sources_response import (
    Setup,
    SyncEventSourcesResponse,
    SyncEventSourcesResponse1,
    SyncEventSourcesResponse2,
)
from adcp.types.generated_poc.media_buy.sync_reporting_receipts_request import (
    SyncReportingReceiptsRequest,
)
from adcp.types.generated_poc.media_buy.sync_reporting_receipts_response import (
    Results20,
    Results21,
    Results22,
    Results23,
    SyncReportingReceiptsResponse,
)
from adcp.types.generated_poc.media_buy.sync_reporting_status_request import (
    SyncReportingStatusRequest,
)
from adcp.types.generated_poc.media_buy.sync_reporting_status_response import (
    Results26,
    Results27,
    SyncReportingStatusResponse,
)
from adcp.types.generated_poc.media_buy.update_media_buy_async_response_input_required import (
    UpdateMediaBuyInputRequired,
)
from adcp.types.generated_poc.media_buy.update_media_buy_async_response_submitted import (
    UpdateMediaBuySubmitted,
)
from adcp.types.generated_poc.media_buy.update_media_buy_async_response_working import (
    UpdateMediaBuyWorking,
)
from adcp.types.generated_poc.media_buy.update_media_buy_request import UpdateMediaBuyRequest
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse,
    UpdateMediaBuyResponse1,
    UpdateMediaBuyResponse2,
    UpdateMediaBuyResponse3,
)

# Explicit exports
__all__ = [
    "AcceptProposalInputRequired",
    "AcceptProposalRequest",
    "AcceptProposalResponse",
    "AcceptProposalResponse1",
    "AcceptProposalResponse2",
    "AcceptProposalResponse3",
    "AcceptProposalResponse4",
    "AcceptProposalResponse5",
    "AcceptProposalResponse6",
    "AcceptProposalResponse7",
    "AcceptProposalSubmitted",
    "AcceptProposalWorking",
    "AcceptanceContext",
    "AcceptancePolicyCatalog",
    "AcceptancePolicyProfile",
    "AcceptancePolicyRequirement",
    "AcceptancePolicyRequirement1",
    "AcceptancePolicyRequirement10",
    "AcceptancePolicyRequirement11",
    "AcceptancePolicyRequirement12",
    "AcceptancePolicyRequirement13",
    "AcceptancePolicyRequirement14",
    "AcceptancePolicyRequirement15",
    "AcceptancePolicyRequirement16",
    "AcceptancePolicyRequirement17",
    "AcceptancePolicyRequirement2",
    "AcceptancePolicyRequirement3",
    "AcceptancePolicyRequirement4",
    "AcceptancePolicyRequirement5",
    "AcceptancePolicyRequirement6",
    "AcceptancePolicyRequirement7",
    "AcceptancePolicyRequirement8",
    "AcceptancePolicyRequirement9",
    "AcceptancePolicyRule",
    "AcceptedLoss",
    "Action11",
    "Action9",
    "AdvertiserJurisdiction",
    "AffectedPackageId",
    "AggregatedTotals",
    "AllowedStatus",
    "Alternatives",
    "ArtifactWebhook",
    "AttributionWindow",
    "AudienceType",
    "Authentication",
    "BatchFrequency",
    "BuildCreativeInputRequired",
    "BuildCreativeRequest",
    "BuildCreativeResponse",
    "BuildCreativeResponse1",
    "BuildCreativeResponse2",
    "BuildCreativeResponse3",
    "BuildCreativeResponse4",
    "BuildCreativeResponse5",
    "BuildCreativeResponse6",
    "BuildCreativeSubmitted",
    "BuildCreativeWorking",
    "BuyProductsInputRequired",
    "BuyProductsRequest",
    "BuyProductsResponse",
    "BuyProductsResponse1",
    "BuyProductsResponse2",
    "BuyProductsResponse3",
    "BuyProductsResponse4",
    "BuyProductsResponse5",
    "BuyProductsResponse6",
    "BuyProductsResponse7",
    "BuyProductsSubmitted",
    "BuyProductsWorking",
    "BuyingMode",
    "ByAudienceItem",
    "ByDemographicItem",
    "ByDevicePlatformItem",
    "ByDeviceTypeItem",
    "ByFormatItem",
    "ByPackageItem1",
    "BySpotItem",
    "Cancellation",
    "Cancellation1",
    "CancellationTerms",
    "Catalog",
    "CatalogItem",
    "CatalogItemRef",
    "ChangeKind",
    "CommercialTerms",
    "CommittedMetrics",
    "CommittedMetrics1",
    "CommittedMetrics2",
    "CompatibilityPurchaseCoordinatorInput",
    "Condition",
    "Constraints",
    "Constraints1",
    "Constraints2",
    "Constraints3",
    "Constraints4",
    "Constraints5",
    "Constraints6",
    "Constraints7",
    "ControlMediaBuyInputRequired",
    "ControlMediaBuyRequest",
    "ControlMediaBuyResponse",
    "ControlMediaBuyResponse1",
    "ControlMediaBuyResponse2",
    "ControlMediaBuyResponse3",
    "ControlMediaBuySubmitted",
    "ControlMediaBuyWorking",
    "Coverage",
    "Cpm",
    "CreateMediaBuyInputRequired",
    "CreateMediaBuyRequest",
    "CreateMediaBuyResponse",
    "CreateMediaBuyResponse1",
    "CreateMediaBuyResponse2",
    "CreateMediaBuyResponse3",
    "CreateMediaBuySubmitted",
    "CreateMediaBuyWorking",
    "CreativeAgent",
    "CreativeApproval",
    "Criterion",
    "DailyBreakdownItem",
    "DailyBreakdownItem1",
    "DeclineProposalsInputRequired",
    "DeclineProposalsRequest",
    "DeclineProposalsResponse",
    "DeclineProposalsResponse1",
    "DeclineProposalsResponse2",
    "DeclineProposalsSubmitted",
    "DeclineProposalsWorking",
    "DeliveryConfigGeneration",
    "DeliveryConfigId",
    "DeliveryJurisdiction",
    "DeliveryMode",
    "Demographic",
    "DevicePlatform",
    "DeviceType",
    "Dimension",
    "Disposition",
    "Estimate",
    "Eval",
    "ExcludedBy",
    "Extensions",
    "Fee",
    "Field1",
    "Fields",
    "FilterDiagnostics",
    "Flight",
    "Format",
    "FormatId",
    "Geo",
    "GetMediaBuyDeliveryRequest",
    "GetMediaBuyDeliveryResponse",
    "GetMediaBuysRequest",
    "GetMediaBuysResponse",
    "GetProductsInputRequired",
    "GetProductsRejected",
    "GetProductsRequest",
    "GetProductsResponse",
    "GetProductsSubmitted",
    "GetProductsWorking",
    "GetReportingStatusRequest",
    "GetReportingStatusResponse",
    "Goal",
    "Goal1",
    "HistoryItem",
    "Impressions",
    "IncompleteItem3",
    "IncompleteItem5",
    "IncompleteItem6",
    "IncompleteItem7",
    "Indicator",
    "Indicator1",
    "Indicator2",
    "IndicatorTypesEvaluatedEnum",
    "IndicatorTypesEvaluatedEnum1",
    "IndicatorTypesEvaluatedEnum2",
    "Input",
    "Input2",
    "ItemIssue",
    "KeepMode",
    "Keyword",
    "KeywordTargetsAddItem",
    "KeywordTargetsRemoveItem",
    "ListCreativeFormatsRequest",
    "ListCreativeFormatsResponse",
    "ListProductsRequest",
    "ListProductsResponse",
    "ListProductsResponse1",
    "ListProductsResponse2",
    "LogEventRequest",
    "LogEventResponse",
    "LogEventResponse1",
    "LogEventResponse2",
    "Loss",
    "Losses",
    "MatchBreakdown",
    "MaxSpend",
    "MediaBuy",
    "MediaBuyChangeTerm",
    "MediaBuyChangeTermConstraints",
    "MediaBuyChangeTermConstraints1",
    "MediaBuyChangeTermConstraints2",
    "MediaBuyChangeTermConstraints3",
    "MediaBuyChangeTermConstraints4",
    "MediaBuyCommitmentResponse",
    "MediaBuyCommitmentResponse1",
    "MediaBuyCommitmentResponse2",
    "MediaBuyCommitmentResponse3",
    "MediaBuyDeliveryWebhookResult",
    "Mode",
    "Money",
    "NegativeKeywordsAddItem",
    "NegativeKeywordsRemoveItem",
    "ObligationCounts",
    "OutcomeTarget",
    "Package",
    "PackageControl",
    "PackageRequest",
    "PackageUpdate",
    "PartialFailure",
    "PerLeaf",
    "Period",
    "Placement",
    "PolicyRef",
    "Preview",
    "Preview2",
    "Preview3",
    "Preview4",
    "PreviewInput",
    "ProductDiscoveryCriteria",
    "ProductDiscoveryTargetingResolution",
    "ProductPurchase",
    "ProductPurchaseInput",
    "ProductRefinementRequests",
    "ProductRefinementRequests1",
    "ProductRefinementRequests2",
    "ProductRefinementRequests3",
    "ProductRefinementRequests4",
    "ProductResponseField",
    "ProductResponseFields",
    "Proposal2",
    "Proposal3",
    "Proposal4",
    "Proposal5",
    "Proposal6",
    "ProposalBudgetConstraint",
    "ProposalDecline",
    "ProposalKind",
    "ProposalRefinement",
    "ProposalRefinement2",
    "ProposalRefinement3",
    "ProposalRefinement4",
    "ProposalRefinement5",
    "ProposalRefinement6",
    "ProposalRefinement7",
    "ProposalRefinement8",
    "ProposalRefinement9",
    "ProvidePerformanceFeedbackRequest",
    "ProvidePerformanceFeedbackResponse",
    "ProvidePerformanceFeedbackResponse1",
    "ProvidePerformanceFeedbackResponse2",
    "PurchaseContinuation",
    "PurchaseContinuation1",
    "PurchaseContinuation2",
    "PurchaseContinuation3",
    "PurchaseContinuation4",
    "PurchaseContinuation5",
    "PurchaseContinuation6",
    "PurchaseContinuation7",
    "Qualifier",
    "Refine",
    "Refine1",
    "Refine2",
    "Refine3",
    "RefineProposalsInputRequired",
    "RefineProposalsRequest",
    "RefineProposalsResponse",
    "RefineProposalsResponse1",
    "RefineProposalsResponse2",
    "RefineProposalsSubmitted",
    "RefineProposalsWorking",
    "RefinementApplied",
    "RefinementApplied1",
    "RefinementApplied2",
    "RefinementApplied3",
    "Refinements",
    "RegionAliase",
    "RegistryAcceptancePolicyProfileReference",
    "ReportingCommitment",
    "ReportingDimensions",
    "ReportingRevisionBinding",
    "ReportingStatusView",
    "RequestProposalsInputRequired",
    "RequestProposalsRequest",
    "RequestProposalsResponse",
    "RequestProposalsResponse1",
    "RequestProposalsResponse2",
    "RequestProposalsResponse3",
    "RequestProposalsResponse4",
    "RequestProposalsSubmitted",
    "RequestProposalsWorking",
    "Results1",
    "Results10",
    "Results11",
    "Results12",
    "Results14",
    "Results15",
    "Results16",
    "Results17",
    "Results18",
    "Results2",
    "Results20",
    "Results21",
    "Results22",
    "Results23",
    "Results26",
    "Results27",
    "Results3",
    "Results4",
    "Results5",
    "Results8",
    "Results9",
    "SelectedProductId",
    "Semantics",
    "Setup",
    "SignalCondition",
    "SignalCondition1",
    "SignalCondition2",
    "SignalCondition3",
    "SignalCondition4",
    "SignalCondition5",
    "SignalCondition6",
    "SignalCondition7",
    "Snapshot",
    "SourceAdcpVersion",
    "Spot",
    "Subject",
    "SubjectCategory",
    "SyncAudiencesRequest",
    "SyncAudiencesResponse",
    "SyncAudiencesResponse1",
    "SyncAudiencesResponse2",
    "SyncAudiencesResponse3",
    "SyncCatalogsInputRequired",
    "SyncCatalogsRequest",
    "SyncCatalogsResponse",
    "SyncCatalogsResponse1",
    "SyncCatalogsResponse2",
    "SyncCatalogsResponse3",
    "SyncCatalogsSubmitted",
    "SyncCatalogsWorking",
    "SyncEventSourcesRequest",
    "SyncEventSourcesResponse",
    "SyncEventSourcesResponse1",
    "SyncEventSourcesResponse2",
    "SyncReportingReceiptsRequest",
    "SyncReportingReceiptsResponse",
    "SyncReportingStatusRequest",
    "SyncReportingStatusResponse",
    "Tag",
    "TargetCapabilityId",
    "TotalBudgetGuidance",
    "UnsatisfiedConstraint",
    "UpdateMediaBuyInputRequired",
    "UpdateMediaBuyRequest",
    "UpdateMediaBuyResponse",
    "UpdateMediaBuyResponse1",
    "UpdateMediaBuyResponse2",
    "UpdateMediaBuyResponse3",
    "UpdateMediaBuySubmitted",
    "UpdateMediaBuyWorking",
    "ValueCurrency",
    "Variant",
    "VariantAxis",
    "Window",
]
