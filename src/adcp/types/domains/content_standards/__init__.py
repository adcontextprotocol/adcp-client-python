"""Types the AdCP ``content_standards`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.content_standards import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.content_standards.<schema>``.
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

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "content_standards.artifact": (
        "AssetAccess",
        "AssetAccess1",
        "AssetAccess2",
        "AssetAccess3",
        "Assets",
        "Assets1",
        "Assets2",
        "Assets3",
        "Assets4",
        "ContentFormat",
        "Identifiers",
        "Metadata",
        "Provider",
        "Role",
        "TranscriptFormat",
        "TranscriptSource",
        "TranscriptSource1",
    ),
    "content_standards.artifact_webhook_payload": ("ArtifactWebhookPayload",),
    "content_standards.calibrate_content_request": ("CalibrateContentRequest",),
    "content_standards.calibrate_content_response": (
        "CalibrateContentResponse",
        "CalibrateContentResponse1",
        "CalibrateContentResponse2",
    ),
    "content_standards.content_standards": ("ContentStandards",),
    "content_standards.create_content_standards_request": ("CreateContentStandardsRequest",),
    "content_standards.create_content_standards_response": (
        "CreateContentStandardsResponse",
        "CreateContentStandardsResponse1",
        "CreateContentStandardsResponse2",
    ),
    "content_standards.get_content_standards_request": ("GetContentStandardsRequest",),
    "content_standards.get_content_standards_response": (
        "GetContentStandardsResponse",
        "GetContentStandardsResponse1",
        "GetContentStandardsResponse2",
    ),
    "content_standards.get_media_buy_artifacts_request": (
        "GetMediaBuyArtifactsRequest",
        "TimeRange",
    ),
    "content_standards.get_media_buy_artifacts_response": (
        "CollectionInfo",
        "GetMediaBuyArtifactsResponse",
        "GetMediaBuyArtifactsResponse1",
        "GetMediaBuyArtifactsResponse2",
    ),
    "content_standards.list_content_standards_request": ("ListContentStandardsRequest",),
    "content_standards.list_content_standards_response": (
        "ListContentStandardsResponse",
        "ListContentStandardsResponse1",
        "ListContentStandardsResponse2",
    ),
    "content_standards.update_content_standards_request": ("UpdateContentStandardsRequest",),
    "content_standards.update_content_standards_response": (
        "UpdateContentStandardsResponse",
        "UpdateContentStandardsResponse1",
        "UpdateContentStandardsResponse2",
    ),
    "content_standards.validate_content_delivery_request": (
        "Record",
        "ValidateContentDeliveryRequest",
    ),
    "content_standards.validate_content_delivery_response": (
        "Result",
        "Summary",
        "ValidateContentDeliveryResponse",
        "ValidateContentDeliveryResponse1",
        "ValidateContentDeliveryResponse2",
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
