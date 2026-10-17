"""Types the AdCP ``creative`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.creative import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.creative.<schema>``.
Nothing here is renamed.

This facade is LAZY (PEP 562). It binds no name at import time, because an
eager one makes every leaf of this domain depend on every other leaf: a
submodule import runs this ``__init__``, so one leaf's dependencies become
the whole domain's. That turned a benign submodule-level cycle between
``core`` and ``formats.canonical`` -- two domains whose schemas reference
each other, which Python handles -- into a package-level one that made all
16 modules under ``formats/canonical/`` unimportable on their own.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-16 22:14:49 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The whole importable surface, so type checkers and IDEs see it. Runtime
    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd
    # import is still an error rather than ``object``.
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
        PlacementId,
        SyncCreativesRequest,
    )
    from adcp.types.domains.creative.sync_creatives_response import (
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

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "creative.audit_observation": (
        "ClaimedValue",
        "CreativeAuditObservation",
        "Details",
        "HumanOversight",
    ),
    "creative.creative_assignment_changed_webhook": (
        "ChangeKind",
        "CreativeAssignmentChangedWebhook",
    ),
    "creative.creative_feature_result": ("CreativeFeatureResult",),
    "creative.creative_purged_webhook": (
        "CreativePurgedWebhook",
        "PurgeKind",
    ),
    "creative.creative_status_changed_webhook": (
        "CreativeStatusChangedWebhook",
        "From",
        "Transition",
    ),
    "creative.get_creative_delivery_request": ("GetCreativeDeliveryRequest",),
    "creative.get_creative_delivery_response": (
        "GetCreativeDeliveryResponse",
        "Pagination",
        "ReportingPeriod",
    ),
    "creative.get_creative_features_async_response_submitted": ("GetCreativeFeaturesSubmitted",),
    "creative.get_creative_features_request": ("GetCreativeFeaturesRequest",),
    "creative.get_creative_features_response": (
        "GetCreativeFeaturesResponse",
        "GetCreativeFeaturesResponse1",
        "GetCreativeFeaturesResponse2",
        "GetCreativeFeaturesResponse3",
    ),
    "creative.get_creative_features_terminal_success": ("GetCreativeFeaturesSuccess",),
    "creative.list_creative_formats_request": (
        "ListCreativeFormatsRequestCreativeAgent",
        "Type",
    ),
    "creative.list_creative_formats_response": (
        "CreativeAgent",
        "ListCreativeFormatsResponseCreativeAgent",
    ),
    "creative.list_creatives_request": (
        "AssignmentProjection",
        "Field1",
        "ListCreativesRequest",
        "Sort",
    ),
    "creative.list_creatives_response": (
        "Assets",
        "AssignedPackage",
        "Assignments",
        "Creatives",
        "Creatives1",
        "Indicator",
        "IndicatorTypesEvaluatedEnum",
        "ListCreativesResponse",
        "LocalizationUnavailable",
        "Purge",
        "QuerySummary",
        "Snapshot",
        "SortApplied",
        "StatusSummary",
    ),
    "creative.list_transformers_request": (
        "ExpandPaginationItem",
        "ListTransformersRequestCreativeAgent",
        "OutputCapabilityId",
    ),
    "creative.list_transformers_response": ("ListTransformersResponseCreativeAgent",),
    "creative.preview_creative_request": (
        "Input10",
        "PreviewCreativeRequest",
        "Request",
        "RequestType",
    ),
    "creative.preview_creative_response": (
        "Input2",
        "Preview",
        "Preview2",
        "Preview3",
        "PreviewCreativeResponse",
        "PreviewCreativeResponse1",
        "PreviewCreativeResponse2",
        "PreviewCreativeResponse3",
        "PreviewCreativeResponse4",
        "Response",
        "Result",
    ),
    "creative.preview_render": (
        "Dimensions",
        "Embedding",
        "PreviewRender",
        "PreviewRender1",
        "PreviewRender2",
        "PreviewRender3",
    ),
    "creative.sync_creatives_async_response_input_required": (
        "Reason",
        "SyncCreativesInputRequired",
    ),
    "creative.sync_creatives_async_response_submitted": ("SyncCreativesSubmitted",),
    "creative.sync_creatives_async_response_working": ("SyncCreativesWorking",),
    "creative.sync_creatives_request": (
        "Assignment",
        "AssignmentOperations",
        "AssignmentOperations1",
        "AssignmentOperations2",
        "AssignmentOperations3",
        "PlacementId",
        "SyncCreativesRequest",
    ),
    "creative.sync_creatives_response": (
        "SyncCreativesResponse",
        "SyncCreativesResponse1",
        "SyncCreativesResponse2",
        "SyncCreativesResponse3",
    ),
    "creative.validate_input_request": (
        "Targets",
        "Targets1",
        "Targets2",
        "Targets3",
        "Targets4",
        "ValidateInputRequest",
    ),
    "creative.validate_input_response": ("ValidateInputResponse",),
    "creative.validate_input_result": (
        "Kind",
        "ResultKind",
        "Target",
        "ValidateInputResult",
        "Violation",
        "Warning",
    ),
    "creative.video_brief": (
        "Segment",
        "VideoBrief",
    ),
}

if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {name: module for module, names in _DECLARED_BY.items() for name in names}

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))


# Explicit exports
__all__ = [
    "Assets",
    "AssignedPackage",
    "Assignment",
    "AssignmentOperations",
    "AssignmentOperations1",
    "AssignmentOperations2",
    "AssignmentOperations3",
    "AssignmentProjection",
    "Assignments",
    "ChangeKind",
    "ClaimedValue",
    "CreativeAgent",
    "CreativeAssignmentChangedWebhook",
    "CreativeAuditObservation",
    "CreativeFeatureResult",
    "CreativePurgedWebhook",
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
