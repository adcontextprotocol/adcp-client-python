"""Types the AdCP ``creative`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.creative import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.creative.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:44:03 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.creative.audit_observation import (
        ClaimedValue,
        CreativeAuditObservation,
        Details,
        HumanOversight,
    )
    from adcp.types.domains.creative.creative_assignment_changed_webhook import (
        ChangeKind,
        CreativeAssignmentChangedWebhook,
    )
    from adcp.types.domains.creative.creative_feature_result import CreativeFeatureResult
    from adcp.types.domains.creative.creative_purged_webhook import CreativePurgedWebhook, PurgeKind
    from adcp.types.domains.creative.creative_staged_update_changed_webhook import (
        CreativeStagedUpdateChangedWebhook,
        Outcome,
    )
    from adcp.types.domains.creative.creative_status_changed_webhook import (
        CreativeStatusChangedWebhook,
        From,
        Transition,
    )
    from adcp.types.domains.creative.get_creative_delivery_request import GetCreativeDeliveryRequest
    from adcp.types.domains.creative.get_creative_delivery_response import (
        GetCreativeDeliveryResponse,
        Pagination,
        ReportingPeriod,
    )
    from adcp.types.domains.creative.get_creative_features_async_response_submitted import (
        GetCreativeFeaturesSubmitted,
    )
    from adcp.types.domains.creative.get_creative_features_request import GetCreativeFeaturesRequest
    from adcp.types.domains.creative.get_creative_features_response import (
        GetCreativeFeaturesResponse,
        GetCreativeFeaturesResponse1,
        GetCreativeFeaturesResponse2,
        GetCreativeFeaturesResponse3,
    )
    from adcp.types.domains.creative.get_creative_features_terminal_success import (
        GetCreativeFeaturesSuccess,
    )
    from adcp.types.domains.creative.list_creative_formats_request import (
        ListCreativeFormatsRequestCreativeAgent,
        Type,
    )
    from adcp.types.domains.creative.list_creative_formats_response import (
        CreativeAgent,
        ListCreativeFormatsResponseCreativeAgent,
    )
    from adcp.types.domains.creative.list_creatives_request import (
        AssignmentProjection,
        Field1,
        ListCreativesRequest,
        Sort,
    )
    from adcp.types.domains.creative.list_creatives_response import (
        Assets,
        AssignedPackage,
        Assignments,
        Creatives,
        Creatives1,
        Indicator,
        IndicatorTypesEvaluatedEnum,
        ListCreativesResponse,
        LocalizationUnavailable,
        Purge,
        QuerySummary,
        Snapshot,
        SortApplied,
        StatusSummary,
    )
    from adcp.types.domains.creative.list_transformers_request import (
        ExpandPaginationItem,
        ListTransformersRequestCreativeAgent,
        OutputCapabilityId,
    )
    from adcp.types.domains.creative.list_transformers_response import (
        ListTransformersResponseCreativeAgent,
    )
    from adcp.types.domains.creative.preview_creative_request import (
        Input10,
        PreviewCreativeRequest,
        Request,
        RequestType,
    )
    from adcp.types.domains.creative.preview_creative_response import (
        Input2,
        Preview,
        Preview2,
        Preview3,
        PreviewCreativeResponse,
        PreviewCreativeResponse1,
        PreviewCreativeResponse2,
        PreviewCreativeResponse3,
        PreviewCreativeResponse4,
        Response,
        Result,
    )
    from adcp.types.domains.creative.preview_render import (
        Dimensions,
        Embedding,
        PreviewRender,
        PreviewRender1,
        PreviewRender2,
        PreviewRender3,
    )
    from adcp.types.domains.creative.sync_creatives_async_response_input_required import (
        Reason,
        SyncCreativesInputRequired,
    )
    from adcp.types.domains.creative.sync_creatives_async_response_submitted import (
        SyncCreativesSubmitted,
    )
    from adcp.types.domains.creative.sync_creatives_async_response_working import (
        SyncCreativesWorking,
    )
    from adcp.types.domains.creative.sync_creatives_request import (
        Assignment,
        AssignmentOperations,
        AssignmentOperations1,
        AssignmentOperations2,
        AssignmentOperations3,
        AssignmentOperations4,
        PlacementId,
        SyncCreativesRequest,
    )
    from adcp.types.domains.creative.sync_creatives_response import (
        Merge,
        SyncCreativesResponse,
        SyncCreativesResponse1,
        SyncCreativesResponse2,
        SyncCreativesResponse3,
    )
    from adcp.types.domains.creative.validate_input_request import (
        Targets,
        Targets1,
        Targets2,
        Targets3,
        Targets4,
        ValidateInputRequest,
    )
    from adcp.types.domains.creative.validate_input_response import ValidateInputResponse
    from adcp.types.domains.creative.validate_input_result import (
        Kind,
        ResultKind,
        Target,
        ValidateInputResult,
        Violation,
        Warning,
    )
    from adcp.types.domains.creative.video_brief import Segment, VideoBrief

# Explicit exports
__all__ = [
    "Assets",
    "AssignedPackage",
    "Assignment",
    "AssignmentOperations",
    "AssignmentOperations1",
    "AssignmentOperations2",
    "AssignmentOperations3",
    "AssignmentOperations4",
    "AssignmentProjection",
    "Assignments",
    "ChangeKind",
    "ClaimedValue",
    "CreativeAgent",
    "CreativeAssignmentChangedWebhook",
    "CreativeAuditObservation",
    "CreativeFeatureResult",
    "CreativePurgedWebhook",
    "CreativeStagedUpdateChangedWebhook",
    "CreativeStatusChangedWebhook",
    "Creatives",
    "Creatives1",
    "Details",
    "Dimensions",
    "Embedding",
    "ExpandPaginationItem",
    "Field1",
    "From",
    "GetCreativeDeliveryRequest",
    "GetCreativeDeliveryResponse",
    "GetCreativeFeaturesRequest",
    "GetCreativeFeaturesResponse",
    "GetCreativeFeaturesResponse1",
    "GetCreativeFeaturesResponse2",
    "GetCreativeFeaturesResponse3",
    "GetCreativeFeaturesSubmitted",
    "GetCreativeFeaturesSuccess",
    "HumanOversight",
    "Indicator",
    "IndicatorTypesEvaluatedEnum",
    "Input10",
    "Input2",
    "Kind",
    "ListCreativeFormatsRequestCreativeAgent",
    "ListCreativeFormatsResponseCreativeAgent",
    "ListCreativesRequest",
    "ListCreativesResponse",
    "ListTransformersRequestCreativeAgent",
    "ListTransformersResponseCreativeAgent",
    "LocalizationUnavailable",
    "Merge",
    "Outcome",
    "OutputCapabilityId",
    "Pagination",
    "PlacementId",
    "Preview",
    "Preview2",
    "Preview3",
    "PreviewCreativeRequest",
    "PreviewCreativeResponse",
    "PreviewCreativeResponse1",
    "PreviewCreativeResponse2",
    "PreviewCreativeResponse3",
    "PreviewCreativeResponse4",
    "PreviewRender",
    "PreviewRender1",
    "PreviewRender2",
    "PreviewRender3",
    "Purge",
    "PurgeKind",
    "QuerySummary",
    "Reason",
    "ReportingPeriod",
    "Request",
    "RequestType",
    "Response",
    "Result",
    "ResultKind",
    "Segment",
    "Snapshot",
    "Sort",
    "SortApplied",
    "StatusSummary",
    "SyncCreativesInputRequired",
    "SyncCreativesRequest",
    "SyncCreativesResponse",
    "SyncCreativesResponse1",
    "SyncCreativesResponse2",
    "SyncCreativesResponse3",
    "SyncCreativesSubmitted",
    "SyncCreativesWorking",
    "Target",
    "Targets",
    "Targets1",
    "Targets2",
    "Targets3",
    "Targets4",
    "Transition",
    "Type",
    "ValidateInputRequest",
    "ValidateInputResponse",
    "ValidateInputResult",
    "VideoBrief",
    "Violation",
    "Warning",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "Assets": "adcp.types.domains.creative.list_creatives_response",
    "AssignedPackage": "adcp.types.domains.creative.list_creatives_response",
    "Assignment": "adcp.types.domains.creative.sync_creatives_request",
    "AssignmentOperations": "adcp.types.domains.creative.sync_creatives_request",
    "AssignmentOperations1": "adcp.types.domains.creative.sync_creatives_request",
    "AssignmentOperations2": "adcp.types.domains.creative.sync_creatives_request",
    "AssignmentOperations3": "adcp.types.domains.creative.sync_creatives_request",
    "AssignmentOperations4": "adcp.types.domains.creative.sync_creatives_request",
    "AssignmentProjection": "adcp.types.domains.creative.list_creatives_request",
    "Assignments": "adcp.types.domains.creative.list_creatives_response",
    "ChangeKind": "adcp.types.domains.creative.creative_assignment_changed_webhook",
    "ClaimedValue": "adcp.types.domains.creative.audit_observation",
    "CreativeAgent": "adcp.types.domains.creative.list_creative_formats_response",
    "CreativeAssignmentChangedWebhook": "adcp.types.domains.creative.creative_assignment_changed_webhook",
    "CreativeAuditObservation": "adcp.types.domains.creative.audit_observation",
    "CreativeFeatureResult": "adcp.types.domains.creative.creative_feature_result",
    "CreativePurgedWebhook": "adcp.types.domains.creative.creative_purged_webhook",
    "CreativeStagedUpdateChangedWebhook": "adcp.types.domains.creative.creative_staged_update_changed_webhook",
    "CreativeStatusChangedWebhook": "adcp.types.domains.creative.creative_status_changed_webhook",
    "Creatives": "adcp.types.domains.creative.list_creatives_response",
    "Creatives1": "adcp.types.domains.creative.list_creatives_response",
    "Details": "adcp.types.domains.creative.audit_observation",
    "Dimensions": "adcp.types.domains.creative.preview_render",
    "Embedding": "adcp.types.domains.creative.preview_render",
    "ExpandPaginationItem": "adcp.types.domains.creative.list_transformers_request",
    "Field1": "adcp.types.domains.creative.list_creatives_request",
    "From": "adcp.types.domains.creative.creative_status_changed_webhook",
    "GetCreativeDeliveryRequest": "adcp.types.domains.creative.get_creative_delivery_request",
    "GetCreativeDeliveryResponse": "adcp.types.domains.creative.get_creative_delivery_response",
    "GetCreativeFeaturesRequest": "adcp.types.domains.creative.get_creative_features_request",
    "GetCreativeFeaturesResponse": "adcp.types.domains.creative.get_creative_features_response",
    "GetCreativeFeaturesResponse1": "adcp.types.domains.creative.get_creative_features_response",
    "GetCreativeFeaturesResponse2": "adcp.types.domains.creative.get_creative_features_response",
    "GetCreativeFeaturesResponse3": "adcp.types.domains.creative.get_creative_features_response",
    "GetCreativeFeaturesSubmitted": "adcp.types.domains.creative.get_creative_features_async_response_submitted",
    "GetCreativeFeaturesSuccess": "adcp.types.domains.creative.get_creative_features_terminal_success",
    "HumanOversight": "adcp.types.domains.creative.audit_observation",
    "Indicator": "adcp.types.domains.creative.list_creatives_response",
    "IndicatorTypesEvaluatedEnum": "adcp.types.domains.creative.list_creatives_response",
    "Input10": "adcp.types.domains.creative.preview_creative_request",
    "Input2": "adcp.types.domains.creative.preview_creative_response",
    "Kind": "adcp.types.domains.creative.validate_input_result",
    "ListCreativeFormatsRequestCreativeAgent": "adcp.types.domains.creative.list_creative_formats_request",
    "ListCreativeFormatsResponseCreativeAgent": "adcp.types.domains.creative.list_creative_formats_response",
    "ListCreativesRequest": "adcp.types.domains.creative.list_creatives_request",
    "ListCreativesResponse": "adcp.types.domains.creative.list_creatives_response",
    "ListTransformersRequestCreativeAgent": "adcp.types.domains.creative.list_transformers_request",
    "ListTransformersResponseCreativeAgent": "adcp.types.domains.creative.list_transformers_response",
    "LocalizationUnavailable": "adcp.types.domains.creative.list_creatives_response",
    "Merge": "adcp.types.domains.creative.sync_creatives_response",
    "Outcome": "adcp.types.domains.creative.creative_staged_update_changed_webhook",
    "OutputCapabilityId": "adcp.types.domains.creative.list_transformers_request",
    "Pagination": "adcp.types.domains.creative.get_creative_delivery_response",
    "PlacementId": "adcp.types.domains.creative.sync_creatives_request",
    "Preview": "adcp.types.domains.creative.preview_creative_response",
    "Preview2": "adcp.types.domains.creative.preview_creative_response",
    "Preview3": "adcp.types.domains.creative.preview_creative_response",
    "PreviewCreativeRequest": "adcp.types.domains.creative.preview_creative_request",
    "PreviewCreativeResponse": "adcp.types.domains.creative.preview_creative_response",
    "PreviewCreativeResponse1": "adcp.types.domains.creative.preview_creative_response",
    "PreviewCreativeResponse2": "adcp.types.domains.creative.preview_creative_response",
    "PreviewCreativeResponse3": "adcp.types.domains.creative.preview_creative_response",
    "PreviewCreativeResponse4": "adcp.types.domains.creative.preview_creative_response",
    "PreviewRender": "adcp.types.domains.creative.preview_render",
    "PreviewRender1": "adcp.types.domains.creative.preview_render",
    "PreviewRender2": "adcp.types.domains.creative.preview_render",
    "PreviewRender3": "adcp.types.domains.creative.preview_render",
    "Purge": "adcp.types.domains.creative.list_creatives_response",
    "PurgeKind": "adcp.types.domains.creative.creative_purged_webhook",
    "QuerySummary": "adcp.types.domains.creative.list_creatives_response",
    "Reason": "adcp.types.domains.creative.sync_creatives_async_response_input_required",
    "ReportingPeriod": "adcp.types.domains.creative.get_creative_delivery_response",
    "Request": "adcp.types.domains.creative.preview_creative_request",
    "RequestType": "adcp.types.domains.creative.preview_creative_request",
    "Response": "adcp.types.domains.creative.preview_creative_response",
    "Result": "adcp.types.domains.creative.preview_creative_response",
    "ResultKind": "adcp.types.domains.creative.validate_input_result",
    "Segment": "adcp.types.domains.creative.video_brief",
    "Snapshot": "adcp.types.domains.creative.list_creatives_response",
    "Sort": "adcp.types.domains.creative.list_creatives_request",
    "SortApplied": "adcp.types.domains.creative.list_creatives_response",
    "StatusSummary": "adcp.types.domains.creative.list_creatives_response",
    "SyncCreativesInputRequired": "adcp.types.domains.creative.sync_creatives_async_response_input_required",
    "SyncCreativesRequest": "adcp.types.domains.creative.sync_creatives_request",
    "SyncCreativesResponse": "adcp.types.domains.creative.sync_creatives_response",
    "SyncCreativesResponse1": "adcp.types.domains.creative.sync_creatives_response",
    "SyncCreativesResponse2": "adcp.types.domains.creative.sync_creatives_response",
    "SyncCreativesResponse3": "adcp.types.domains.creative.sync_creatives_response",
    "SyncCreativesSubmitted": "adcp.types.domains.creative.sync_creatives_async_response_submitted",
    "SyncCreativesWorking": "adcp.types.domains.creative.sync_creatives_async_response_working",
    "Target": "adcp.types.domains.creative.validate_input_result",
    "Targets": "adcp.types.domains.creative.validate_input_request",
    "Targets1": "adcp.types.domains.creative.validate_input_request",
    "Targets2": "adcp.types.domains.creative.validate_input_request",
    "Targets3": "adcp.types.domains.creative.validate_input_request",
    "Targets4": "adcp.types.domains.creative.validate_input_request",
    "Transition": "adcp.types.domains.creative.creative_status_changed_webhook",
    "Type": "adcp.types.domains.creative.list_creative_formats_request",
    "ValidateInputRequest": "adcp.types.domains.creative.validate_input_request",
    "ValidateInputResponse": "adcp.types.domains.creative.validate_input_response",
    "ValidateInputResult": "adcp.types.domains.creative.validate_input_result",
    "VideoBrief": "adcp.types.domains.creative.video_brief",
    "Violation": "adcp.types.domains.creative.validate_input_result",
    "Warning": "adcp.types.domains.creative.validate_input_result",
}

if not TYPE_CHECKING:
    import importlib as _importlib

    def __getattr__(name: str) -> object:
        module_name = _DECLARED_IN.get(name)
        if module_name is None:
            # Not a declared name: let the import system try a submodule, or fail.
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(_importlib.import_module(module_name), name)
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(globals()) | set(__all__))
