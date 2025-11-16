from __future__ import annotations

"""Type definitions for AdCP client."""

from adcp.types.base import AdCPBaseModel
from adcp.types.core import (
    Activity,
    ActivityType,
    AgentConfig,
    DebugInfo,
    Protocol,
    TaskResult,
    TaskStatus,
    WebhookMetadata,
)
from adcp.types.generated import (
    BothPreviewRender,
    HtmlPreviewRender,
    InlineDaastAsset,
    InlineVastAsset,
    MediaSubAsset,
    TextSubAsset,
    UrlDaastAsset,
    UrlPreviewRender,
    UrlVastAsset,
)

__all__ = [
    "AdCPBaseModel",
    "AgentConfig",
    "Protocol",
    "TaskResult",
    "TaskStatus",
    "WebhookMetadata",
    "Activity",
    "ActivityType",
    "DebugInfo",
    # Semantic aliases for discriminated unions
    "BothPreviewRender",
    "HtmlPreviewRender",
    "InlineDaastAsset",
    "InlineVastAsset",
    "MediaSubAsset",
    "TextSubAsset",
    "UrlDaastAsset",
    "UrlPreviewRender",
    "UrlVastAsset",
]
