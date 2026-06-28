"""AdCP creative types — curated partial surface.

Creative + format types — sync / build / preview creatives, creative status
and approval, formats, and the open creative-asset union.

A stable, narrow alternative to importing the whole :mod:`adcp.types`
namespace. Every name here is also exported from :mod:`adcp.types`; this
module simply groups the ones a creative integration reaches for, and never
exposes the internal generated layer.

This module is for curation and discoverability, not a separate
performance tier: importing it is cheap, but the first access to *any* AdCP
type (here or via :mod:`adcp.types` / :mod:`adcp`) realizes the full generated
Pydantic graph — there is no per-domain graph. Use it for a smaller, focused
import surface.

    from adcp.types.creative import SyncCreativesRequest
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "SyncCreativesRequest",
    "SyncCreativesResponse",
    "SyncCreativesSuccessResponse",
    "SyncCreativesSubmittedResponse",
    "SyncCreativesErrorResponse",
    "BuildCreativeRequest",
    "BuildCreativeResponse",
    "BuildCreativeSuccessResponse",
    "BuildCreativeSubmittedResponse",
    "BuildCreativeErrorResponse",
    "PreviewCreativeRequest",
    "PreviewCreativeResponse",
    "PreviewCreativeSingleResponse",
    "PreviewCreativeBatchResponse",
    "PreviewCreativeVariantResponse",
    "PreviewRender",
    "Renders",
    "ListCreativesRequest",
    "ListCreativesResponse",
    "ListCreativeFormatsRequest",
    "ListCreativeFormatsResponse",
    "Creative",
    "CreativeAsset",
    "CreativeManifest",
    "CreativeVariant",
    "CreativeAssignment",
    "CreativeApproval",
    "CreativeApprovalStatus",
    "CreativeStatus",
    "CreativePolicy",
    "CreativeFilters",
    "CreativeAgent",
    "Format",
    "FormatId",
    "FormatCard",
    "FormatCardDetailed",
    "FormatAssetUnion",
    "GroupFormatAssetUnion",
    "RepeatableAssetGroup",
    "Asset",
    "AssetContentType",
    "ImageContent",
    "VideoContent",
    "AudioContent",
    "HtmlContent",
    "TextContent",
    "UrlContent",
    "Dimensions",
    "Responsive",
    "GetCreativeFeaturesRequest",
    "GetCreativeFeaturesResponse",
]


if not TYPE_CHECKING:
    # Lazy runtime resolution (shared with the other partial modules). Defined
    # under ``not TYPE_CHECKING`` so type checkers see the surface only via the
    # explicit ``TYPE_CHECKING`` re-export block below — a typo'd import is
    # flagged rather than silently typed as ``object``.
    from adcp.types._partial import lazy_partial_surface

    __getattr__, __dir__ = lazy_partial_surface(__name__, __all__, globals())


if TYPE_CHECKING:
    # Eager re-export so type checkers and IDEs see the surface; resolved
    # lazily through ``__getattr__`` at runtime.
    from adcp.types import (  # noqa: F401
        Asset,
        AssetContentType,
        AudioContent,
        BuildCreativeErrorResponse,
        BuildCreativeRequest,
        BuildCreativeResponse,
        BuildCreativeSubmittedResponse,
        BuildCreativeSuccessResponse,
        Creative,
        CreativeAgent,
        CreativeApproval,
        CreativeApprovalStatus,
        CreativeAsset,
        CreativeAssignment,
        CreativeFilters,
        CreativeManifest,
        CreativePolicy,
        CreativeStatus,
        CreativeVariant,
        Dimensions,
        Format,
        FormatAssetUnion,
        FormatCard,
        FormatCardDetailed,
        FormatId,
        GetCreativeFeaturesRequest,
        GetCreativeFeaturesResponse,
        GroupFormatAssetUnion,
        HtmlContent,
        ImageContent,
        ListCreativeFormatsRequest,
        ListCreativeFormatsResponse,
        ListCreativesRequest,
        ListCreativesResponse,
        PreviewCreativeBatchResponse,
        PreviewCreativeRequest,
        PreviewCreativeResponse,
        PreviewCreativeSingleResponse,
        PreviewCreativeVariantResponse,
        PreviewRender,
        Renders,
        RepeatableAssetGroup,
        Responsive,
        SyncCreativesErrorResponse,
        SyncCreativesRequest,
        SyncCreativesResponse,
        SyncCreativesSubmittedResponse,
        SyncCreativesSuccessResponse,
        TextContent,
        UrlContent,
        VideoContent,
    )
