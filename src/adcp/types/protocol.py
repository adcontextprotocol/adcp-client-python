"""AdCP protocol types — curated partial surface.

Cross-cutting protocol types — request / response envelopes, errors,
pagination, task status, capabilities, and webhook challenge handshakes.

A stable, narrow alternative to importing the whole :mod:`adcp.types`
namespace. Every name here is also exported from :mod:`adcp.types`; this
module simply groups the ones a protocol integration reaches for, and never
exposes the internal generated layer.

This module is for curation and discoverability, not a separate
performance tier: importing it is cheap, but the first access to *any* AdCP
type (here or via :mod:`adcp.types` / :mod:`adcp`) realizes the full generated
Pydantic graph — there is no per-domain graph. Use it for a smaller, focused
import surface.

    from adcp.types.protocol import Request
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "Request",
    "Response",
    "ProtocolEnvelope",
    "ProtocolResponse",
    "AdcpProtocol",
    "Protocol",
    "Error",
    "ErrorCode",
    "AuthorizationRequiredDetails",
    "Metadata",
    "ContextObject",
    "ExtensionObject",
    "Pagination",
    "PaginationRequest",
    "PaginationResponse",
    "Sort",
    "SortDirection",
    "SortApplied",
    "QuerySummary",
    "StatusSummary",
    "GetTaskStatusRequest",
    "GetTaskStatusResponse",
    "ListTasksRequest",
    "ListTasksResponse",
    "TaskType",
    "TaskResult",
    "GeneratedTaskStatus",
    "GetAdcpCapabilitiesRequest",
    "GetAdcpCapabilitiesResponse",
    "WebhookChallenge",
    "WebhookChallengeResponse",
    "WebhookResponseType",
    "WebhookMetadata",
    "McpWebhookPayload",
    "ResponsePayloadJwsEnvelope",
    "NotificationConfig",
    "NotificationType",
    "PushNotificationConfig",
    "Authentication",
    "AuthenticationScheme",
    "Security",
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
        AdcpProtocol,
        Authentication,
        AuthenticationScheme,
        AuthorizationRequiredDetails,
        ContextObject,
        Error,
        ErrorCode,
        ExtensionObject,
        GeneratedTaskStatus,
        GetAdcpCapabilitiesRequest,
        GetAdcpCapabilitiesResponse,
        GetTaskStatusRequest,
        GetTaskStatusResponse,
        ListTasksRequest,
        ListTasksResponse,
        McpWebhookPayload,
        Metadata,
        NotificationConfig,
        NotificationType,
        Pagination,
        PaginationRequest,
        PaginationResponse,
        Protocol,
        ProtocolEnvelope,
        ProtocolResponse,
        PushNotificationConfig,
        QuerySummary,
        Request,
        Response,
        ResponsePayloadJwsEnvelope,
        Security,
        Sort,
        SortApplied,
        SortDirection,
        StatusSummary,
        TaskResult,
        TaskType,
        WebhookChallenge,
        WebhookChallengeResponse,
        WebhookMetadata,
        WebhookResponseType,
    )
