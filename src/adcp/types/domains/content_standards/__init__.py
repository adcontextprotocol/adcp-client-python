"""Types the AdCP ``content_standards`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.content_standards import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.content_standards.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:49:37 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.content_standards.artifact import (
        AssetAccess,
        AssetAccess1,
        AssetAccess2,
        AssetAccess3,
        Assets,
        Assets1,
        Assets2,
        Assets3,
        Assets4,
        ContentFormat,
        Identifiers,
        Metadata,
        Provider,
        Role,
        TranscriptFormat,
        TranscriptSource,
        TranscriptSource1,
    )
    from adcp.types.domains.content_standards.artifact_webhook_payload import ArtifactWebhookPayload
    from adcp.types.domains.content_standards.calibrate_content_request import (
        CalibrateContentRequest,
    )
    from adcp.types.domains.content_standards.calibrate_content_response import (
        CalibrateContentResponse,
        CalibrateContentResponse1,
        CalibrateContentResponse2,
    )
    from adcp.types.domains.content_standards.content_standards import ContentStandards
    from adcp.types.domains.content_standards.create_content_standards_request import (
        CreateContentStandardsRequest,
    )
    from adcp.types.domains.content_standards.create_content_standards_response import (
        CreateContentStandardsResponse,
        CreateContentStandardsResponse1,
        CreateContentStandardsResponse2,
    )
    from adcp.types.domains.content_standards.get_content_standards_request import (
        GetContentStandardsRequest,
    )
    from adcp.types.domains.content_standards.get_content_standards_response import (
        GetContentStandardsResponse,
        GetContentStandardsResponse1,
        GetContentStandardsResponse2,
    )
    from adcp.types.domains.content_standards.get_media_buy_artifacts_request import (
        GetMediaBuyArtifactsRequest,
        TimeRange,
    )
    from adcp.types.domains.content_standards.get_media_buy_artifacts_response import (
        CollectionInfo,
        GetMediaBuyArtifactsResponse,
        GetMediaBuyArtifactsResponse1,
        GetMediaBuyArtifactsResponse2,
    )
    from adcp.types.domains.content_standards.list_content_standards_request import (
        ListContentStandardsRequest,
    )
    from adcp.types.domains.content_standards.list_content_standards_response import (
        ListContentStandardsResponse,
        ListContentStandardsResponse1,
        ListContentStandardsResponse2,
    )
    from adcp.types.domains.content_standards.update_content_standards_request import (
        UpdateContentStandardsRequest,
    )
    from adcp.types.domains.content_standards.update_content_standards_response import (
        UpdateContentStandardsResponse,
        UpdateContentStandardsResponse1,
        UpdateContentStandardsResponse2,
    )
    from adcp.types.domains.content_standards.validate_content_delivery_request import (
        Record,
        ValidateContentDeliveryRequest,
    )
    from adcp.types.domains.content_standards.validate_content_delivery_response import (
        Result,
        Summary,
        ValidateContentDeliveryResponse,
        ValidateContentDeliveryResponse1,
        ValidateContentDeliveryResponse2,
    )

# Explicit exports
__all__ = [
    "ArtifactWebhookPayload",
    "AssetAccess",
    "AssetAccess1",
    "AssetAccess2",
    "AssetAccess3",
    "Assets",
    "Assets1",
    "Assets2",
    "Assets3",
    "Assets4",
    "CalibrateContentRequest",
    "CalibrateContentResponse",
    "CalibrateContentResponse1",
    "CalibrateContentResponse2",
    "CollectionInfo",
    "ContentFormat",
    "ContentStandards",
    "CreateContentStandardsRequest",
    "CreateContentStandardsResponse",
    "CreateContentStandardsResponse1",
    "CreateContentStandardsResponse2",
    "GetContentStandardsRequest",
    "GetContentStandardsResponse",
    "GetContentStandardsResponse1",
    "GetContentStandardsResponse2",
    "GetMediaBuyArtifactsRequest",
    "GetMediaBuyArtifactsResponse",
    "GetMediaBuyArtifactsResponse1",
    "GetMediaBuyArtifactsResponse2",
    "Identifiers",
    "ListContentStandardsRequest",
    "ListContentStandardsResponse",
    "ListContentStandardsResponse1",
    "ListContentStandardsResponse2",
    "Metadata",
    "Provider",
    "Record",
    "Result",
    "Role",
    "Summary",
    "TimeRange",
    "TranscriptFormat",
    "TranscriptSource",
    "TranscriptSource1",
    "UpdateContentStandardsRequest",
    "UpdateContentStandardsResponse",
    "UpdateContentStandardsResponse1",
    "UpdateContentStandardsResponse2",
    "ValidateContentDeliveryRequest",
    "ValidateContentDeliveryResponse",
    "ValidateContentDeliveryResponse1",
    "ValidateContentDeliveryResponse2",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "ArtifactWebhookPayload": "adcp.types.domains.content_standards.artifact_webhook_payload",
    "AssetAccess": "adcp.types.domains.content_standards.artifact",
    "AssetAccess1": "adcp.types.domains.content_standards.artifact",
    "AssetAccess2": "adcp.types.domains.content_standards.artifact",
    "AssetAccess3": "adcp.types.domains.content_standards.artifact",
    "Assets": "adcp.types.domains.content_standards.artifact",
    "Assets1": "adcp.types.domains.content_standards.artifact",
    "Assets2": "adcp.types.domains.content_standards.artifact",
    "Assets3": "adcp.types.domains.content_standards.artifact",
    "Assets4": "adcp.types.domains.content_standards.artifact",
    "CalibrateContentRequest": "adcp.types.domains.content_standards.calibrate_content_request",
    "CalibrateContentResponse": "adcp.types.domains.content_standards.calibrate_content_response",
    "CalibrateContentResponse1": "adcp.types.domains.content_standards.calibrate_content_response",
    "CalibrateContentResponse2": "adcp.types.domains.content_standards.calibrate_content_response",
    "CollectionInfo": "adcp.types.domains.content_standards.get_media_buy_artifacts_response",
    "ContentFormat": "adcp.types.domains.content_standards.artifact",
    "ContentStandards": "adcp.types.domains.content_standards.content_standards",
    "CreateContentStandardsRequest": "adcp.types.domains.content_standards.create_content_standards_request",
    "CreateContentStandardsResponse": "adcp.types.domains.content_standards.create_content_standards_response",
    "CreateContentStandardsResponse1": "adcp.types.domains.content_standards.create_content_standards_response",
    "CreateContentStandardsResponse2": "adcp.types.domains.content_standards.create_content_standards_response",
    "GetContentStandardsRequest": "adcp.types.domains.content_standards.get_content_standards_request",
    "GetContentStandardsResponse": "adcp.types.domains.content_standards.get_content_standards_response",
    "GetContentStandardsResponse1": "adcp.types.domains.content_standards.get_content_standards_response",
    "GetContentStandardsResponse2": "adcp.types.domains.content_standards.get_content_standards_response",
    "GetMediaBuyArtifactsRequest": "adcp.types.domains.content_standards.get_media_buy_artifacts_request",
    "GetMediaBuyArtifactsResponse": "adcp.types.domains.content_standards.get_media_buy_artifacts_response",
    "GetMediaBuyArtifactsResponse1": "adcp.types.domains.content_standards.get_media_buy_artifacts_response",
    "GetMediaBuyArtifactsResponse2": "adcp.types.domains.content_standards.get_media_buy_artifacts_response",
    "Identifiers": "adcp.types.domains.content_standards.artifact",
    "ListContentStandardsRequest": "adcp.types.domains.content_standards.list_content_standards_request",
    "ListContentStandardsResponse": "adcp.types.domains.content_standards.list_content_standards_response",
    "ListContentStandardsResponse1": "adcp.types.domains.content_standards.list_content_standards_response",
    "ListContentStandardsResponse2": "adcp.types.domains.content_standards.list_content_standards_response",
    "Metadata": "adcp.types.domains.content_standards.artifact",
    "Provider": "adcp.types.domains.content_standards.artifact",
    "Record": "adcp.types.domains.content_standards.validate_content_delivery_request",
    "Result": "adcp.types.domains.content_standards.validate_content_delivery_response",
    "Role": "adcp.types.domains.content_standards.artifact",
    "Summary": "adcp.types.domains.content_standards.validate_content_delivery_response",
    "TimeRange": "adcp.types.domains.content_standards.get_media_buy_artifacts_request",
    "TranscriptFormat": "adcp.types.domains.content_standards.artifact",
    "TranscriptSource": "adcp.types.domains.content_standards.artifact",
    "TranscriptSource1": "adcp.types.domains.content_standards.artifact",
    "UpdateContentStandardsRequest": "adcp.types.domains.content_standards.update_content_standards_request",
    "UpdateContentStandardsResponse": "adcp.types.domains.content_standards.update_content_standards_response",
    "UpdateContentStandardsResponse1": "adcp.types.domains.content_standards.update_content_standards_response",
    "UpdateContentStandardsResponse2": "adcp.types.domains.content_standards.update_content_standards_response",
    "ValidateContentDeliveryRequest": "adcp.types.domains.content_standards.validate_content_delivery_request",
    "ValidateContentDeliveryResponse": "adcp.types.domains.content_standards.validate_content_delivery_response",
    "ValidateContentDeliveryResponse1": "adcp.types.domains.content_standards.validate_content_delivery_response",
    "ValidateContentDeliveryResponse2": "adcp.types.domains.content_standards.validate_content_delivery_response",
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
