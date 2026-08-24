from __future__ import annotations

"""Main client classes for AdCP."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import warnings
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypedDict, cast
from uuid import uuid4

from a2a.types import Task, TaskStatusUpdateEvent
from pydantic import BaseModel, TypeAdapter

if TYPE_CHECKING:
    import httpx
    from mcp import ClientSession

from adcp._version import resolve_adcp_version
from adcp.canonical_formats import (
    CanonicalFormatLegacyResolutionError,
    CanonicalFormatLegacyResolver,
    CreativeDialect,
    CreativeDialectError,
    LegacyCreativeProjectionError,
    LegacyFormatConverter,
    normalize_legacy_creative_request,
    project_legacy_format_id,
    project_legacy_product,
    resolve_creative_dialect,
    resolve_legacy_format_refs,
)
from adcp.capabilities import TASK_FEATURE_MAP, FeatureResolver, looks_like_v3_capabilities
from adcp.compat.legacy import LEGACY_ADAPTER_VERSIONS
from adcp.exceptions import ADCPError, ADCPWebhookSignatureError
from adcp.negotiation import (
    WIRE_RESPONSE_METADATA_KEY,
    VerifiedRefinementResult,
)
from adcp.negotiation import (
    refine_proposals_verified as _refine_proposals_verified,
)
from adcp.negotiation import verify_refinement_result as _verify_refinement_result
from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.base import ProtocolAdapter
from adcp.protocols.mcp import MCPAdapter, MCPHttpxClientFactory
from adcp.signing.autosign import (
    SigningConfig,
    operation_needs_signing,
    signing_profile_for_adcp_version,
)
from adcp.signing.autosign import (
    current_operation as _signing_current_operation,
)
from adcp.signing.signer import sign_request
from adcp.types import (
    AcceptProposalRequest,
    AcceptProposalResponse,
    ActivateSignalRequest,
    ActivateSignalResponse,
    BuyProductsRequest,
    BuyProductsResponse,
    ControlMediaBuyRequest,
    ControlMediaBuyResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    DeclineProposalsRequest,
    DeclineProposalsResponse,
    Format,
    GeneratedTaskStatus,
    GetAccountFinancialsRequest,
    GetAccountFinancialsResponse,
    GetCreativeDeliveryRequest,
    GetCreativeDeliveryResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetMediaBuysRequest,
    GetMediaBuysResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    ListAccountsRequest,
    ListAccountsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    ListProductsRequest,
    ListProductsResponse,
    LogEventRequest,
    LogEventResponse,
    Product,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    RefineProposalsRequest,
    RefineProposalsResponse,
    ReportUsageRequest,
    ReportUsageResponse,
    RequestProposalsRequest,
    RequestProposalsResponse,
    SyncAccountsRequest,
    SyncAccountsResponse,
    SyncAudiencesRequest,
    SyncAudiencesResponse,
    SyncCatalogsRequest,
    SyncCatalogsResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
    SyncEventSourcesRequest,
    SyncEventSourcesResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
)
from adcp.types.canonical_creative import strip_legacy_creative_identity
from adcp.types.core import (
    Activity,
    ActivityType,
    AgentConfig,
    Protocol,
    TaskResult,
    TaskStatus,
    is_loopback_http_uri,
)

# V3 Governance (Sync Governance) types
from adcp.types.generated_poc.account.sync_governance_request import (
    SyncGovernanceRequest,
)
from adcp.types.generated_poc.account.sync_governance_response import (
    SyncGovernanceResponse,
)
from adcp.types.generated_poc.brand.acquire_rights_request import AcquireRightsRequest
from adcp.types.generated_poc.brand.acquire_rights_response import (
    AcquireRightsResponse,
)
from adcp.types.generated_poc.brand.get_brand_identity_request import (
    GetBrandIdentityRequest,
)
from adcp.types.generated_poc.brand.get_brand_identity_response import (
    GetBrandIdentityResponse,
)
from adcp.types.generated_poc.brand.get_rights_request import GetRightsRequest
from adcp.types.generated_poc.brand.get_rights_response import GetRightsResponse
from adcp.types.generated_poc.brand.update_rights_request import UpdateRightsRequest
from adcp.types.generated_poc.brand.update_rights_response import (
    UpdateRightsResponse,
)

# V3 Governance (Collection Lists) types
from adcp.types.generated_poc.collection.create_collection_list_request import (
    CreateCollectionListRequest,
)
from adcp.types.generated_poc.collection.create_collection_list_response import (
    CreateCollectionListResponse,
)
from adcp.types.generated_poc.collection.delete_collection_list_request import (
    DeleteCollectionListRequest,
)
from adcp.types.generated_poc.collection.delete_collection_list_response import (
    DeleteCollectionListResponse,
)
from adcp.types.generated_poc.collection.get_collection_list_request import (
    GetCollectionListRequest,
)
from adcp.types.generated_poc.collection.get_collection_list_response import (
    GetCollectionListResponse,
)
from adcp.types.generated_poc.collection.list_collection_lists_request import (
    ListCollectionListsRequest,
)
from adcp.types.generated_poc.collection.list_collection_lists_response import (
    ListCollectionListsResponse,
)
from adcp.types.generated_poc.collection.update_collection_list_request import (
    UpdateCollectionListRequest,
)
from adcp.types.generated_poc.collection.update_collection_list_response import (
    UpdateCollectionListResponse,
)
from adcp.types.generated_poc.compliance.comply_test_controller_request import (
    ComplyTestControllerRequest,
)
from adcp.types.generated_poc.compliance.comply_test_controller_response import (
    ComplyTestControllerResponse,
)
from adcp.types.generated_poc.content_standards.calibrate_content_request import (
    CalibrateContentRequest,
)
from adcp.types.generated_poc.content_standards.calibrate_content_response import (
    CalibrateContentResponse,
)

# V3 Content Standards types
from adcp.types.generated_poc.content_standards.create_content_standards_request import (
    CreateContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.create_content_standards_response import (
    CreateContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.get_content_standards_request import (
    GetContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.get_content_standards_response import (
    GetContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.get_media_buy_artifacts_request import (
    GetMediaBuyArtifactsRequest,
)
from adcp.types.generated_poc.content_standards.get_media_buy_artifacts_response import (
    GetMediaBuyArtifactsResponse,
)
from adcp.types.generated_poc.content_standards.list_content_standards_request import (
    ListContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.list_content_standards_response import (
    ListContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.update_content_standards_request import (
    UpdateContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.update_content_standards_response import (
    UpdateContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.validate_content_delivery_request import (
    ValidateContentDeliveryRequest,
)
from adcp.types.generated_poc.content_standards.validate_content_delivery_response import (
    ValidateContentDeliveryResponse,
)
from adcp.types.generated_poc.core.async_response_data import AdcpAsyncResponseData
from adcp.types.generated_poc.creative.get_creative_features_request import (
    GetCreativeFeaturesRequest,
)
from adcp.types.generated_poc.creative.get_creative_features_response import (
    GetCreativeFeaturesResponse,
)
from adcp.types.generated_poc.creative.list_transformers_request import (
    ListTransformersRequestCreativeAgent as ListTransformersRequest,
)
from adcp.types.generated_poc.creative.list_transformers_response import (
    ListTransformersResponseCreativeAgent as ListTransformersResponse,
)

# V3 Governance (Property Lists) types
from adcp.types.generated_poc.governance.check_governance_request import (
    CheckGovernanceRequest,
)
from adcp.types.generated_poc.governance.check_governance_response import (
    CheckGovernanceResponse,
)
from adcp.types.generated_poc.governance.get_plan_audit_logs_request import (
    GetPlanAuditLogsRequest,
)
from adcp.types.generated_poc.governance.get_plan_audit_logs_response import (
    GetPlanAuditLogsResponse,
)
from adcp.types.generated_poc.governance.report_plan_adjustment_request import (
    ReportPlanAdjustmentRequest,
)
from adcp.types.generated_poc.governance.report_plan_adjustment_response import (
    ReportPlanAdjustmentResponse,
)
from adcp.types.generated_poc.governance.report_plan_outcome_request import (
    ReportPlanOutcomeRequest,
)
from adcp.types.generated_poc.governance.report_plan_outcome_response import (
    ReportPlanOutcomeResponse,
)
from adcp.types.generated_poc.governance.sync_plans_request import SyncPlansRequest
from adcp.types.generated_poc.governance.sync_plans_response import SyncPlansResponse
from adcp.types.generated_poc.property.create_property_list_request import (
    CreatePropertyListRequest,
)
from adcp.types.generated_poc.property.create_property_list_response import (
    CreatePropertyListResponse,
)
from adcp.types.generated_poc.property.delete_property_list_request import (
    DeletePropertyListRequest,
)
from adcp.types.generated_poc.property.delete_property_list_response import (
    DeletePropertyListResponse,
)
from adcp.types.generated_poc.property.get_property_list_request import (
    GetPropertyListRequest,
)
from adcp.types.generated_poc.property.get_property_list_response import (
    GetPropertyListResponse,
)
from adcp.types.generated_poc.property.list_property_lists_request import (
    ListPropertyListsRequest,
)
from adcp.types.generated_poc.property.list_property_lists_response import (
    ListPropertyListsResponse,
)
from adcp.types.generated_poc.property.update_property_list_request import (
    UpdatePropertyListRequest,
)
from adcp.types.generated_poc.property.update_property_list_response import (
    UpdatePropertyListResponse,
)

# V3 Protocol Discovery types
from adcp.types.generated_poc.protocol.get_adcp_capabilities_request import (
    GetAdcpCapabilitiesRequest,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    GetAdcpCapabilitiesResponse,
)
from adcp.types.generated_poc.protocol.get_task_status_request import GetTaskStatusRequest
from adcp.types.generated_poc.protocol.get_task_status_response import GetTaskStatusResponse
from adcp.types.generated_poc.protocol.list_tasks_request import ListTasksRequest
from adcp.types.generated_poc.protocol.list_tasks_response import ListTasksResponse
from adcp.types.generated_poc.protocol.sync_agent_notification_configs_request import (
    SyncAgentNotificationConfigsRequest,
)
from adcp.types.generated_poc.protocol.sync_agent_notification_configs_response import (
    SyncAgentNotificationConfigsResponse,
)

# V3 Sponsored Intelligence types
from adcp.types.generated_poc.sponsored_intelligence.si_get_offering_request import (
    SiGetOfferingRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_get_offering_response import (
    SiGetOfferingResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_initiate_session_request import (
    SiInitiateSessionRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_initiate_session_response import (
    SiInitiateSessionResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_send_message_request import (
    SiSendMessageRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_send_message_response import (
    SiSendMessageResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_terminate_session_request import (
    SiTerminateSessionRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_terminate_session_response import (
    SiTerminateSessionResponse,
)
from adcp.types.generated_poc.trusted_match.context_match_request import ContextMatchRequest
from adcp.types.generated_poc.trusted_match.context_match_response import (
    ContextMatchResponseRouterPublisher as ContextMatchResponse,
)
from adcp.types.generated_poc.trusted_match.identity_match_request import IdentityMatchRequest
from adcp.types.generated_poc.trusted_match.identity_match_response import IdentityMatchResponse
from adcp.types.legacy import (
    LegacyBuildCreativeRequest,
    LegacyBuildCreativeResponse,
    LegacyCreateMediaBuyRequest,
    LegacyCreateMediaBuyResponse,
    LegacyGetCreativeDeliveryResponse,
    LegacyGetMediaBuyDeliveryResponse,
    LegacyGetMediaBuysResponse,
    LegacyGetProductsRequest,
    LegacyGetProductsResponse,
    LegacyListCreativesRequest,
    LegacyListCreativesResponse,
    LegacyPreviewCreativeRequest,
    LegacyPreviewCreativeResponse,
    LegacySyncCreativesRequest,
    LegacySyncCreativesResponse,
    LegacyUpdateMediaBuyRequest,
    LegacyUpdateMediaBuyResponse,
)
from adcp.types.legacy import (
    LegacyListCreativeFormatsRequest as ListCreativeFormatsRequest,
)
from adcp.types.legacy import (
    LegacyListCreativeFormatsResponse as ListCreativeFormatsResponse,
)
from adcp.utils.operation_id import create_operation_id
from adcp.validation.client_hooks import ValidationHookConfig
from adcp.validation.version import resolve_bundle_key

logger = logging.getLogger(__name__)

_LEGACY_CREATIVE_TASKS = frozenset(
    {
        "build_creative",
        "create_media_buy",
        "get_creative_delivery",
        "get_media_buy_delivery",
        "get_media_buys",
        "get_products",
        "list_creative_formats",
        "list_creatives",
        "preview_creative",
        "sync_creatives",
        "update_media_buy",
    }
)
_LEGACY_ONLY_CREATIVE_TASKS = frozenset(
    {"build_creative", "list_creative_formats", "preview_creative"}
)


class Checkpoint(TypedDict):
    """Persistable session-resume state for an A2A ``ADCPClient``.

    The minimal set of fields needed to reconnect to an in-flight A2A
    conversation after a process restart. Produced by
    ``ADCPClient.checkpoint()``; consumed by
    ``ADCPClient.from_checkpoint()``.

    - ``agent_id`` — binds the checkpoint to the agent that minted it,
      so a restore against the wrong ``AgentConfig`` fails loudly
      instead of sending Agent A's ids to Agent B.
    - ``context_id`` — the A2A conversation id.
    - ``active_task_id`` — the in-flight task the next message must
      echo; ``None`` if no task is pending.
    """

    agent_id: str
    context_id: str | None
    active_task_id: str | None


def _resolve_server_version(pin: str | None) -> str | None:
    """Validate the optional ``server_version`` constructor arg.

    Returns the normalized bundle-key (``"3.0.7"`` → ``"3.0"``,
    ``"2.5"`` → ``"2.5"``) so :meth:`ADCPClient.get_server_version`
    reports a stable shape. ``None`` passes through.

    Pins to a pre-3.0 version in
    :data:`adcp.compat.legacy.LEGACY_ADAPTER_VERSIONS` emit a
    :class:`DeprecationWarning`. Canonical creative negotiation covers the
    supported AdCP 3.0/3.1 matrix; older protocol adapters remain
    migration-only.

    Garbage input raises :class:`ValueError` — same contract as
    :func:`adcp.validation.version.resolve_bundle_key`.
    """
    if pin is None:
        return None
    normalized = resolve_bundle_key(pin)
    if normalized in LEGACY_ADAPTER_VERSIONS:
        import warnings as _warnings

        _warnings.warn(
            f"server_version={pin!r} pins this client to a legacy AdCP "
            f"wire shape outside the canonical-creative 3.x negotiation "
            f"matrix. Upgrade the seller to AdCP 3.0 or newer.",
            DeprecationWarning,
            stacklevel=3,
        )
    return normalized


class ADCPClient:
    """Client for interacting with a single AdCP agent."""

    def __init__(
        self,
        agent_config: AgentConfig,
        webhook_url_template: str | None = None,
        webhook_secret: str | None = None,
        on_activity: Callable[[Activity], None] | None = None,
        webhook_timestamp_tolerance: int = 300,
        capabilities_ttl: float = 3600.0,
        validate_features: bool = False,
        strict_idempotency: bool = False,
        signing: SigningConfig | None = None,
        context_id: str | None = None,
        validation: ValidationHookConfig | None = None,
        force_a2a_version: str | None = None,
        adcp_version: str | None = None,
        server_version: str | None = None,
        legacy_format_converter: LegacyFormatConverter | None = None,
        canonical_format_legacy_resolver: CanonicalFormatLegacyResolver | None = None,
        allow_unauthenticated_webhooks: bool = False,
        httpx_client_factory: MCPHttpxClientFactory | None = None,
    ):
        """
        Initialize ADCP client for a single agent.

        Args:
            agent_config: Agent configuration
            webhook_url_template: Template for webhook URLs with {agent_id},
                {task_type}, {operation_id}
            webhook_secret: Shared secret for the deprecated HMAC-SHA256 webhook
                fallback. Configure this only when the registration explicitly
                selected legacy HMAC; conformant public endpoints should use
                :class:`adcp.webhooks.WebhookReceiver` for RFC 9421 verification.
            allow_unauthenticated_webhooks: Explicit compatibility escape for
                accepting unsigned MCP webhooks when ``webhook_secret`` is not
                configured. Defaults to False so public webhook receivers fail
                closed. Only enable this for endpoints that cannot be reached
                from an untrusted network. A2A webhook handling is unaffected.
            on_activity: Callback for activity events
            webhook_timestamp_tolerance: Maximum age (in seconds) for webhook
                timestamps. Webhooks with timestamps older than this or more than
                this far in the future are rejected. Defaults to 300 (5 minutes).
            capabilities_ttl: Time-to-live in seconds for cached capabilities (default: 1 hour)
            validate_features: When True, automatically check that the seller supports
                required features before making task calls (e.g., sync_audiences requires
                audience_targeting). Requires capabilities to have been fetched first.
            strict_idempotency: When True, verify the seller declared
                ``adcp.idempotency.replay_ttl_seconds`` in capabilities before any
                mutating call. Fetches capabilities lazily on first use. Raises
                ``IdempotencyUnsupportedError`` if the declaration is missing —
                sellers that don't declare it provide no retry-safety guarantee
                per AdCP #2315. Defaults to False for backward compatibility.
            signing: Optional RFC 9421 request-signing config. When provided,
                the client automatically attaches ``Signature`` /
                ``Signature-Input`` / ``Content-Digest`` headers to operations
                the seller's ``request_signing`` capability lists in
                ``required_for``, ``warn_for``, or ``supported_for``. The
                seller's ``covers_content_digest`` policy determines whether
                the body is bound to the signature. Generate a key with
                ``adcp-keygen`` and publish the public JWK at your
                ``jwks_uri``. Supported on both A2A and MCP
                (``mcp_transport="streamable_http"``); SSE-transport MCP
                logs a warning and falls through unsigned.
            httpx_client_factory: MCP-only factory for the async HTTP client
                used by streamable HTTP, SSE, and explicit session close.
                Use this to enforce an application-owned egress policy. The
                factory must accept the standard MCP client kwargs and return
                a compatible client with ``trust_env=False`` and
                ``follow_redirects=False``; the SDK verifies both and composes
                request-signing hooks with the client's existing hooks. MCP
                SDK v2 uses ``httpx2``; a plain ``httpx.AsyncClient`` (including
                one using the signing package's httpx-native pinned transport)
                is not wire-compatible and is rejected explicitly.
            validation: Schema-driven validation modes for outgoing
                requests and incoming responses against the bundled AdCP
                JSON schemas. Defaults (matching the TS port): requests
                in ``warn`` mode (drift logged but not blocked — partial
                payloads in error-path tests still work) and responses
                in ``strict`` mode (agent drift fails the task).
                ``ADCP_VALIDATION_MODE=strict|warn|off`` overrides both
                sides at call time (matches the TS port); ``ADCP_ENV``
                set to ``production`` / ``prod`` flips only the response
                default to ``warn``. Generic ``ENV`` / ``ENVIRONMENT`` /
                ``PYTHON_ENV`` are deliberately ignored — they collide
                with unrelated tooling. Storyboards and compliance
                runners that want hard-stop enforcement everywhere pass
                ``validation=ValidationHookConfig(requests="strict",
                responses="strict")``; high-throughput callers can set
                either side to ``"off"`` to skip the validator entirely
                with zero overhead.
            context_id: A2A-only. Seed the A2A conversation context. Pass a
                previously-returned ``context_id`` to resume a session
                across process restarts, or a self-assigned UUID to name
                the session with your own correlation key (the ADK server
                honors buyer-proposed ids). If omitted, the server mints
                one on the first message and this client auto-retains it
                for subsequent calls. Read the current value via
                ``client.context_id``; call ``client.reset_context()`` to
                start a fresh conversation. Rule of thumb: one
                ``ADCPClient`` per A2A conversation — if a buyer has
                multiple concurrent briefs with the same agent, construct
                one client per brief rather than sharing.

                For HITL flows that can span a process restart mid-task,
                use ``checkpoint()`` / ``from_checkpoint()`` instead of
                persisting ``context_id`` alone — full resume state is
                both ``context_id`` AND ``active_task_id``.

                Raises ``TypeError`` if passed with a non-A2A protocol.
            force_a2a_version: A2A-only. Pin the **A2A transport
                version** (e.g. ``"0.3"``, ``"1.0"``) by filtering the
                peer's advertised ``supported_interfaces`` to entries
                whose ``protocol_version`` matches. Not for AdCP
                protocol pinning — see ``adcp_version`` for that.
                Intended for tests or for forcing a 0.3-speaking path
                against a dual-advertising peer. Raises
                :class:`ADCPConnectionError` on the first call if no
                advertised interface matches. ``None`` (default) lets
                the SDK's ``ClientFactory`` pick the most capable
                transport the peer supports. Use
                :attr:`a2a_protocol_versions` to probe what a peer
                advertises before pinning.

                Raises ``TypeError`` if passed with a non-A2A protocol.
            adcp_version: AdCP protocol release this client speaks
                (release-precision string, e.g. ``"3.0"``, ``"3.1"``,
                ``"3.1-beta"``). Stripe-style per-instance pin: the
                value is sent as ``adcp_version`` on every outbound
                request and selects creative dialect behavior. ``None``
                (default) resolves
                to the SDK's compile-time pin (``ADCP_VERSION``
                packaged with the wheel). Cross-major pins raise
                :class:`ConfigurationError` at construction; install
                the SDK major that targets your wire version instead.
                Patch-precision strings (``"3.0.1"``) and build
                metadata (``"3.0.1+canary"``) are accepted at construction
                but normalized to release-precision before wire emission
                per the spec — patches and build metadata are not part
                of the negotiation contract. ``get_adcp_version()``
                returns the normalized form.

                Caller-supplied ``adcp_version`` on a per-call params
                dict wins over the constructor pin: the enricher is
                the default, not an override.

                Migration from ``adcp_major_version`` (legacy integer
                wire field): generated request types still expose
                ``adcp_major_version: int | None`` from the pre-#3493
                schema. Both fields will coexist on the wire through
                3.x; servers prefer the new ``adcp_version`` when both
                are present. Stop populating ``adcp_major_version`` on
                request models once your seller advertises 3.1 in
                ``supported_versions``.
            server_version: AdCP wire shape the *seller* speaks. Most
                adopters leave this ``None`` — the SDK assumes a v3
                seller and the seller's
                ``/.well-known/agent-card.json`` is the canonical
                source of truth once a probe-and-cache path lands.

                Pin explicitly when:

                * You're talking to a known-legacy seller (e.g.
                  ``server_version="3.0"``). Canonical discovery results
                  are upgraded for application code and canonical writes
                  are downgraded only through preserved or explicit routes.
                * You want telemetry to attribute outbound traffic to
                  a specific server-side version regardless of what the
                  seller advertises.

                Retrieve the current value via :meth:`get_server_version`.
        """
        self._adcp_version: str = resolve_adcp_version(adcp_version)
        self._server_version: str | None = _resolve_server_version(server_version)
        if type(allow_unauthenticated_webhooks) is not bool:
            raise TypeError("allow_unauthenticated_webhooks must be a bool")

        self.agent_config = agent_config
        self.webhook_url_template = webhook_url_template
        self.webhook_secret = webhook_secret
        self.allow_unauthenticated_webhooks = allow_unauthenticated_webhooks
        self.on_activity = on_activity
        self.webhook_timestamp_tolerance = webhook_timestamp_tolerance
        self.capabilities_ttl = capabilities_ttl
        self.validate_features = validate_features
        self.strict_idempotency = strict_idempotency
        self.signing = signing
        if httpx_client_factory is not None and not callable(httpx_client_factory):
            raise TypeError("httpx_client_factory must be callable")
        if httpx_client_factory is not None and agent_config.protocol != Protocol.MCP:
            raise TypeError(
                "httpx_client_factory is only supported for MCP protocol; "
                f"got {agent_config.protocol}"
            )
        if (
            signing is not None
            and agent_config.agent_uri.startswith("http://")
            and not is_loopback_http_uri(agent_config.agent_uri)
        ):
            raise ValueError(
                "request signing requires an https:// agent_uri for non-loopback hosts; "
                "plain HTTP is only allowed for localhost/loopback development"
            )
        self.legacy_format_converter = legacy_format_converter
        self.canonical_format_legacy_resolver = canonical_format_legacy_resolver
        self._canonical_legacy_routes: dict[
            tuple[str | None, str | None, str], list[dict[str, Any]] | None
        ] = {}

        # Capabilities cache
        self._capabilities: GetAdcpCapabilitiesResponse | None = None
        self._feature_resolver: FeatureResolver | None = None
        self._capabilities_fetched_at: float | None = None
        self._idempotency_capability_verified: bool = False
        # Unique per-instance token so use_idempotency_key scopes to this
        # client and does not bleed to siblings (AdCP #2315 cross-seller risk).
        from uuid import uuid4 as _uuid4

        self._idempotency_client_token: str = _uuid4().hex

        if force_a2a_version is not None and agent_config.protocol != Protocol.A2A:
            raise TypeError(
                f"force_a2a_version is only supported for A2A protocol; got {agent_config.protocol}"
            )

        # Initialize protocol adapter
        self.adapter: ProtocolAdapter
        if agent_config.protocol == Protocol.A2A:
            self.adapter = A2AAdapter(agent_config, force_a2a_version=force_a2a_version)
        elif agent_config.protocol == Protocol.MCP:
            self.adapter = MCPAdapter(
                agent_config,
                httpx_client_factory=httpx_client_factory,
            )
        else:
            raise ValueError(f"Unsupported protocol: {agent_config.protocol}")

        self.adapter.idempotency_client_token = self._idempotency_client_token
        if strict_idempotency:
            self.adapter.idempotency_capability_check = self._ensure_idempotency_capability
        if signing is not None:
            self.adapter.signing_request_hook = self._sign_outgoing_request
            self.adapter.signing_capability_check = self._prepare_signing_capabilities
        # Apply schema validation modes (default: requests=warn, responses=strict
        # in dev/test, warn in production — see ``ValidationHookConfig`` docs).
        self.adapter.configure_validation(validation)
        # Auto-inject the per-instance ``adcp_version`` pin into every
        # outbound request envelope. Caller-supplied values on the
        # request object win — the enricher is the default, not an
        # override — so per-call overrides remain available once the
        # generated request types declare the field.
        _pinned_version = self._server_version or self._adcp_version
        self._signing_profile_version = (
            None
            if signing is None
            else signing.signing_profile_version
            or signing_profile_for_adcp_version(_pinned_version)
        )

        def _inject_adcp_version(params: dict[str, Any]) -> dict[str, Any]:
            return {"adcp_version": _pinned_version, **params}

        self.adapter.envelope_enricher = _inject_adcp_version

        if context_id:
            # Empty string is treated as "not provided" — callers using
            # ``context_id=os.getenv("...") or ""`` patterns shouldn't
            # silently seed an empty id on the wire.
            if not isinstance(self.adapter, A2AAdapter):
                raise TypeError(
                    f"context_id is only supported for A2A protocol; got {agent_config.protocol}"
                )
            self.adapter.set_context_id(context_id)

        # Initialize simple API accessor (lazy import to avoid circular dependency)
        from adcp.simple import SimpleAPI

        self.simple = SimpleAPI(self)

    def get_adcp_version(self) -> str:
        """Return the AdCP protocol release this client is pinned to.

        Resolved at construction from the ``adcp_version`` kwarg, with
        fallback to the SDK's compile-time pin (``ADCP_VERSION``
        packaged with the wheel) when the caller didn't pin
        explicitly. Same value across the client's lifetime — the pin
        is per-instance, not per-call.

        See ``__init__``'s ``adcp_version`` parameter for the full
        semantics, including the cross-major fence and dialect selection.
        """
        return self._adcp_version

    def get_server_version(self) -> str | None:
        """Return the seller's AdCP wire-shape version, or ``None``.

        ``None`` means the SDK is assuming a current-major seller
        (the default). Returns a release-precision string
        (``"3.0"``, ``"3.1"``, ``"2.5"``) when the adopter pinned
        via the ``server_version`` constructor arg or — once the
        agent-card probe lands — when the SDK detected the seller's
        version from its agent-card.

        See ``__init__``'s ``server_version`` parameter for negotiated
        canonical/legacy creative behavior.
        """
        return self._server_version

    @property
    def context_id(self) -> str | None:
        """Current A2A conversation context_id.

        Reads the context_id currently associated with this client: the
        value assigned by the A2A server (auto-captured from the most
        recent response) or the one seeded via the constructor or
        ``reset_context()``. Returns ``None`` before the first A2A call
        in a fresh conversation, or for clients on non-A2A protocols —
        reads are lenient across protocols so generic code can probe
        ``if client.context_id: ...`` safely. Writes (constructor kwarg,
        ``reset_context``) raise on non-A2A because the operation has no
        meaning there.

        Not safe for concurrent calls on the same client — the adapter
        mutates this on every response. Rule of thumb: one ADCPClient
        per A2A conversation.

        For simple completed-task resume, persist this value and pass
        it to ``ADCPClient(context_id=...)``. For HITL flows that may
        restart mid-``input-required``, use ``checkpoint()`` /
        ``from_checkpoint()`` — full resume state is both this id AND
        ``active_task_id``.
        """
        if isinstance(self.adapter, A2AAdapter):
            return self.adapter.context_id
        return None

    @property
    def active_task_id(self) -> str | None:
        """A2A task_id the next send must echo to resume the same task.

        Set when the last A2A response was non-terminal
        (``input-required``, ``working``, ``submitted``,
        ``auth-required``). The adapter echoes this id on the next
        outbound message so the server resumes the same task. Clears
        automatically when the task reaches a terminal state.

        Full resume state is *both* ``context_id`` and
        ``active_task_id`` — persist both (or use ``checkpoint()``) to
        survive a process restart mid-HITL without orphaning the task.

        Returns ``None`` for non-A2A clients.
        """
        if isinstance(self.adapter, A2AAdapter):
            return self.adapter.active_task_id
        return None

    @property
    def a2a_protocol_versions(self) -> list[str] | None:
        """A2A ``protocol_version`` strings the peer advertises, sorted.

        Lazily populated after the first operation that fetches the
        peer's ``AgentCard`` (``fetch_capabilities``, ``list_tools``,
        ``get_agent_info``, or any skill-call). Returns ``None`` before
        the card has been fetched so callers can distinguish "not yet
        known" from "peer advertises nothing" (empty list). Returns
        ``None`` for non-A2A clients.

        Useful for probing which wire version a peer speaks — buyers
        running alongside both 0.3-era and 1.0-era agents can use this
        to confirm what they're talking to.
        """
        if isinstance(self.adapter, A2AAdapter):
            return self.adapter.a2a_protocol_versions
        return None

    def reset_context(self, context_id: str | None = None) -> None:
        """Start a new A2A conversation on this client.

        Passing ``None`` (default) clears the current context so the
        server mints a fresh one on the next call. Passing a string uses
        it as the new conversation id — useful for resuming a specific
        prior session or for naming the conversation with your own
        correlation key. Note: some servers (notably ADK) rewrite
        client-supplied ids into their own session format; the client
        auto-adopts the rewritten id on the next response.

        Also clears any active_task_id — starting a new conversation
        discards any in-flight task on the old one.

        Raises ``TypeError`` when called on a non-A2A client.
        """
        if not isinstance(self.adapter, A2AAdapter):
            raise TypeError(
                f"reset_context is only supported for A2A protocol; "
                f"got {self.agent_config.protocol}"
            )
        self.adapter.set_context_id(context_id)

    def checkpoint(self) -> Checkpoint:
        """Return the minimal state needed to resume this A2A session.

        Full resume for HITL / multi-turn flows requires *both*
        ``context_id`` (which conversation) AND ``active_task_id``
        (which in-flight task to echo). Persisting only ``context_id``
        reconnects to the right conversation but orphans the pending
        task server-side — the next send starts a new task under the
        same context, and the original ``input-required`` task is
        abandoned.

        The returned dict also carries ``agent_id`` so a later
        ``from_checkpoint`` call against a different ``AgentConfig``
        fails loudly instead of sending one agent's session ids to
        another.

        Pair with ``ADCPClient.from_checkpoint(agent_config, state)``.

        Returns a fully-populated ``Checkpoint`` on non-A2A clients
        with ``context_id``/``active_task_id`` set to ``None``, so
        generic persist-and-restore code can call this without
        branching on protocol.
        """
        return Checkpoint(
            agent_id=self.agent_config.id,
            context_id=self.context_id,
            active_task_id=self.active_task_id,
        )

    @classmethod
    def from_checkpoint(
        cls,
        agent_config: AgentConfig,
        state: Checkpoint,
        **kwargs: Any,
    ) -> ADCPClient:
        """Rehydrate an ADCPClient from a prior ``checkpoint()``.

        Restores both ``context_id`` and ``active_task_id`` so a process
        restart mid-``input-required`` can resume the same task, not
        orphan it. Accepts the same keyword arguments as ``__init__``
        (signing, strict_idempotency, etc.) — the checkpoint only
        carries session-resume state; operational config is re-supplied
        by the caller.

        Raises ``ValueError`` if the checkpoint's ``agent_id`` doesn't
        match ``agent_config.id`` — a checkpoint minted for Agent A
        must not be restored onto Agent B, or the client will leak
        Agent A's opaque session ids to Agent B on the next message.

        Raises ``TypeError`` on a non-A2A ``agent_config`` if the
        checkpoint carries a non-empty ``context_id`` or
        ``active_task_id`` — session-resume state on a protocol that
        doesn't support it would be silently dropped, masking bugs.
        An empty/absent checkpoint round-trips cleanly on any protocol.
        """
        saved_agent_id = state.get("agent_id") if state else None
        if saved_agent_id and saved_agent_id != agent_config.id:
            raise ValueError(
                f"checkpoint was minted for agent {saved_agent_id!r}, "
                f"cannot restore against {agent_config.id!r}"
            )
        context_id = state.get("context_id") if state else None
        active_task_id = state.get("active_task_id") if state else None
        if active_task_id and agent_config.protocol != Protocol.A2A:
            raise TypeError(
                f"active_task_id in checkpoint is only supported for A2A "
                f"protocol; got {agent_config.protocol}"
            )
        client = cls(agent_config, context_id=context_id, **kwargs)
        if active_task_id and isinstance(client.adapter, A2AAdapter):
            client.adapter._restore_active_task_id(active_task_id)
        return client

    @classmethod
    def from_mcp_client(
        cls,
        client: ClientSession,
        *,
        agent_id: str | None = None,
        validation: ValidationHookConfig | None = None,
        capabilities_ttl: float = 3600.0,
        validate_features: bool = False,
        strict_idempotency: bool = False,
    ) -> ADCPClient:
        """Create an ADCPClient wrapping a pre-connected MCP ClientSession.

        Parity with JS ``AgentClient.fromMCPClient()`` (v5.19.0). The primary
        use case is compliance test fleets that wire a full ``ADCPClient``
        against an in-process MCP server without standing up a loopback HTTP
        server.

        Warning:
            The returned client's ``close()`` and ``async with`` ``__aexit__``
            are **no-ops** — the caller owns the injected session and is
            responsible for closing it. Code that relies on ``async with
            ADCPClient.from_mcp_client(...) as c:`` to clean up the session
            will leak the session.

            Webhook delivery and ``on_activity`` callbacks are **not wired**
            on the in-process path — there is no HTTP transport for the
            seller to call back through. Don't pass these to the factory
            (they're absent from the signature on purpose).

            If the injected session has not been initialized
            (``await session.initialize()``), the first tool call surfaces
            as an opaque MCP protocol error in ``TaskResult.error``. The
            factory does not initialize for you — verify before calling.

        **Session lifecycle:** the caller owns the session — ``close()`` and
        ``async with`` exit on the returned client are no-ops. Use your own
        ``AsyncExitStack`` to scope both the transport and the client::

            import contextlib
            from mcp import ClientSession
            from mcp.shared.memory import create_client_server_memory_streams

            async with contextlib.AsyncExitStack() as stack:
                (c_read, c_write), (s_read, s_write) = await stack.enter_async_context(
                    create_client_server_memory_streams()
                )
                # wire your in-process server to (s_read, s_write) here
                session = await stack.enter_async_context(
                    ClientSession(c_read, c_write)
                )
                await session.initialize()
                # close() is a no-op on injected sessions; no stack.enter_async_context needed.
                adcp_client = ADCPClient.from_mcp_client(session, agent_id="test-seller")
                result = await adcp_client.get_products(GetProductsRequest(...))

        Note:
            Request signing is not supported on the injected-session path —
            the signing hook is wired into the HTTP transport layer that is
            bypassed here. ``signing=`` is intentionally absent from this
            factory's parameters.

        Args:
            client: A pre-connected ``mcp.ClientSession`` whose
                ``initialize()`` has already been awaited.
            agent_id: Identifier for the wrapped agent used in log messages
                and error objects. Defaults to a unique ``in-process-XXXXXXXX``
                token; set this explicitly when running multiple in-process
                agents concurrently so log lines are distinguishable.
            validation: Schema-validation modes (same as ``__init__``).
            strict_idempotency: Verify seller declared idempotency support
                before each mutating call (same as ``__init__``).
            validate_features: Gate tool calls on fetched capability
                declarations (same as ``__init__``).
            capabilities_ttl: TTL for the capability cache in seconds
                (same as ``__init__``).

        Returns:
            A fully configured ``ADCPClient`` backed by the injected session.
        """
        effective_id = agent_id if agent_id is not None else f"in-process-{uuid4().hex[:8]}"
        config = AgentConfig(
            id=effective_id,
            # RFC 2606 .invalid TLD — passes the http:// validator, guaranteed
            # not to route to a real host. Self-documenting in error messages.
            agent_uri="http://in-process.invalid",
            protocol=Protocol.MCP,
        )
        instance = cls(
            config,
            validation=validation,
            strict_idempotency=strict_idempotency,
            validate_features=validate_features,
            capabilities_ttl=capabilities_ttl,
        )
        if not isinstance(instance.adapter, MCPAdapter):
            raise RuntimeError(  # pragma: no cover
                f"from_mcp_client: expected MCPAdapter but got {type(instance.adapter).__name__}"
            )
        instance.adapter._inject_session(client)
        return instance

    async def _ensure_idempotency_capability(self) -> None:
        """Verify the seller positively declares idempotency support in capabilities.

        Called before every mutating request when ``strict_idempotency=True``.
        Fetches capabilities on first invocation; subsequent calls are no-ops
        once the declaration has been observed. Raises
        ``IdempotencyUnsupportedError`` when ``adcp.idempotency`` is missing,
        declares ``supported=False`` (seller does not dedupe — naive retry
        would double-process), or declares ``supported=True`` without a
        ``replay_ttl_seconds`` window.

        Sets ``_idempotency_capability_verified = True`` BEFORE calling
        ``fetch_capabilities`` so any recursive dispatch through the adapter
        terminates (``get_adcp_capabilities`` is non-mutating, so it would
        short-circuit anyway — but this guard protects against future refactors
        that might add it to the mutating set).
        """
        from adcp.exceptions import IdempotencyUnsupportedError

        if self._idempotency_capability_verified:
            return

        self._idempotency_capability_verified = True
        try:
            caps = await self.fetch_capabilities()
            adcp_info = getattr(caps, "adcp", None)
            idempotency_info = getattr(adcp_info, "idempotency", None) if adcp_info else None

            if idempotency_info is None:
                raise IdempotencyUnsupportedError(
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                    reason="seller did not declare adcp.idempotency",
                )

            supported = getattr(idempotency_info, "supported", None)
            if supported is False:
                raise IdempotencyUnsupportedError(
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                    reason="seller declared adcp.idempotency.supported=false",
                )

            ttl = getattr(idempotency_info, "replay_ttl_seconds", None)
            if ttl is None:
                raise IdempotencyUnsupportedError(
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                    reason=(
                        "seller declared adcp.idempotency.supported=true but omitted "
                        "replay_ttl_seconds"
                    ),
                )
        except Exception:
            self._idempotency_capability_verified = False
            raise

    async def _prepare_signing_capabilities(self) -> None:
        """Populate signing policy before a transport writer sends a request."""
        await self.fetch_capabilities()

    @staticmethod
    def _mcp_operation_from_request(request: httpx.Request) -> str | None:
        """Extract one MCP ``tools/call`` name from its JSON-RPC body."""
        try:
            payload = json.loads(request.content)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("method") != "tools/call":
            return None
        params = payload.get("params")
        if not isinstance(params, dict):
            return None
        name = params.get("name")
        return name if isinstance(name, str) and name else None

    async def _sign_outgoing_request(self, request: httpx.Request) -> None:
        """httpx request event hook that attaches RFC 9421 signature headers.

        Installed on the protocol adapter's httpx client when a
        ``SigningConfig`` was passed to ``ADCPClient``. Consults the
        seller's advertised ``request_signing`` capability and signs only
        the operations the seller listed in ``required_for``, ``warn_for``,
        or ``supported_for`` — other requests (including the agent-card
        fetch and ``get_adcp_capabilities`` itself) pass through unsigned.
        The ``covers_content_digest`` tri-state determines whether the
        body is bound to the signature.
        """
        if self.signing is None:
            return
        mcp_operation = self._mcp_operation_from_request(request)
        operation = mcp_operation or _signing_current_operation.get()
        # Unset ContextVar → out-of-band call (agent-card fetch, session
        # initialize, etc). Skip without fetching capabilities.
        #
        # get_adcp_capabilities → bootstrap carve-out: signing it would
        # require capabilities we don't have yet, and if a pathological
        # seller listed this op in its own required_for we'd recurse.
        # Keep this check narrow — only operations strictly required to
        # *obtain* capabilities belong here. Today that's just
        # get_adcp_capabilities. A future adapter that adds another
        # capabilities-precondition op MUST extend this guard.
        if operation is None or operation == "get_adcp_capabilities":
            return

        if mcp_operation is not None:
            # MCP's httpx hook runs in a writer task whose ContextVar snapshot
            # was captured when the session connected. The adapter prefetches
            # capabilities in the caller task before enqueueing tools/call;
            # fetching here would deadlock the same writer stream.
            caps = self._capabilities
            if caps is None:
                raise RuntimeError(
                    "MCP request signing policy was not prefetched before tools/call"
                )
        else:
            # A2A's event hook runs in the caller task. Retain this fallback
            # for direct/custom transports, while the bundled adapter also
            # prefetches so normal hooks stay network-free.
            caps = self._capabilities or await self.fetch_capabilities()
        req_signing = getattr(caps, "request_signing", None)

        # Detect and surface a malformed seller config: supported=False is
        # "signatures are ignored", but populating required_for alongside
        # it is contradictory. The classifier correctly skips (matches
        # verifier behavior) but the silent downgrade hides a config bug
        # that will bite pilots.
        if (
            req_signing is not None
            and not req_signing.supported
            and (req_signing.required_for or req_signing.warn_for)
        ):
            logger.warning(
                "Seller %s advertises request_signing.supported=false but "
                "populates required_for/warn_for — treating as unsupported "
                "per spec. Verify the seller's capability advertisement.",
                self.agent_config.id,
            )

        decision = operation_needs_signing(req_signing, operation)
        if decision == "skip":
            return

        covers_policy: str | None = None
        if req_signing is not None and req_signing.covers_content_digest is not None:
            covers_policy = req_signing.covers_content_digest.value
        if covers_policy == "forbidden":
            cover_digest = False
        elif covers_policy == "required":
            cover_digest = True
        else:
            # "either" or absent — signer's choice; default stricter.
            cover_digest = True

        body = request.content
        signing_profile_version = self._signing_profile_version
        if signing_profile_version is None:  # pragma: no cover - constructor invariant
            raise RuntimeError("request signing profile was not initialized")
        signed = sign_request(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=body,
            private_key=self.signing.private_key,
            key_id=self.signing.key_id,
            alg=self.signing.alg,
            cover_content_digest=cover_digest,
            tag=self.signing.tag,
            signing_profile_version=signing_profile_version,
        )
        # pop-then-set ensures our signed values are authoritative even if
        # another hook or earlier layer added a same-named header. httpx
        # headers are a case-insensitive MultiDict, so a naive assignment
        # could leave a duplicate value in a different case.
        for header_name, header_value in signed.as_dict().items():
            request.headers.pop(header_name, None)
            request.headers[header_name] = header_value

    def get_webhook_url(self, task_type: str, operation_id: str) -> str:
        """Generate webhook URL for a task."""
        if not self.webhook_url_template:
            raise ValueError("webhook_url_template not configured")

        return self.webhook_url_template.format(
            agent_id=self.agent_config.id,
            task_type=task_type,
            operation_id=operation_id,
        )

    def _emit_activity(self, activity: Activity) -> None:
        """Emit activity event."""
        if self.on_activity:
            self.on_activity(activity)

    @contextlib.contextmanager
    def use_idempotency_key(self, key: str) -> Iterator[str]:
        """Pin an ``idempotency_key`` for the next mutating call on THIS client.

        Use when you've persisted a key (e.g., in a buyer-side database) and
        want the SDK to send that exact key on resume or retry across process
        restarts. The key is validated against ``^[A-Za-z0-9_.:-]{16,255}$`` on
        entry; a ``ValueError`` is raised for malformed keys.

        Scope rules:

        * **Single-use within scope.** The first mutating call inside the
          ``with`` block consumes the pinned key; a second mutating call falls
          through to a fresh UUID. This protects against ``asyncio.gather``
          siblings accidentally sharing the key (which would trigger
          ``IDEMPOTENCY_CONFLICT`` or silently duplicate work). If you need to
          retry, wrap each attempt in its own ``with`` block.
        * **Client-scoped.** The pinned key applies only to calls on THIS
          client. A mutating call on a sibling ``ADCPClient`` inside the same
          ``with`` block generates a fresh key and emits a ``UserWarning`` —
          keys must be unique per (seller, request) pair (AdCP #2315).
        * **No nesting.** Nested ``use_idempotency_key`` on the same client
          raises ``RuntimeError``.

        Example::

            with client.use_idempotency_key(campaign.stored_key):
                result = await client.create_media_buy(request)
        """
        from adcp import _idempotency

        _idempotency.validate_key(key)
        token = self._idempotency_client_token
        if token in _idempotency._scoped_keys:
            raise RuntimeError(
                "use_idempotency_key is already active on this client; "
                "nested usage is not supported."
            )
        _idempotency._scoped_keys[token] = key
        try:
            yield key
        finally:
            _idempotency._scoped_keys.pop(token, None)

    # ========================================================================
    # Capability Validation
    # ========================================================================

    @property
    def capabilities(self) -> GetAdcpCapabilitiesResponse | None:
        """Return cached capabilities, or None if not yet fetched."""
        return self._capabilities

    @property
    def feature_resolver(self) -> FeatureResolver | None:
        """Return the FeatureResolver for cached capabilities, or None."""
        return self._feature_resolver

    async def fetch_capabilities(self) -> GetAdcpCapabilitiesResponse:
        """Fetch capabilities, using cache if still valid.

        Returns:
            The seller's capabilities response.
        """
        if self._capabilities is not None and self._capabilities_fetched_at is not None:
            elapsed = time.monotonic() - self._capabilities_fetched_at
            if elapsed < self.capabilities_ttl:
                return self._capabilities

        return await self.refresh_capabilities()

    async def refresh_capabilities(self) -> GetAdcpCapabilitiesResponse:
        """Fetch capabilities from the seller, bypassing cache.

        On strict-schema validation failure the raw response is inspected with
        ``looks_like_v3_capabilities``: if the agent is structurally v3-shaped,
        a wire-shape bug is surfaced loudly with the original validation error
        rather than silently downgrading to v2 (the v2 fallback would then ask
        for v2.5 schemas, which aren't shipped — one missing field would
        cascade into "AdCP schema data for version v2.5 not found"). Genuinely
        non-v3 responses still fall through to the transport-error path.

        Returns:
            The seller's capabilities response.

        Raises:
            ADCPError: On transport failure, or when the response is
                v3-shaped but fails schema validation. The error message
                explicitly references v3 in the latter case so the underlying
                wire-shape bug doesn't get blamed on a v2.5-schema cascade.
        """
        result = await self.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
        if result.success and result.data is not None:
            self._capabilities = result.data
            self._feature_resolver = FeatureResolver(result.data)
            self._capabilities_fetched_at = time.monotonic()
            return self._capabilities

        # The typed call discards the raw payload on parse failure (only the
        # error string survives). Distinguish parse-failure (worth shape-
        # checking) from transport-failure (no data ever arrived) by the
        # error prefix produced by ProtocolAdapter._parse_response. Only on
        # parse-failure do we re-fetch the raw dict from the adapter to
        # inspect its shape; transport failures fall straight through to
        # the original error path.
        raw_data: Any = None
        is_parse_failure = result.error is not None and result.error.startswith(
            "Failed to parse response:"
        )
        if is_parse_failure:
            raw_result = await self.adapter.get_adcp_capabilities(
                GetAdcpCapabilitiesRequest().model_dump(mode="json", exclude_none=True)
            )
            raw_data = raw_result.data
            if isinstance(raw_data, list) and len(raw_data) == 1 and isinstance(raw_data[0], dict):
                # MCP content array — unwrap a single-item content envelope
                # so the heuristic sees the same shape the parser would.
                raw_data = raw_data[0]

        if looks_like_v3_capabilities(raw_data):
            logger.warning(
                "[AdCP] Agent %r returned a get_adcp_capabilities response that "
                "failed validation, but the response is structurally v3-shaped. "
                "The agent has a wire-shape bug — that's the thing to fix. "
                "(has_error=%s, has_data=%s)",
                self.agent_config.id,
                bool(result.error),
                raw_data is not None,
            )
            raise ADCPError(
                f"v3 capabilities response from agent {self.agent_config.id!r} "
                f"failed schema validation: {result.error or result.message}. "
                f"The response is structurally v3-shaped (carries `adcp`, "
                f"`supported_protocols`, or a v3 protocol block) — fix the "
                f"agent's wire shape rather than downgrading to v2.",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
            )

        raise ADCPError(
            f"Failed to fetch capabilities: {result.error or result.message}",
            agent_id=self.agent_config.id,
            agent_uri=self.agent_config.agent_uri,
        )

    def _ensure_resolver(self) -> FeatureResolver:
        """Return the FeatureResolver, raising if capabilities haven't been fetched."""
        if self._feature_resolver is None:
            raise ADCPError(
                "Cannot check feature support: capabilities have not been fetched. "
                "Call fetch_capabilities() first.",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
            )
        return self._feature_resolver

    def supports(self, feature: str) -> bool:
        """Check if the seller supports a feature.

        Supports multiple feature namespaces:
        - Protocol support: ``supports("media_buy")`` checks ``supported_protocols``
        - Extension support: ``supports("ext:scope3")`` checks ``extensions_supported``
        - Targeting: ``supports("targeting.geo_countries")`` checks
          ``media_buy.execution.targeting``
        - Media buy features: ``supports("audience_targeting")`` checks
          ``media_buy.features``
        - Signals features: ``supports("catalog_signals")`` checks
          ``signals.features``

        Args:
            feature: Feature identifier to check.

        Returns:
            True if the seller declares the feature as supported.

        Raises:
            ADCPError: If capabilities have not been fetched yet.
        """
        return self._ensure_resolver().supports(feature)

    def require(self, *features: str) -> None:
        """Assert that the seller supports all listed features.

        Args:
            *features: Feature identifiers to require.

        Raises:
            ADCPFeatureUnsupportedError: If any features are not supported.
            ADCPError: If capabilities have not been fetched yet.
        """
        self._ensure_resolver().require(
            *features,
            agent_id=self.agent_config.id,
            agent_uri=self.agent_config.agent_uri,
        )

    def _validate_task_features(self, task_name: str) -> None:
        """Check feature requirements for a task if validate_features is enabled.

        Returns without checking if validate_features is False or capabilities
        haven't been fetched yet (logs a warning in the latter case).
        """
        if not self.validate_features:
            return
        if self._feature_resolver is None:
            logger.warning(
                "validate_features is enabled but capabilities have not been fetched. "
                "Call fetch_capabilities() to enable feature validation."
            )
            return
        required_feature = TASK_FEATURE_MAP.get(task_name)
        if required_feature is None:
            return
        self.require(required_feature)

    def _remember_canonical_product_routes(self, products: list[Any]) -> None:
        """Retain bounded same-client canonical-to-legacy routes from discovery."""

        for product in products:
            product_id = getattr(product, "product_id", None)
            for declaration in getattr(product, "format_options", None) or []:
                option_id = getattr(declaration, "format_option_id", None)
                if not option_id:
                    continue
                refs = getattr(declaration, "legacy_format_refs", ())
                if not refs:
                    continue
                route = [ref.model_dump(mode="json") for ref in refs]
                publisher = getattr(declaration, "publisher_domain", None)
                self._canonical_legacy_routes[(product_id, publisher, option_id)] = route
                global_key = (None, publisher, option_id)
                existing = self._canonical_legacy_routes.get(global_key)
                if global_key not in self._canonical_legacy_routes:
                    self._canonical_legacy_routes[global_key] = route
                elif existing != route:
                    self._canonical_legacy_routes[global_key] = None

    def _creative_dialect(
        self, request: Any = None, *, legacy_projection_available: bool = False
    ) -> CreativeDialect:
        """Resolve this call's wire dialect from version, capability, and request evidence."""

        request_version = None
        if isinstance(request, dict):
            request_version = request.get("adcp_version")
        elif request is not None:
            request_version = getattr(request, "adcp_version", None)
        version = request_version or self._server_version or self._adcp_version
        if not str(version).startswith("3."):
            return CreativeDialect.LEGACY
        return resolve_creative_dialect(
            version,
            capabilities=self._capabilities,
            request=request,
            legacy_projection_available=legacy_projection_available,
        )

    def _callback_creative_dialect(self, result: Any) -> CreativeDialect:
        """Resolve callbacks from payload evidence, canonical-first when neutral."""

        try:
            return self._creative_dialect(result)
        except CreativeDialectError:
            return CreativeDialect.CANONICAL

    def _legacy_refs_for_option(
        self,
        option: dict[str, Any],
        *,
        product_id: str | None,
        field: str,
    ) -> list[dict[str, Any]]:
        option_id = option.get("format_option_id")
        publisher = option.get("publisher_domain")
        if not isinstance(option_id, str):
            raise CanonicalFormatLegacyResolutionError(f"{field} has no format_option_id")
        route = self._canonical_legacy_routes.get((product_id, publisher, option_id))
        if route is None:
            route = self._canonical_legacy_routes.get((None, publisher, option_id))
        if route is None:
            raise CanonicalFormatLegacyResolutionError(
                f"no discovered legacy route for {field}; rediscover the product or configure "
                "canonical_format_legacy_resolver"
            )
        return [dict(ref) for ref in route]

    def _legacy_refs_for_declaration(
        self,
        declaration: dict[str, Any],
        *,
        product_id: str | None,
        field: str,
    ) -> list[dict[str, Any]]:
        option_id = declaration.get("format_option_id")
        if isinstance(option_id, str):
            try:
                return self._legacy_refs_for_option(
                    declaration,
                    product_id=product_id,
                    field=field,
                )
            except CanonicalFormatLegacyResolutionError:
                pass
        canonical = Format.model_validate(declaration)
        return [
            ref.model_dump(mode="json")
            for ref in resolve_legacy_format_refs(
                canonical,
                resolver=self.canonical_format_legacy_resolver,
                product_id=product_id,
                field=field,
            )
        ]

    def _downgrade_package(self, package: dict[str, Any], *, field: str) -> None:
        product_id = package.get("product_id")
        product_id = product_id if isinstance(product_id, str) else None
        refs = package.pop("format_option_refs", None)
        if refs:
            legacy: list[dict[str, Any]] = []
            for index, option in enumerate(refs):
                legacy.extend(
                    self._legacy_refs_for_option(
                        option,
                        product_id=product_id,
                        field=f"{field}.format_option_refs[{index}]",
                    )
                )
            package["format_ids"] = legacy
        elif package.get("format_kind") is not None:
            declaration = {
                "format_kind": package.pop("format_kind"),
                "params": package.pop("params", None) or {},
            }
            package["format_ids"] = self._legacy_refs_for_declaration(
                declaration,
                product_id=product_id,
                field=f"{field}.format_kind",
            )
        else:
            package.pop("params", None)

        for index, creative in enumerate(package.get("creatives") or []):
            self._downgrade_creative(
                creative,
                field=f"{field}.creatives[{index}]",
                product_id=product_id,
            )

    def _downgrade_creative(
        self,
        creative: dict[str, Any],
        *,
        field: str,
        product_id: str | None = None,
    ) -> None:
        option = creative.pop("format_option_ref", None)
        if option:
            refs = self._legacy_refs_for_option(
                option, product_id=product_id, field=f"{field}.format_option_ref"
            )
        else:
            declaration = {
                "format_kind": creative.pop("format_kind"),
                "params": creative.pop("params", None) or {},
            }
            refs = self._legacy_refs_for_declaration(
                declaration, product_id=product_id, field=field
            )
        if len(refs) != 1:
            raise CanonicalFormatLegacyResolutionError(
                f"{field} resolves to {len(refs)} legacy formats; a creative requires exactly one"
            )
        creative["format_id"] = refs[0]

    def _prepare_creative_params(self, request: BaseModel) -> dict[str, Any]:
        """Serialize canonical input and deterministically adapt legacy peers."""

        params = request.model_dump(mode="json", exclude_none=True)
        if strip_legacy_creative_identity(params) != params:
            raise ValueError(
                "primary creative methods reject legacy format identity; use the explicit "
                "*_legacy method"
            )
        if self._creative_dialect(request) is CreativeDialect.CANONICAL:
            return params
        for collection in ("packages", "new_packages"):
            for index, package in enumerate(params.get(collection) or []):
                self._downgrade_package(package, field=f"{collection}[{index}]")
        for index, creative in enumerate(params.get("creatives") or []):
            self._downgrade_creative(creative, field=f"creatives[{index}]")
        return params

    def _canonicalize_get_products_result(
        self,
        raw_result: TaskResult[Any],
    ) -> TaskResult[GetProductsResponse]:
        """Parse the wire shape, project products, and enforce the primary boundary."""

        # Canonical responses must be parsed before the legacy compatibility
        # model. The generated legacy ProductFormatDeclaration intentionally
        # lacks canonical ``format_kind`` and ``params`` fields, so parsing a
        # canonical-only response through it first irreversibly discards the
        # declaration before ``project_legacy_product`` can inspect it.
        canonical_result: TaskResult[GetProductsResponse] = self.adapter._parse_response(
            raw_result, GetProductsResponse
        )
        if not raw_result.success or raw_result.data is None:
            return canonical_result
        if canonical_result.success and canonical_result.data is not None:
            direct_products = list(canonical_result.data.products or [])
            self._remember_canonical_product_routes(direct_products)
            metadata = dict(canonical_result.metadata or {})
            metadata["projection"] = {"diagnostics": []}
            canonical_result.metadata = metadata
            return canonical_result

        legacy_result: TaskResult[Any] = self.adapter._parse_response(
            raw_result, LegacyGetProductsResponse
        )
        if not legacy_result.success or legacy_result.data is None:
            return cast(TaskResult[GetProductsResponse], legacy_result)

        body = legacy_result.data.model_dump(mode="json")
        canonical_products: list[Product] = []
        diagnostics: list[dict[str, Any]] = []
        portable_errors = list(body.get("errors") or [])
        for raw_product in body.get("products") or []:
            projected = project_legacy_product(
                raw_product,
                legacy_format_converter=self.legacy_format_converter,
            )
            if projected.product is not None:
                canonical_products.append(projected.product)
            for diagnostic in projected.diagnostics:
                dumped = diagnostic.model_dump()
                diagnostics.append(dumped)
                details = dumped["error"]["details"]
                portable_errors.append(
                    {
                        "code": diagnostic.code,
                        "message": (
                            "Product has no format declaration representable on the "
                            "canonical-only surface"
                            if diagnostic.code == "CANONICAL_PRODUCT_FORMATS_UNAVAILABLE"
                            else "Legacy creative format could not be projected to a "
                            "canonical declaration"
                        ),
                        "field": diagnostic.field,
                        "recovery": "correctable",
                        "source": "sdk",
                        "sdk_id": dumped["sdk_id"],
                        "details": details,
                    }
                )

        body["products"] = canonical_products
        if portable_errors:
            body["errors"] = portable_errors
        try:
            canonical = GetProductsResponse.model_validate(body)
        except ValueError as exc:
            return TaskResult[GetProductsResponse](
                status=TaskStatus.FAILED,
                success=False,
                error=f"Failed to project canonical get_products response: {exc}",
                message=legacy_result.message,
                metadata=legacy_result.metadata,
                debug_info=legacy_result.debug_info,
                idempotency_key=legacy_result.idempotency_key,
                replayed=legacy_result.replayed,
            )

        self._remember_canonical_product_routes(canonical_products)
        metadata = dict(legacy_result.metadata or {})
        metadata["projection"] = {"diagnostics": diagnostics}
        return TaskResult[GetProductsResponse](
            status=legacy_result.status,
            data=canonical,
            message=legacy_result.message,
            success=True,
            error=legacy_result.error,
            metadata=metadata,
            debug_info=legacy_result.debug_info,
            idempotency_key=legacy_result.idempotency_key,
            replayed=legacy_result.replayed,
        )

    def _canonicalize_format_read_result(
        self,
        raw_result: TaskResult[Any],
        *,
        legacy_type: Any,
        canonical_type: Any,
        collection: str,
        require_format: bool,
    ) -> TaskResult[Any]:
        """Upgrade legacy creative rows through the same projector used by discovery."""

        legacy_result = self.adapter._parse_response(raw_result, legacy_type)
        if not legacy_result.success or legacy_result.data is None:
            return legacy_result
        body = legacy_result.data.model_dump(mode="json")
        converted: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        errors = list(body.get("errors") or [])
        for index, raw_item in enumerate(body.get(collection) or []):
            item = dict(raw_item)
            legacy_id = item.pop("format_id", None)
            if item.get("format_kind") is None and legacy_id is not None:
                projection = project_legacy_format_id(
                    legacy_id,
                    product_id=str(item.get("media_buy_id") or ""),
                    field=f"{collection}[{index}].format_id",
                    legacy_format_converter=self.legacy_format_converter,
                )
                if projection.declaration is not None:
                    item["format_kind"] = projection.declaration.format_kind.value
                elif projection.diagnostic is not None:
                    diagnostic = projection.diagnostic.model_dump()
                    diagnostics.append(diagnostic)
                    errors.append(
                        {
                            "code": "FORMAT_PROJECTION_FAILED",
                            "message": "Legacy creative format could not be projected.",
                            "field": f"{collection}[{index}].format_id",
                            "severity": "warning",
                            "source": "sdk",
                            "details": diagnostic["error"]["details"],
                        }
                    )
            if require_format and item.get("format_kind") is None:
                continue
            converted.append(item)
        body[collection] = converted
        if errors:
            body["errors"] = errors
        try:
            canonical = canonical_type.model_validate(body)
        except ValueError as exc:
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                success=False,
                error=f"Failed to project canonical {collection} response: {exc}",
                message=legacy_result.message,
                metadata=legacy_result.metadata,
            )
        metadata = dict(legacy_result.metadata or {})
        metadata["projection"] = {"diagnostics": diagnostics}
        return TaskResult[Any](
            status=legacy_result.status,
            data=canonical,
            message=legacy_result.message,
            success=True,
            metadata=metadata,
            debug_info=legacy_result.debug_info,
            idempotency_key=legacy_result.idempotency_key,
            replayed=legacy_result.replayed,
        )

    def _canonicalize_lifecycle_result(
        self,
        raw_result: TaskResult[Any],
        *,
        legacy_type: Any,
        canonical_type: Any,
    ) -> TaskResult[Any]:
        """Upgrade legacy package/creative selectors on lifecycle responses."""

        legacy_result = self.adapter._parse_response(raw_result, legacy_type)
        if not legacy_result.success or legacy_result.data is None:
            return legacy_result
        try:
            body = normalize_legacy_creative_request(
                legacy_result.data.model_dump(mode="json"),
                legacy_format_converter=self.legacy_format_converter,
            )
            canonical = TypeAdapter(canonical_type).validate_python(body)
        except (LegacyCreativeProjectionError, ValueError) as exc:
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                success=False,
                error=f"Failed to project canonical lifecycle response: {exc}",
                message=legacy_result.message,
                metadata=legacy_result.metadata,
            )
        return TaskResult[Any](
            status=legacy_result.status,
            data=canonical,
            message=legacy_result.message,
            success=True,
            metadata=legacy_result.metadata,
            debug_info=legacy_result.debug_info,
            idempotency_key=legacy_result.idempotency_key,
            replayed=legacy_result.replayed,
        )

    async def _execute_typed_task(
        self,
        task_type: str,
        request: BaseModel,
        response_type: type[BaseModel] | Any,
    ) -> TaskResult[Any]:
        """Execute and parse one typed AdCP task with activity events."""
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)
        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=task_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        method = getattr(self.adapter, task_type)
        raw_result = await method(params)
        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=task_type,
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        parsed_result = self.adapter._parse_response(raw_result, response_type)
        if task_type == "refine_proposals" and isinstance(raw_result.data, Mapping):
            metadata = dict(parsed_result.metadata or {})
            metadata[WIRE_RESPONSE_METADATA_KEY] = raw_result.data
            parsed_result = parsed_result.model_copy(update={"metadata": metadata})
        return parsed_result

    async def list_products(self, request: ListProductsRequest) -> TaskResult[ListProductsResponse]:
        """List products using the AdCP 3.2 compact discovery lifecycle."""
        return cast(
            TaskResult[ListProductsResponse],
            await self._execute_typed_task("list_products", request, ListProductsResponse),
        )

    async def request_proposals(
        self, request: RequestProposalsRequest
    ) -> TaskResult[RequestProposalsResponse]:
        """Request seller proposals for selected products."""
        return cast(
            TaskResult[RequestProposalsResponse],
            await self._execute_typed_task("request_proposals", request, RequestProposalsResponse),
        )

    async def refine_proposals(
        self, request: RefineProposalsRequest
    ) -> TaskResult[RefineProposalsResponse]:
        """Refine one or more seller proposals."""
        return cast(
            TaskResult[RefineProposalsResponse],
            await self._execute_typed_task("refine_proposals", request, RefineProposalsResponse),
        )

    async def refine_proposals_verified(
        self,
        request: RefineProposalsRequest,
        proposal_refinement: BaseModel | Mapping[str, Any] | None = None,
        *,
        source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> VerifiedRefinementResult:
        """Preflight, execute, and verify a proposal-refinement request.

        ``proposal_refinement`` is the seller's advertised capability block.
        Unsupported typed dimensions fail before transport. Completed success
        responses are checked for ordering, lineage, constraints, alternatives,
        expiry shape, and exact RFC 8785 terms digests before being returned.
        """

        return await _refine_proposals_verified(
            self,
            request,
            proposal_refinement,
            source_proposals=source_proposals,
            now=now,
        )

    async def wait_for_refinement_verified(
        self,
        request: RefineProposalsRequest,
        initial: VerifiedRefinementResult,
        proposal_refinement: BaseModel | Mapping[str, Any] | None = None,
        *,
        source_proposals: Mapping[str, BaseModel | Mapping[str, Any]] | None = None,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        now: datetime | None = None,
    ) -> VerifiedRefinementResult:
        """Poll a submitted refinement and verify its exact terminal payload."""

        if initial.valid or not initial.pending:
            return initial
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        raw = (initial.task_result.metadata or {}).get(WIRE_RESPONSE_METADATA_KEY)
        task_id = raw.get("task_id") if isinstance(raw, Mapping) else None
        if not isinstance(task_id, str):
            data = initial.task_result.data
            task_id = getattr(data, "task_id", None)
        if not isinstance(task_id, str):
            raise ValueError("submitted refinement result is missing task_id")

        deadline = time.monotonic() + timeout
        while True:
            polled = await self.get_task_status(
                GetTaskStatusRequest(task_id=task_id, include_result=True)
            )
            status_data = polled.data
            status = getattr(status_data, "status", None)
            status_value = getattr(status, "value", status)
            if status_value in {"completed", "failed", "rejected", "canceled"}:
                terminal = getattr(status_data, "result", None)
                error = getattr(status_data, "error", None)
                adcp_error = (
                    error.model_dump(mode="json", exclude_none=True)
                    if isinstance(error, BaseModel)
                    else error
                )
                task_status = (
                    TaskStatus.COMPLETED if status_value == "completed" else TaskStatus.FAILED
                )
                terminal_result = TaskResult[Any](
                    status=task_status,
                    success=status_value == "completed",
                    data=terminal,
                    adcp_error=adcp_error if isinstance(adcp_error, dict) else None,
                    metadata={
                        "task_id": task_id,
                        WIRE_RESPONSE_METADATA_KEY: terminal,
                    },
                )
                return _verify_refinement_result(
                    request,
                    terminal_result,
                    proposal_refinement=proposal_refinement,
                    source_proposals=source_proposals,
                    now=now,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"refinement task {task_id!r} did not complete in time")
            await asyncio.sleep(min(poll_interval, remaining))

    async def decline_proposals(
        self, request: DeclineProposalsRequest
    ) -> TaskResult[DeclineProposalsResponse]:
        """Decline one or more seller proposals."""
        return cast(
            TaskResult[DeclineProposalsResponse],
            await self._execute_typed_task("decline_proposals", request, DeclineProposalsResponse),
        )

    async def buy_products(self, request: BuyProductsRequest) -> TaskResult[BuyProductsResponse]:
        """Commit a direct product purchase."""
        return cast(
            TaskResult[BuyProductsResponse],
            await self._execute_typed_task("buy_products", request, BuyProductsResponse),
        )

    async def accept_proposal(
        self, request: AcceptProposalRequest
    ) -> TaskResult[AcceptProposalResponse]:
        """Accept a seller proposal and create its media buy."""
        return cast(
            TaskResult[AcceptProposalResponse],
            await self._execute_typed_task("accept_proposal", request, AcceptProposalResponse),
        )

    async def control_media_buy(
        self, request: ControlMediaBuyRequest
    ) -> TaskResult[ControlMediaBuyResponse]:
        """Apply lifecycle controls to an existing media buy."""
        return cast(
            TaskResult[ControlMediaBuyResponse],
            await self._execute_typed_task("control_media_buy", request, ControlMediaBuyResponse),
        )

    async def get_products(
        self,
        request: GetProductsRequest,
        fetch_previews: bool = False,
        preview_output_format: str = "url",
        creative_agent_client: ADCPClient | None = None,
    ) -> TaskResult[GetProductsResponse]:
        """
        Get advertising products.

        Args:
            request: Request parameters
            fetch_previews: If True, generate preview URLs for each product's formats
                (uses batch API for 5-10x performance improvement)
            preview_output_format: "url" for iframe URLs (default), "html" for direct
                embedding (2-3x faster, no iframe overhead)
            creative_agent_client: Client for creative agent (required if
                fetch_previews=True)

        Returns:
            TaskResult containing GetProductsResponse with optional preview URLs in metadata

        Raises:
            ValueError: If fetch_previews=True but creative_agent_client is not provided
        """
        if fetch_previews and not creative_agent_client:
            raise ValueError("creative_agent_client is required when fetch_previews=True")

        self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_products",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_products(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_products",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = self._canonicalize_get_products_result(raw_result)

        if (
            fetch_previews
            and result.success
            and result.data
            and result.data.products
            and creative_agent_client
        ):
            from adcp.utils.preview_cache import add_preview_urls_to_products

            products_with_previews = await add_preview_urls_to_products(
                result.data.products,
                creative_agent_client,
                use_batch=True,
                output_format=preview_output_format,
            )
            result.metadata = result.metadata or {}
            result.metadata["products_with_previews"] = products_with_previews

        return result

    async def get_products_legacy(
        self,
        request: LegacyGetProductsRequest,
    ) -> TaskResult[LegacyGetProductsResponse]:
        """Return the raw AdCP 3.x product wire shape for migration tooling."""

        self._warn_legacy_creative_api("get_products_legacy")
        raw_result = await self.adapter.get_products(request.model_dump(mode="json"))
        return self.adapter._parse_response(raw_result, LegacyGetProductsResponse)

    @staticmethod
    def _warn_legacy_creative_api(method: str) -> None:
        warnings.warn(
            f"{method} exposes legacy named-format identity and will be removed with AdCP 4.0",
            DeprecationWarning,
            stacklevel=3,
        )

    async def list_creative_formats_legacy(
        self,
        request: ListCreativeFormatsRequest,
        fetch_previews: bool = False,
        preview_output_format: str = "url",
    ) -> TaskResult[ListCreativeFormatsResponse]:
        """
        List supported creative formats.

        Args:
            request: Request parameters
            fetch_previews: If True, generate preview URLs for each format using
                sample manifests (uses batch API for 5-10x performance improvement)
            preview_output_format: "url" for iframe URLs (default), "html" for direct
                embedding (2-3x faster, no iframe overhead)

        Returns:
            TaskResult containing ListCreativeFormatsResponse with optional preview URLs in metadata
        """
        self._warn_legacy_creative_api("list_creative_formats_legacy")
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creative_formats",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_creative_formats(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creative_formats",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result: TaskResult[ListCreativeFormatsResponse] = self.adapter._parse_response(
            raw_result, ListCreativeFormatsResponse
        )

        if fetch_previews and result.success and result.data:
            from adcp.utils.preview_cache import add_preview_urls_to_formats

            formats_with_previews = await add_preview_urls_to_formats(
                result.data.formats,
                self,
                use_batch=True,
                output_format=preview_output_format,
            )
            result.metadata = result.metadata or {}
            result.metadata["formats_with_previews"] = formats_with_previews

        return result

    async def create_media_buy_legacy(
        self, request: LegacyCreateMediaBuyRequest
    ) -> TaskResult[LegacyCreateMediaBuyResponse]:
        """Execute create_media_buy without the canonical application boundary."""

        self._warn_legacy_creative_api("create_media_buy_legacy")
        raw = await self.adapter.create_media_buy(
            request.model_dump(mode="json", exclude_none=True)
        )
        return self.adapter._parse_response(raw, LegacyCreateMediaBuyResponse)

    async def update_media_buy_legacy(
        self, request: LegacyUpdateMediaBuyRequest
    ) -> TaskResult[LegacyUpdateMediaBuyResponse]:
        """Execute update_media_buy without the canonical application boundary."""

        self._warn_legacy_creative_api("update_media_buy_legacy")
        raw = await self.adapter.update_media_buy(
            request.model_dump(mode="json", exclude_none=True)
        )
        return self.adapter._parse_response(raw, LegacyUpdateMediaBuyResponse)

    async def sync_creatives_legacy(
        self, request: LegacySyncCreativesRequest
    ) -> TaskResult[LegacySyncCreativesResponse]:
        """Execute sync_creatives without the canonical application boundary."""

        self._warn_legacy_creative_api("sync_creatives_legacy")
        raw = await self.adapter.sync_creatives(request.model_dump(mode="json", exclude_none=True))
        return self.adapter._parse_response(raw, LegacySyncCreativesResponse)

    async def list_creatives_legacy(
        self, request: LegacyListCreativesRequest
    ) -> TaskResult[LegacyListCreativesResponse]:
        """Return raw creative rows carrying legacy format identity."""

        self._warn_legacy_creative_api("list_creatives_legacy")
        raw = await self.adapter.list_creatives(request.model_dump(mode="json", exclude_none=True))
        return self.adapter._parse_response(raw, LegacyListCreativesResponse)

    async def get_media_buys_legacy(
        self, request: GetMediaBuysRequest
    ) -> TaskResult[LegacyGetMediaBuysResponse]:
        """Return raw media-buy rows carrying legacy format identity."""

        self._warn_legacy_creative_api("get_media_buys_legacy")
        raw = await self.adapter.get_media_buys(request.model_dump(mode="json", exclude_none=True))
        return self.adapter._parse_response(raw, LegacyGetMediaBuysResponse)

    async def get_media_buy_delivery_legacy(
        self, request: GetMediaBuyDeliveryRequest
    ) -> TaskResult[LegacyGetMediaBuyDeliveryResponse]:
        """Return raw media-buy delivery carrying legacy format identity."""

        self._warn_legacy_creative_api("get_media_buy_delivery_legacy")
        raw = await self.adapter.get_media_buy_delivery(
            request.model_dump(mode="json", exclude_none=True)
        )
        return self.adapter._parse_response(raw, LegacyGetMediaBuyDeliveryResponse)

    async def get_creative_delivery_legacy(
        self, request: GetCreativeDeliveryRequest
    ) -> TaskResult[LegacyGetCreativeDeliveryResponse]:
        """Return raw creative delivery carrying legacy format identity."""

        self._warn_legacy_creative_api("get_creative_delivery_legacy")
        raw = await self.adapter.get_creative_delivery(
            request.model_dump(mode="json", exclude_none=True)
        )
        return self.adapter._parse_response(raw, LegacyGetCreativeDeliveryResponse)

    async def preview_creative_legacy(
        self,
        request: LegacyPreviewCreativeRequest,
    ) -> TaskResult[LegacyPreviewCreativeResponse]:
        """
        Generate preview of a creative manifest.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing PreviewCreativeResponse with preview URLs
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="preview_creative",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.preview_creative(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="preview_creative",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        self._warn_legacy_creative_api("preview_creative_legacy")
        return self.adapter._parse_response(raw_result, LegacyPreviewCreativeResponse)

    async def sync_creatives(
        self,
        request: SyncCreativesRequest,
    ) -> TaskResult[SyncCreativesResponse]:
        """
        Sync Creatives.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing SyncCreativesResponse
        """
        dialect = self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = self._prepare_creative_params(request)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_creatives",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_creatives(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_creatives",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if dialect is CreativeDialect.LEGACY:
            return cast(
                TaskResult[SyncCreativesResponse],
                self._canonicalize_lifecycle_result(
                    raw_result,
                    legacy_type=LegacySyncCreativesResponse,
                    canonical_type=SyncCreativesResponse,
                ),
            )
        return self.adapter._parse_response(raw_result, SyncCreativesResponse)

    async def list_creatives(
        self,
        request: ListCreativesRequest,
    ) -> TaskResult[ListCreativesResponse]:
        """
        List Creatives.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ListCreativesResponse
        """
        dialect = self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creatives",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_creatives(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creatives",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if dialect is CreativeDialect.CANONICAL:
            return self.adapter._parse_response(raw_result, ListCreativesResponse)
        return cast(
            TaskResult[ListCreativesResponse],
            self._canonicalize_format_read_result(
                raw_result,
                legacy_type=LegacyListCreativesResponse,
                canonical_type=ListCreativesResponse,
                collection="creatives",
                require_format=True,
            ),
        )

    async def get_media_buy_delivery(
        self,
        request: GetMediaBuyDeliveryRequest,
    ) -> TaskResult[GetMediaBuyDeliveryResponse]:
        """
        Get Media Buy Delivery.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetMediaBuyDeliveryResponse
        """
        dialect = self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_delivery",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_media_buy_delivery(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_delivery",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if dialect is CreativeDialect.LEGACY:
            return cast(
                TaskResult[GetMediaBuyDeliveryResponse],
                self._canonicalize_lifecycle_result(
                    raw_result,
                    legacy_type=LegacyGetMediaBuyDeliveryResponse,
                    canonical_type=GetMediaBuyDeliveryResponse,
                ),
            )
        return self.adapter._parse_response(raw_result, GetMediaBuyDeliveryResponse)

    async def get_media_buys(
        self,
        request: GetMediaBuysRequest,
    ) -> TaskResult[GetMediaBuysResponse]:
        """
        Get Media Buys.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetMediaBuysResponse
        """
        dialect = self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)
        if params.get("include_webhook_activity") is False:
            params.pop("include_webhook_activity")
        if params.get("webhook_activity_limit") == 50:
            params.pop("webhook_activity_limit")

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buys",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_media_buys(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buys",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if dialect is CreativeDialect.LEGACY:
            return cast(
                TaskResult[GetMediaBuysResponse],
                self._canonicalize_lifecycle_result(
                    raw_result,
                    legacy_type=LegacyGetMediaBuysResponse,
                    canonical_type=GetMediaBuysResponse,
                ),
            )
        return self.adapter._parse_response(raw_result, GetMediaBuysResponse)

    async def get_signals(
        self,
        request: GetSignalsRequest,
    ) -> TaskResult[GetSignalsResponse]:
        """
        Get Signals.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetSignalsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_signals",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_signals(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_signals",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetSignalsResponse)

    async def activate_signal(
        self,
        request: ActivateSignalRequest,
    ) -> TaskResult[ActivateSignalResponse]:
        """
        Activate Signal.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ActivateSignalResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="activate_signal",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.activate_signal(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="activate_signal",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ActivateSignalResponse)

    async def provide_performance_feedback(
        self,
        request: ProvidePerformanceFeedbackRequest,
    ) -> TaskResult[ProvidePerformanceFeedbackResponse]:
        """
        Provide Performance Feedback.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ProvidePerformanceFeedbackResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="provide_performance_feedback",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.provide_performance_feedback(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="provide_performance_feedback",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ProvidePerformanceFeedbackResponse)

    async def create_media_buy(
        self,
        request: CreateMediaBuyRequest,
    ) -> TaskResult[CreateMediaBuyResponse]:
        """
        Create a new media buy reservation.

        Requests the agent to reserve inventory for a campaign. The agent returns a
        media_buy_id that tracks this reservation and can be used for updates.

        Args:
            request: Media buy creation parameters including:
                - brand: Brand reference; resolved from brand.json or the registry at execution
                - packages: List of package requests specifying desired inventory
                - publisher_properties: Target properties for ad placement
                - budget: Optional budget constraints
                - start_date/end_date: Campaign flight dates

        Returns:
            TaskResult containing CreateMediaBuyResponse with:
                - media_buy_id: Unique identifier for this reservation
                - status: Current state of the media buy
                - packages: Confirmed package details
                - Additional platform-specific metadata

        Example:
            >>> from adcp import ADCPClient, CreateMediaBuyRequest, BrandReference
            >>> client = ADCPClient(agent_config)
            >>> request = CreateMediaBuyRequest(
            ...     brand=BrandReference(domain="acme.com"),
            ...     packages=[package_request],
            ...     publisher_properties=properties,
            ... )
            >>> result = await client.create_media_buy(request)
            >>> if result.success:
            ...     media_buy_id = result.data.media_buy_id
        """
        dialect = self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = self._prepare_creative_params(request)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_media_buy",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.create_media_buy(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_media_buy",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if dialect is CreativeDialect.LEGACY:
            return cast(
                TaskResult[CreateMediaBuyResponse],
                self._canonicalize_lifecycle_result(
                    raw_result,
                    legacy_type=LegacyCreateMediaBuyResponse,
                    canonical_type=CreateMediaBuyResponse,
                ),
            )
        return self.adapter._parse_response(raw_result, CreateMediaBuyResponse)

    async def update_media_buy(
        self,
        request: UpdateMediaBuyRequest,
    ) -> TaskResult[UpdateMediaBuyResponse]:
        """
        Update an existing media buy reservation.

        Modifies a previously created media buy by updating packages or publisher
        properties. The update operation uses discriminated unions to specify what
        to change - either package details or targeting properties.

        Args:
            request: Media buy update parameters including:
                - media_buy_id: Identifier from create_media_buy response
                - updates: Discriminated union specifying update type:
                    * UpdateMediaBuyPackagesRequest: Modify package selections
                    * UpdateMediaBuyPropertiesRequest: Change targeting properties

        Returns:
            TaskResult containing UpdateMediaBuyResponse with:
                - media_buy_id: The updated media buy identifier
                - status: Updated state of the media buy
                - packages: Updated package configurations
                - Additional platform-specific metadata

        Example:
            >>> from adcp import ADCPClient, UpdateMediaBuyPackagesRequest
            >>> client = ADCPClient(agent_config)
            >>> request = UpdateMediaBuyPackagesRequest(
            ...     media_buy_id="mb_123",
            ...     packages=[updated_package]
            ... )
            >>> result = await client.update_media_buy(request)
            >>> if result.success:
            ...     updated_packages = result.data.packages
        """
        dialect = self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = self._prepare_creative_params(request)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_media_buy",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_media_buy(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_media_buy",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if dialect is CreativeDialect.LEGACY:
            return cast(
                TaskResult[UpdateMediaBuyResponse],
                self._canonicalize_lifecycle_result(
                    raw_result,
                    legacy_type=LegacyUpdateMediaBuyResponse,
                    canonical_type=UpdateMediaBuyResponse,
                ),
            )
        return self.adapter._parse_response(raw_result, UpdateMediaBuyResponse)

    async def build_creative_legacy(
        self,
        request: LegacyBuildCreativeRequest,
    ) -> TaskResult[LegacyBuildCreativeResponse]:
        """
        Generate production-ready creative assets.

        Requests the creative agent to build final deliverable assets in the target
        format (e.g., VAST, DAAST, HTML5). This is typically called after previewing
        and approving a creative manifest.

        Args:
            request: Creative build parameters including:
                - manifest: Creative manifest with brand info and content
                - target_format_id: Desired output format identifier
                - inputs: Optional user-provided inputs for template variables
                - deployment: Platform or agent deployment configuration

        Returns:
            TaskResult containing BuildCreativeResponse with:
                - assets: Production-ready creative files (URLs or inline content)
                - format_id: The generated format identifier
                - manifest: The creative manifest used for generation
                - metadata: Additional platform-specific details

        Example:
            >>> from adcp import ADCPClient, LegacyBuildCreativeRequest
            >>> client = ADCPClient(agent_config)
            >>> request = LegacyBuildCreativeRequest(
            ...     manifest=creative_manifest,
            ...     target_format_id="vast_2.0",
            ...     inputs={"duration": 30}
            ... )
            >>> result = await client.build_creative_legacy(request)
            >>> if result.success:
            ...     vast_url = result.data.assets[0].url
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="build_creative",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.build_creative(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="build_creative",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        self._warn_legacy_creative_api("build_creative_legacy")
        return self.adapter._parse_response(raw_result, LegacyBuildCreativeResponse)

    async def list_accounts(
        self,
        request: ListAccountsRequest,
    ) -> TaskResult[ListAccountsResponse]:
        """
        List Accounts.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ListAccountsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_accounts",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_accounts(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_accounts",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListAccountsResponse)

    async def sync_accounts(
        self,
        request: SyncAccountsRequest,
    ) -> TaskResult[SyncAccountsResponse]:
        """
        Sync Accounts.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing SyncAccountsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_accounts",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_accounts(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_accounts",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SyncAccountsResponse)

    async def get_account_financials(
        self,
        request: GetAccountFinancialsRequest,
    ) -> TaskResult[GetAccountFinancialsResponse]:
        """
        Get Account Financials.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetAccountFinancialsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_account_financials",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_account_financials(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_account_financials",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetAccountFinancialsResponse)

    async def report_usage(
        self,
        request: ReportUsageRequest,
    ) -> TaskResult[ReportUsageResponse]:
        """
        Report Usage.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ReportUsageResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="report_usage",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.report_usage(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="report_usage",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ReportUsageResponse)

    async def log_event(
        self,
        request: LogEventRequest,
    ) -> TaskResult[LogEventResponse]:
        """
        Log Event.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing LogEventResponse
        """
        self._validate_task_features("log_event")
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="log_event",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.log_event(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="log_event",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, LogEventResponse)

    async def sync_event_sources(
        self,
        request: SyncEventSourcesRequest,
    ) -> TaskResult[SyncEventSourcesResponse]:
        """
        Sync Event Sources.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing SyncEventSourcesResponse
        """
        self._validate_task_features("sync_event_sources")
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_event_sources",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_event_sources(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_event_sources",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SyncEventSourcesResponse)

    async def sync_audiences(
        self,
        request: SyncAudiencesRequest,
    ) -> TaskResult[SyncAudiencesResponse]:
        """
        Sync Audiences.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing SyncAudiencesResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_audiences",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_audiences(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_audiences",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SyncAudiencesResponse)

    async def sync_catalogs(
        self,
        request: SyncCatalogsRequest,
    ) -> TaskResult[SyncCatalogsResponse]:
        """
        Sync Catalogs.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing SyncCatalogsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_catalogs",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_catalogs(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_catalogs",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SyncCatalogsResponse)

    async def get_creative_delivery(
        self,
        request: GetCreativeDeliveryRequest,
    ) -> TaskResult[GetCreativeDeliveryResponse]:
        """
        Get Creative Delivery.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetCreativeDeliveryResponse
        """
        self._creative_dialect(request, legacy_projection_available=True)
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_creative_delivery",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_creative_delivery(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_creative_delivery",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        if (
            self._creative_dialect(request, legacy_projection_available=True)
            is CreativeDialect.CANONICAL
        ):
            return self.adapter._parse_response(raw_result, GetCreativeDeliveryResponse)
        return cast(
            TaskResult[GetCreativeDeliveryResponse],
            self._canonicalize_format_read_result(
                raw_result,
                legacy_type=LegacyGetCreativeDeliveryResponse,
                canonical_type=GetCreativeDeliveryResponse,
                collection="creatives",
                require_format=False,
            ),
        )

    async def list_transformers(
        self,
        request: ListTransformersRequest,
    ) -> TaskResult[ListTransformersResponse]:
        """
        List Creative Transformers.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ListTransformersResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_transformers",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_transformers(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_transformers",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListTransformersResponse)

    # ========================================================================
    # V3 Protocol Methods - Protocol Discovery
    # ========================================================================

    async def get_adcp_capabilities(
        self,
        request: GetAdcpCapabilitiesRequest,
    ) -> TaskResult[GetAdcpCapabilitiesResponse]:
        """
        Get AdCP capabilities from the agent.

        Queries the agent's supported AdCP features, protocol versions, and
        domain-specific capabilities (media_buy, signals, sponsored_intelligence).

        Args:
            request: Request parameters including optional protocol filters

        Returns:
            TaskResult containing GetAdcpCapabilitiesResponse with:
                - adcp: Core protocol version information
                - supported_protocols: List of supported domain protocols
                - media_buy: Media buy capabilities (if supported)
                - sponsored_intelligence: SI capabilities (if supported)
                - signals: Signals capabilities (if supported)
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_adcp_capabilities",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_adcp_capabilities(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_adcp_capabilities",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetAdcpCapabilitiesResponse)

    async def sync_agent_notification_configs(
        self,
        request: SyncAgentNotificationConfigsRequest,
    ) -> TaskResult[SyncAgentNotificationConfigsResponse]:
        """Replace the caller-scoped agent notification subscriber set."""
        return cast(
            TaskResult[SyncAgentNotificationConfigsResponse],
            await self._execute_typed_task(
                "sync_agent_notification_configs",
                request,
                SyncAgentNotificationConfigsResponse,
            ),
        )

    async def get_task_status(
        self,
        request: GetTaskStatusRequest,
    ) -> TaskResult[GetTaskStatusResponse]:
        """
        Get Task Status.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetTaskStatusResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_task_status",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_task_status(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_task_status",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetTaskStatusResponse)

    async def list_tasks(
        self,
        request: ListTasksRequest,
    ) -> TaskResult[ListTasksResponse]:
        """
        List Tasks.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ListTasksResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_tasks",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_tasks(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_tasks",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListTasksResponse)

    # ========================================================================
    # V3 Protocol Methods - Content Standards
    # ========================================================================

    async def create_content_standards(
        self,
        request: CreateContentStandardsRequest,
    ) -> TaskResult[CreateContentStandardsResponse]:
        """
        Create a new content standards configuration.

        Defines acceptable content contexts for ad placement using natural
        language policy and optional calibration exemplars.

        Args:
            request: Request parameters including policy and scope

        Returns:
            TaskResult containing CreateContentStandardsResponse with standards_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.create_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CreateContentStandardsResponse)

    async def get_content_standards(
        self,
        request: GetContentStandardsRequest,
    ) -> TaskResult[GetContentStandardsResponse]:
        """
        Get a content standards configuration by ID.

        Args:
            request: Request parameters including standards_id

        Returns:
            TaskResult containing GetContentStandardsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetContentStandardsResponse)

    async def list_content_standards(
        self,
        request: ListContentStandardsRequest,
    ) -> TaskResult[ListContentStandardsResponse]:
        """
        List content standards configurations.

        Args:
            request: Request parameters including optional filters

        Returns:
            TaskResult containing ListContentStandardsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListContentStandardsResponse)

    async def update_content_standards(
        self,
        request: UpdateContentStandardsRequest,
    ) -> TaskResult[UpdateContentStandardsResponse]:
        """
        Update a content standards configuration.

        Args:
            request: Request parameters including standards_id and updates

        Returns:
            TaskResult containing UpdateContentStandardsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, UpdateContentStandardsResponse)

    async def calibrate_content(
        self,
        request: CalibrateContentRequest,
    ) -> TaskResult[CalibrateContentResponse]:
        """
        Calibrate content against standards.

        Evaluates content (artifact or URL) against configured standards to
        determine suitability for ad placement.

        Args:
            request: Request parameters including content to evaluate

        Returns:
            TaskResult containing CalibrateContentResponse with verdict
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="calibrate_content",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.calibrate_content(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="calibrate_content",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CalibrateContentResponse)

    async def validate_content_delivery(
        self,
        request: ValidateContentDeliveryRequest,
    ) -> TaskResult[ValidateContentDeliveryResponse]:
        """
        Validate content delivery against standards.

        Validates that ad delivery records comply with content standards.

        Args:
            request: Request parameters including delivery records

        Returns:
            TaskResult containing ValidateContentDeliveryResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="validate_content_delivery",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.validate_content_delivery(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="validate_content_delivery",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ValidateContentDeliveryResponse)

    async def get_media_buy_artifacts(
        self,
        request: GetMediaBuyArtifactsRequest,
    ) -> TaskResult[GetMediaBuyArtifactsResponse]:
        """
        Get artifacts associated with a media buy.

        Retrieves content artifacts where ads were delivered for a media buy.

        Args:
            request: Request parameters including media_buy_id

        Returns:
            TaskResult containing GetMediaBuyArtifactsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_artifacts",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_media_buy_artifacts(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_artifacts",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetMediaBuyArtifactsResponse)

    # ========================================================================
    # V3 Protocol Methods - Sponsored Intelligence
    # ========================================================================

    async def si_get_offering(
        self,
        request: SiGetOfferingRequest,
    ) -> TaskResult[SiGetOfferingResponse]:
        """
        Get sponsored intelligence offering.

        Retrieves product/service offerings that can be presented in a
        sponsored intelligence session.

        Args:
            request: Request parameters including brand context

        Returns:
            TaskResult containing SiGetOfferingResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_get_offering",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_get_offering(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_get_offering",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiGetOfferingResponse)

    async def si_initiate_session(
        self,
        request: SiInitiateSessionRequest,
    ) -> TaskResult[SiInitiateSessionResponse]:
        """
        Initiate a sponsored intelligence session.

        Starts a conversational brand experience session with a user.

        Args:
            request: Request parameters including identity and context

        Returns:
            TaskResult containing SiInitiateSessionResponse with session_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_initiate_session",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_initiate_session(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_initiate_session",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiInitiateSessionResponse)

    async def si_send_message(
        self,
        request: SiSendMessageRequest,
    ) -> TaskResult[SiSendMessageResponse]:
        """
        Send a message in a sponsored intelligence session.

        Continues the conversation in an active SI session.

        Args:
            request: Request parameters including session_id and message

        Returns:
            TaskResult containing SiSendMessageResponse with brand response
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_send_message",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_send_message(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_send_message",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiSendMessageResponse)

    async def si_terminate_session(
        self,
        request: SiTerminateSessionRequest,
    ) -> TaskResult[SiTerminateSessionResponse]:
        """
        Terminate a sponsored intelligence session.

        Ends an active SI session, optionally with follow-up actions.

        Args:
            request: Request parameters including session_id and termination context

        Returns:
            TaskResult containing SiTerminateSessionResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_terminate_session",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_terminate_session(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_terminate_session",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiTerminateSessionResponse)

    # ========================================================================
    # V3 Governance Methods
    # ========================================================================

    async def get_creative_features(
        self,
        request: GetCreativeFeaturesRequest,
    ) -> TaskResult[GetCreativeFeaturesResponse]:
        """Evaluate governance features for a creative manifest."""
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_creative_features",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_creative_features(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_creative_features",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetCreativeFeaturesResponse)

    async def sync_plans(
        self,
        request: SyncPlansRequest,
    ) -> TaskResult[SyncPlansResponse]:
        """Sync campaign governance plans to the governance agent."""
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_plans",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_plans(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_plans",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SyncPlansResponse)

    async def check_governance(
        self,
        request: CheckGovernanceRequest,
    ) -> TaskResult[CheckGovernanceResponse]:
        """Check a proposed or committed action against campaign governance."""
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="check_governance",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.check_governance(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="check_governance",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CheckGovernanceResponse)

    async def report_plan_outcome(
        self,
        request: ReportPlanOutcomeRequest,
    ) -> TaskResult[ReportPlanOutcomeResponse]:
        """Report the outcome of a governed action to the governance agent."""
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="report_plan_outcome",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.report_plan_outcome(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="report_plan_outcome",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ReportPlanOutcomeResponse)

    async def report_plan_adjustment(
        self,
        request: ReportPlanAdjustmentRequest,
    ) -> TaskResult[ReportPlanAdjustmentResponse]:
        """Report or review an adjustment to a governed plan outcome."""
        return cast(
            TaskResult[ReportPlanAdjustmentResponse],
            await self._execute_typed_task(
                "report_plan_adjustment", request, ReportPlanAdjustmentResponse
            ),
        )

    async def get_plan_audit_logs(
        self,
        request: GetPlanAuditLogsRequest,
    ) -> TaskResult[GetPlanAuditLogsResponse]:
        """Retrieve governance state and audit logs for one or more plans."""
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_plan_audit_logs",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_plan_audit_logs(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_plan_audit_logs",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetPlanAuditLogsResponse)

    async def create_property_list(
        self,
        request: CreatePropertyListRequest,
    ) -> TaskResult[CreatePropertyListResponse]:
        """
        Create a property list for governance filtering.

        Property lists define dynamic sets of properties based on filters,
        brand manifests, and feature requirements.

        Args:
            request: Request parameters for creating the property list

        Returns:
            TaskResult containing CreatePropertyListResponse with list_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.create_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CreatePropertyListResponse)

    async def get_property_list(
        self,
        request: GetPropertyListRequest,
    ) -> TaskResult[GetPropertyListResponse]:
        """
        Get a property list with optional resolution.

        When resolve=true, returns the list of resolved property identifiers.
        Use this to get the actual properties that match the list's filters.

        Args:
            request: Request parameters including list_id and resolve flag

        Returns:
            TaskResult containing GetPropertyListResponse with identifiers
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetPropertyListResponse)

    async def list_property_lists(
        self,
        request: ListPropertyListsRequest,
    ) -> TaskResult[ListPropertyListsResponse]:
        """
        List property lists owned by a principal.

        Retrieves metadata for all property lists, optionally filtered
        by principal or pagination parameters.

        Args:
            request: Request parameters with optional filtering

        Returns:
            TaskResult containing ListPropertyListsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_property_lists",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_property_lists(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_property_lists",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListPropertyListsResponse)

    async def update_property_list(
        self,
        request: UpdatePropertyListRequest,
    ) -> TaskResult[UpdatePropertyListResponse]:
        """
        Update a property list.

        Modifies the filters, brand manifest, or other parameters
        of an existing property list.

        Args:
            request: Request parameters with list_id and updates

        Returns:
            TaskResult containing UpdatePropertyListResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, UpdatePropertyListResponse)

    async def delete_property_list(
        self,
        request: DeletePropertyListRequest,
    ) -> TaskResult[DeletePropertyListResponse]:
        """
        Delete a property list.

        Removes a property list. Any active subscriptions to this list
        will be terminated.

        Args:
            request: Request parameters with list_id

        Returns:
            TaskResult containing DeletePropertyListResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="delete_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.delete_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="delete_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, DeletePropertyListResponse)

    # ========================================================================
    # V3 Protocol Methods - Governance (Collection Lists)
    # ========================================================================

    async def create_collection_list(
        self,
        request: CreateCollectionListRequest,
    ) -> TaskResult[CreateCollectionListResponse]:
        """Create a collection list for governance filtering.

        Collection lists define dynamic sets of collections (properties, segments, etc.)
        that can be referenced by authorization rules and audience scoping.

        Args:
            request: Request parameters for creating the collection list

        Returns:
            TaskResult containing CreateCollectionListResponse with list_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_collection_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.create_collection_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_collection_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CreateCollectionListResponse)

    async def get_collection_list(
        self,
        request: GetCollectionListRequest,
    ) -> TaskResult[GetCollectionListResponse]:
        """Get a collection list with optional resolution.

        When resolve=true, returns the resolved members of the collection list.

        Args:
            request: Request parameters including list_id and resolve flag

        Returns:
            TaskResult containing GetCollectionListResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_collection_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_collection_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_collection_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetCollectionListResponse)

    async def list_collection_lists(
        self,
        request: ListCollectionListsRequest,
    ) -> TaskResult[ListCollectionListsResponse]:
        """List collection lists owned by a principal.

        Args:
            request: Request parameters with optional filtering

        Returns:
            TaskResult containing ListCollectionListsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_collection_lists",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_collection_lists(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_collection_lists",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListCollectionListsResponse)

    async def update_collection_list(
        self,
        request: UpdateCollectionListRequest,
    ) -> TaskResult[UpdateCollectionListResponse]:
        """Update a collection list.

        Args:
            request: Request parameters with list_id and updates

        Returns:
            TaskResult containing UpdateCollectionListResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_collection_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_collection_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_collection_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, UpdateCollectionListResponse)

    async def delete_collection_list(
        self,
        request: DeleteCollectionListRequest,
    ) -> TaskResult[DeleteCollectionListResponse]:
        """Delete a collection list.

        Args:
            request: Request parameters with list_id

        Returns:
            TaskResult containing DeleteCollectionListResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="delete_collection_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.delete_collection_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="delete_collection_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, DeleteCollectionListResponse)

    # ========================================================================
    # V3 Protocol Methods - Governance (Sync Governance)
    # ========================================================================

    async def sync_governance(
        self,
        request: SyncGovernanceRequest,
    ) -> TaskResult[SyncGovernanceResponse]:
        """Sync governance agents attached to an account.

        Attach, detach, or replace the set of governance agents that must be
        consulted for plan approval on an account.

        Args:
            request: Request parameters with account and governance agents

        Returns:
            TaskResult containing SyncGovernanceResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_governance",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_governance(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_governance",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SyncGovernanceResponse)

    # ========================================================================
    # V3 Protocol Methods - Temporal Matching Protocol (TMP)
    # ========================================================================

    async def context_match(
        self,
        request: ContextMatchRequest,
    ) -> TaskResult[ContextMatchResponse]:
        """Match ad context to buyer packages.

        Evaluates contextual signals for a publisher placement against the
        buyer's active packages and returns matching offers.

        Args:
            request: Context match request with placement, property, and
                optional artifact refs, context signals, and geo data.

        Returns:
            TaskResult containing ContextMatchResponse with offers.
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True, by_alias=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="context_match",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.context_match(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="context_match",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ContextMatchResponse)

    async def identity_match(
        self,
        request: IdentityMatchRequest,
    ) -> TaskResult[IdentityMatchResponse]:
        """Match user identity for package eligibility.

        Evaluates a user identity token against all active packages for
        frequency capping and personalization.

        Args:
            request: Identity match request with user_token, uid_type,
                and package_ids.

        Returns:
            TaskResult containing IdentityMatchResponse with eligible_package_ids.
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True, by_alias=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="identity_match",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.identity_match(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="identity_match",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, IdentityMatchResponse)

    # ========================================================================
    # V3 Protocol Methods - Brand Rights
    # ========================================================================

    async def get_brand_identity(
        self,
        request: GetBrandIdentityRequest,
    ) -> TaskResult[GetBrandIdentityResponse]:
        """Get brand identity information.

        Retrieves brand identity data including logos, colors, fonts,
        voice synthesis config, and rights availability.

        Args:
            request: Request with brand_id and optional fields filter.

        Returns:
            TaskResult containing GetBrandIdentityResponse.
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_brand_identity",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_brand_identity(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_brand_identity",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetBrandIdentityResponse)

    async def get_rights(
        self,
        request: GetRightsRequest,
    ) -> TaskResult[GetRightsResponse]:
        """Get available rights for licensing.

        Searches for rights offerings using natural language query and
        filters by type, uses, countries, and buyer compatibility.

        Args:
            request: Request with query, uses, and optional filters.

        Returns:
            TaskResult containing GetRightsResponse with matched rights.
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_rights",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_rights(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_rights",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetRightsResponse)

    async def acquire_rights(
        self,
        request: AcquireRightsRequest,
    ) -> TaskResult[AcquireRightsResponse]:
        """Acquire rights for brand content usage.

        Binding contractual request to license rights for a campaign.
        Returns credentials for generating rights-cleared content.

        Args:
            request: Request with rights_id, pricing_option_id, buyer,
                campaign, and revocation_webhook.

        Returns:
            TaskResult containing AcquireRightsResponse (acquired,
            pending_approval, rejected, or error).
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="acquire_rights",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.acquire_rights(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="acquire_rights",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, AcquireRightsResponse)

    async def update_rights(
        self,
        request: UpdateRightsRequest,
    ) -> TaskResult[UpdateRightsResponse]:
        """Update terms of an existing rights acquisition.

        Modifies a previously acquired rights record — typically to extend
        the ``end_date``, raise the ``impression_cap``, pause/unpause via
        ``paused``, or swap to a compatible ``pricing_option_id``. Partial
        update: pass only the fields you want to change.

        Failure modes (surface as ``TaskResult`` with ``success=False``):

        * Acquisition is expired or revoked — the seller rejects the update
          outright; mint a fresh ``acquire_rights`` instead.
        * ``pricing_option_id`` swap to an incompatible option — rejected;
          the new option's terms must be a strict superset / compatible
          with the original acquisition.
        * No partial-state mutations on rejection: the acquisition remains
          at its prior state when any field fails validation.

        Args:
            request: Request with ``rights_id`` and at least one mutable
                field (``end_date``, ``impression_cap``, ``paused``, or
                ``pricing_option_id``).

        Returns:
            TaskResult containing UpdateRightsResponse (updated or error).
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_rights",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_rights(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_rights",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, UpdateRightsResponse)

    async def validate_input(self, request: Any) -> TaskResult[Any]:
        """Validate creative input against a format declaration."""
        from adcp.types import _generated as gen

        params = request.model_dump(mode="json", exclude_none=True)
        raw_result = await self.adapter.validate_input(params)
        return self.adapter._parse_response(raw_result, gen.ValidateInputResponse)

    async def verify_brand_claim(self, request: Any) -> TaskResult[Any]:
        """Verify a single brand claim."""
        from adcp.types import _generated as gen

        params = request.model_dump(mode="json", exclude_none=True)
        raw_result = await self.adapter.verify_brand_claim(params)
        return self.adapter._parse_response(raw_result, gen.VerifyBrandClaimResponse)

    async def verify_brand_claims(self, request: Any) -> TaskResult[Any]:
        """Verify multiple brand claims."""
        from adcp.types import _generated as gen

        params = request.model_dump(mode="json", exclude_none=True)
        raw_result = await self.adapter.verify_brand_claims(params)
        return self.adapter._parse_response(raw_result, gen.VerifyBrandClaimsResponseBulk)

    # ========================================================================
    # V3 Protocol Methods - Compliance
    # ========================================================================

    async def comply_test_controller(
        self,
        request: ComplyTestControllerRequest,
    ) -> TaskResult[ComplyTestControllerResponse]:
        """Compliance test controller for sandbox testing.

        Enables sellers to simulate state transitions and delivery data
        in a sandbox environment for compliance testing.

        Args:
            request: Request specifying scenario and parameters.

        Returns:
            TaskResult containing ComplyTestControllerResponse.
        """
        operation_id = create_operation_id()
        params = request.model_dump(mode="json", exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="comply_test_controller",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.comply_test_controller(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="comply_test_controller",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ComplyTestControllerResponse)

    async def execute_task(self, task_name: str, request: BaseModel) -> TaskResult[Any]:
        """Execute a standard task through the canonical primary API map."""

        legacy_only_tasks = {
            "build_creative",
            "list_creative_formats",
            "preview_creative",
        }
        if task_name in legacy_only_tasks:
            raise ValueError(
                f"{task_name} is legacy-only; use execute_task_legacy() or the "
                "corresponding *_legacy method"
            )
        serialized = request.model_dump(mode="json", exclude_none=True)
        if strip_legacy_creative_identity(serialized) != serialized:
            raise ValueError(
                f"{task_name} request contains legacy creative identity; generic execute_task "
                "is canonical-only"
            )
        method = getattr(self, task_name, None)
        if method is None or not callable(method) or task_name.endswith("_legacy"):
            raise ValueError(f"Unknown canonical AdCP task: {task_name}")
        return cast(TaskResult[Any], await method(request))

    async def execute_task_legacy(self, task_name: str, request: BaseModel) -> TaskResult[Any]:
        """Execute an explicitly raw creative task for migration tooling."""

        methods: dict[str, Callable[[Any], Any]] = {
            "build_creative": self.build_creative_legacy,
            "create_media_buy": self.create_media_buy_legacy,
            "get_creative_delivery": self.get_creative_delivery_legacy,
            "get_media_buy_delivery": self.get_media_buy_delivery_legacy,
            "get_media_buys": self.get_media_buys_legacy,
            "get_products": self.get_products_legacy,
            "list_creative_formats": self.list_creative_formats_legacy,
            "list_creatives": self.list_creatives_legacy,
            "preview_creative": self.preview_creative_legacy,
            "sync_creatives": self.sync_creatives_legacy,
            "update_media_buy": self.update_media_buy_legacy,
        }
        method = methods.get(task_name)
        if method is None:
            raise ValueError(
                f"No explicit legacy task adapter for {task_name!r}; "
                "use the raw protocol adapter for conformance tooling"
            )
        return cast(TaskResult[Any], await method(request))

    async def list_tools(self) -> list[str]:
        """
        List available tools from the agent.

        Returns:
            List of tool names
        """
        return await self.adapter.list_tools()

    async def get_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata.

        Returns agent card information including:
        - Agent name, description, version
        - Protocol type (mcp or a2a)
        - AdCP version (from extensions.adcp.adcp_version)
        - Supported protocols (from extensions.adcp.protocols_supported)
        - Available tools/skills

        Returns:
            Dictionary with agent metadata
        """
        return await self.adapter.get_agent_info()

    async def close(self) -> None:
        """Close the adapter and clean up resources."""
        if hasattr(self.adapter, "close"):
            logger.debug(f"Closing adapter for agent {self.agent_config.id}")
            await self.adapter.close()

    async def close_mcp_session(self, session_id: str | None = None) -> None:
        """Explicitly terminate a stateful MCP Streamable HTTP session.

        This sends ``DELETE`` to the configured MCP endpoint with the
        ``Mcp-Session-Id`` header. When ``session_id`` is omitted, the
        SDK-managed current session is closed. It is only valid for MCP
        agents using ``mcp_transport="streamable_http"``.
        """
        if not isinstance(self.adapter, MCPAdapter):
            raise TypeError(
                "close_mcp_session is only supported for MCP clients; "
                f"got {self.agent_config.protocol}"
            )
        await self.adapter.close_mcp_session(session_id)

    async def __aenter__(self) -> ADCPClient:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    def _verify_webhook_signature(
        self,
        payload: dict[str, Any],
        signature: str,
        timestamp: str,
        raw_body: bytes | str | None = None,
    ) -> bool:
        """
        Verify HMAC-SHA256 signature of webhook payload.

        The verification algorithm matches get_adcp_signed_headers_for_webhook:
        1. Constructs message as "{timestamp}.{raw_http_body_bytes}"
        2. HMAC-SHA256 signs with the shared secret
        3. Compares against the provided signature (with "sha256=" prefix stripped)
           using constant-time comparison.

        Per AdCP spec (adcontextprotocol/adcp#2478): verifiers MUST use the raw
        HTTP body bytes captured before any JSON parse; they SHOULD NOT
        re-serialize a parsed payload to reconstruct the signed bytes, because
        re-serialization silently fails against signers whose output differs in
        separator choice, key order, unicode escapes, or number formatting —
        masking signer bugs the verifier should surface. Callers that genuinely
        cannot capture raw bytes MUST fail closed.

        This implementation therefore rejects verification attempts that don't
        supply ``raw_body``. Capture it from your framework's pre-parse hook
        (FastAPI ``Request.body()``, Flask ``request.get_data(cache=True)``,
        aiohttp ``Request.read()``, Express ``express.raw()``).

        Args:
            payload: Parsed webhook payload dict (not used for signing; kept
                for signature parity with callers, but verification derives
                solely from ``raw_body``).
            signature: Signature to verify (with or without "sha256=" prefix)
            timestamp: Unix timestamp in seconds from X-AdCP-Timestamp header
            raw_body: Raw HTTP request body bytes as received on the wire,
                captured before any JSON parse. Required.

        Returns:
            True if signature is valid, False otherwise (including when
            ``raw_body`` is missing — fails closed per spec).
        """
        if not self.webhook_secret:
            logger.error("Webhook signature verification failed: no webhook_secret configured")
            return False

        # Fail closed per adcontextprotocol/adcp#2478: verifiers that cannot
        # capture raw bytes MUST reject, surfacing the infrastructure gap
        # rather than silently reconstructing a signed body that may diverge
        # from the bytes the signer actually hashed.
        if raw_body is None:
            logger.error(
                "Webhook signature verification failed: raw_body is required. "
                "Capture the raw HTTP body pre-parse and pass it to "
                "handle_webhook(raw_body=...). See "
                "https://adcontextprotocol.org/docs/building/implementation/security"
                "#legacy-hmac-sha256-fallback-deprecated-removed-in-40"
            )
            return False

        # Reject stale or future timestamps to prevent replay attacks
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False
        now = int(time.time())
        if abs(now - ts) > self.webhook_timestamp_tolerance:
            return False

        # Strip "sha256=" prefix if present
        if signature.startswith("sha256="):
            signature = signature[7:]

        payload_str = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body

        # Construct signed message: timestamp.payload
        signed_message = f"{timestamp}.{payload_str}"

        # Generate expected signature
        expected_signature = hmac.new(
            self.webhook_secret.encode("utf-8"), signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected_signature)

    def _parse_webhook_result(
        self,
        task_id: str,
        task_type: str,
        operation_id: str,
        status: GeneratedTaskStatus,
        result: Any,
        timestamp: datetime | str,
        message: str | None,
        context_id: str | None,
        *,
        preserve_legacy_identity: bool = False,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Parse webhook data into typed TaskResult based on task_type.

        Args:
            task_id: Unique identifier for this task
            task_type: Task type from application routing (e.g., "get_products")
            operation_id: Operation identifier from application routing
            status: Current task status
            result: Task-specific payload (AdCP response data)
            timestamp: ISO 8601 timestamp when webhook was generated
            message: Human-readable summary of task state
            context_id: Session/conversation identifier

        Returns:
            TaskResult with task-specific typed response data

        Note:
            This method works with both MCP and A2A protocols by accepting
            protocol-agnostic parameters rather than protocol-specific objects.
        """
        from adcp.utils.response_parser import parse_json_or_text

        # Map task types to their response types (using string literals, not enum)
        # Note: Some response types are Union types (e.g., ActivateSignalResponse = Success | Error)
        response_type_map: dict[str, type[BaseModel] | Any] = {
            # Core operations
            "get_products": GetProductsResponse,
            "list_products": ListProductsResponse,
            "request_proposals": RequestProposalsResponse,
            "refine_proposals": RefineProposalsResponse,
            "decline_proposals": DeclineProposalsResponse,
            "buy_products": BuyProductsResponse,
            "accept_proposal": AcceptProposalResponse,
            "control_media_buy": ControlMediaBuyResponse,
            "list_creative_formats": ListCreativeFormatsResponse,
            "sync_creatives": SyncCreativesResponse,
            "list_creatives": ListCreativesResponse,
            "build_creative": LegacyBuildCreativeResponse,
            "preview_creative": LegacyPreviewCreativeResponse,
            "create_media_buy": CreateMediaBuyResponse,
            "update_media_buy": UpdateMediaBuyResponse,
            "get_media_buy_delivery": GetMediaBuyDeliveryResponse,
            "get_media_buys": GetMediaBuysResponse,
            "get_signals": GetSignalsResponse,
            "activate_signal": ActivateSignalResponse,
            "provide_performance_feedback": ProvidePerformanceFeedbackResponse,
            "report_usage": ReportUsageResponse,
            "get_account_financials": GetAccountFinancialsResponse,
            "list_accounts": ListAccountsResponse,
            "sync_accounts": SyncAccountsResponse,
            "log_event": LogEventResponse,
            "sync_event_sources": SyncEventSourcesResponse,
            "sync_audiences": SyncAudiencesResponse,
            "sync_catalogs": SyncCatalogsResponse,
            "get_creative_delivery": GetCreativeDeliveryResponse,
            # V3 Protocol Discovery
            "get_adcp_capabilities": GetAdcpCapabilitiesResponse,
            "sync_agent_notification_configs": SyncAgentNotificationConfigsResponse,
            # V3 Content Standards
            "create_content_standards": CreateContentStandardsResponse,
            "get_content_standards": GetContentStandardsResponse,
            "list_content_standards": ListContentStandardsResponse,
            "update_content_standards": UpdateContentStandardsResponse,
            "calibrate_content": CalibrateContentResponse,
            "validate_content_delivery": ValidateContentDeliveryResponse,
            "get_media_buy_artifacts": GetMediaBuyArtifactsResponse,
            # V3 Sponsored Intelligence
            "si_get_offering": SiGetOfferingResponse,
            "si_initiate_session": SiInitiateSessionResponse,
            "si_send_message": SiSendMessageResponse,
            "si_terminate_session": SiTerminateSessionResponse,
            # V3 Governance
            "get_creative_features": GetCreativeFeaturesResponse,
            "sync_plans": SyncPlansResponse,
            "check_governance": CheckGovernanceResponse,
            "report_plan_outcome": ReportPlanOutcomeResponse,
            "report_plan_adjustment": ReportPlanAdjustmentResponse,
            "get_plan_audit_logs": GetPlanAuditLogsResponse,
            "create_property_list": CreatePropertyListResponse,
            "get_property_list": GetPropertyListResponse,
            "list_property_lists": ListPropertyListsResponse,
            "update_property_list": UpdatePropertyListResponse,
            "delete_property_list": DeletePropertyListResponse,
            # TMP
            "context_match": ContextMatchResponse,
            "identity_match": IdentityMatchResponse,
            # Brand Rights
            "get_brand_identity": GetBrandIdentityResponse,
            "get_rights": GetRightsResponse,
            "acquire_rights": AcquireRightsResponse,
            "update_rights": UpdateRightsResponse,
            # Compliance
            "comply_test_controller": ComplyTestControllerResponse,
        }

        if preserve_legacy_identity:
            response_type_map.update(
                {
                    "get_products": LegacyGetProductsResponse,
                    "list_creative_formats": ListCreativeFormatsResponse,
                    "sync_creatives": LegacySyncCreativesResponse,
                    "list_creatives": LegacyListCreativesResponse,
                    "build_creative": LegacyBuildCreativeResponse,
                    "preview_creative": LegacyPreviewCreativeResponse,
                    "create_media_buy": LegacyCreateMediaBuyResponse,
                    "update_media_buy": LegacyUpdateMediaBuyResponse,
                    "get_media_buy_delivery": LegacyGetMediaBuyDeliveryResponse,
                    "get_media_buys": LegacyGetMediaBuysResponse,
                    "get_creative_delivery": LegacyGetCreativeDeliveryResponse,
                }
            )

        # Handle completed tasks with result parsing
        if status == GeneratedTaskStatus.completed and result is not None:
            if task_type == "get_products" and not preserve_legacy_identity:
                projected = self._canonicalize_get_products_result(
                    TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=result,
                        success=True,
                        message=message,
                        metadata={
                            "task_id": task_id,
                            "operation_id": operation_id,
                            "timestamp": timestamp,
                            "message": message,
                        },
                    )
                )
                return cast(TaskResult[AdcpAsyncResponseData], projected)
            legacy_lifecycle_types = {
                "create_media_buy": (LegacyCreateMediaBuyResponse, CreateMediaBuyResponse),
                "update_media_buy": (LegacyUpdateMediaBuyResponse, UpdateMediaBuyResponse),
                "sync_creatives": (LegacySyncCreativesResponse, SyncCreativesResponse),
                "get_media_buys": (LegacyGetMediaBuysResponse, GetMediaBuysResponse),
                "get_media_buy_delivery": (
                    LegacyGetMediaBuyDeliveryResponse,
                    GetMediaBuyDeliveryResponse,
                ),
            }
            lifecycle_types = legacy_lifecycle_types.get(task_type)
            if (
                not preserve_legacy_identity
                and lifecycle_types is not None
                and self._callback_creative_dialect(result) is CreativeDialect.LEGACY
            ):
                projected = self._canonicalize_lifecycle_result(
                    TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=result,
                        success=True,
                        message=message,
                    ),
                    legacy_type=lifecycle_types[0],
                    canonical_type=lifecycle_types[1],
                )
                return cast(TaskResult[AdcpAsyncResponseData], projected)
            if (
                not preserve_legacy_identity
                and task_type == "list_creatives"
                and self._callback_creative_dialect(result) is CreativeDialect.LEGACY
            ):
                projected = self._canonicalize_format_read_result(
                    TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=result,
                        success=True,
                        message=message,
                    ),
                    legacy_type=LegacyListCreativesResponse,
                    canonical_type=ListCreativesResponse,
                    collection="creatives",
                    require_format=True,
                )
                return cast(TaskResult[AdcpAsyncResponseData], projected)
            if (
                not preserve_legacy_identity
                and task_type == "get_creative_delivery"
                and self._callback_creative_dialect(result) is CreativeDialect.LEGACY
            ):
                projected = self._canonicalize_format_read_result(
                    TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=result,
                        success=True,
                        message=message,
                    ),
                    legacy_type=LegacyGetCreativeDeliveryResponse,
                    canonical_type=GetCreativeDeliveryResponse,
                    collection="creatives",
                    require_format=False,
                )
                return cast(TaskResult[AdcpAsyncResponseData], projected)
            response_type = response_type_map.get(task_type)
            if response_type:
                try:
                    parsed_result: Any = parse_json_or_text(result, response_type)
                    metadata: dict[str, Any] = {
                        "task_id": task_id,
                        "operation_id": operation_id,
                        "timestamp": timestamp,
                        "message": message,
                    }
                    if task_type == "refine_proposals" and isinstance(result, Mapping):
                        metadata[WIRE_RESPONSE_METADATA_KEY] = result
                    return TaskResult[AdcpAsyncResponseData](
                        status=TaskStatus.COMPLETED,
                        data=parsed_result,
                        success=True,
                        metadata=metadata,
                    )
                except ValueError as e:
                    logger.warning(f"Failed to parse webhook result: {e}")
                    # Fall through to untyped result

        # Handle failed, input-required, or unparseable results
        # Convert status to core TaskStatus enum
        status_map = {
            GeneratedTaskStatus.completed: TaskStatus.COMPLETED,
            GeneratedTaskStatus.submitted: TaskStatus.SUBMITTED,
            GeneratedTaskStatus.working: TaskStatus.WORKING,
            GeneratedTaskStatus.failed: TaskStatus.FAILED,
            GeneratedTaskStatus.input_required: TaskStatus.NEEDS_INPUT,
        }
        task_status = status_map.get(status, TaskStatus.FAILED)

        # Extract error message from result.errors if present
        error_message: str | None = None
        if result is not None and hasattr(result, "errors"):
            errors = getattr(result, "errors", None)
            if errors and len(errors) > 0:
                first_error = errors[0]
                if hasattr(first_error, "message"):
                    error_message = first_error.message

        fallback_metadata: dict[str, Any] = {
            "task_id": task_id,
            "operation_id": operation_id,
            "timestamp": timestamp,
            "message": message,
            "context_id": context_id,
        }
        if task_type == "refine_proposals" and isinstance(result, Mapping):
            fallback_metadata[WIRE_RESPONSE_METADATA_KEY] = result
        return TaskResult[AdcpAsyncResponseData](
            status=task_status,
            data=result,
            success=status == GeneratedTaskStatus.completed,
            error=error_message,
            metadata=fallback_metadata,
        )

    async def _handle_mcp_webhook(
        self,
        payload: dict[str, Any],
        task_type: str,
        operation_id: str,
        signature: str | None,
        timestamp: str | None = None,
        raw_body: bytes | str | None = None,
        preserve_legacy_identity: bool = False,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Handle MCP webhook delivered via HTTP POST.

        Args:
            payload: Webhook payload dict
            task_type: Task type from application routing
            operation_id: Operation identifier from application routing
            signature: HMAC-SHA256 signature from X-AdCP-Signature. Required
                when ``webhook_secret`` configures the deprecated HMAC fallback.
            timestamp: Unix timestamp from X-AdCP-Timestamp. Required with the
                deprecated HMAC fallback.
            raw_body: Raw HTTP request body. Required with the deprecated HMAC
                fallback so the authenticated bytes are the bytes processed.

        Returns:
            TaskResult with parsed task-specific response data

        Raises:
            ADCPWebhookSignatureError: If signature verification fails
            ValidationError: If payload doesn't match McpWebhookPayload schema
        """
        from adcp.types.generated_poc.core.mcp_webhook_payload import McpWebhookPayload

        # Signed MCP webhooks are the secure default. Receiving without a
        # verifier requires an explicit compatibility opt-in so a missing
        # secret cannot silently turn a public endpoint into an unauthenticated
        # callback receiver.
        if self.webhook_secret:
            if not signature or not timestamp:
                raise ADCPWebhookSignatureError(
                    "Webhook signature and timestamp headers are required"
                )
            if not self._verify_webhook_signature(payload, signature, timestamp, raw_body):
                logger.warning(
                    f"Webhook signature verification failed for agent {self.agent_config.id}"
                )
                raise ADCPWebhookSignatureError("Invalid webhook signature")
            if raw_body is None:  # Defensive type narrowing; verifier rejects this above.
                raise ADCPWebhookSignatureError("Signed webhook raw body is required")
            try:
                authenticated_payload = json.loads(raw_body)
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise ADCPWebhookSignatureError("Invalid signed webhook body") from exc
            if not isinstance(authenticated_payload, dict):
                raise ADCPWebhookSignatureError("Signed webhook body must be a JSON object")
            # Process the bytes that were authenticated, not a separately
            # supplied parsed object that middleware could have transformed.
            payload = cast(dict[str, Any], authenticated_payload)
        elif self.allow_unauthenticated_webhooks is not True:
            raise ADCPWebhookSignatureError(
                "MCP webhook cannot be authenticated because webhook_secret is not configured; "
                "use WebhookReceiver for RFC 9421 callbacks, configure a shared secret only for "
                "an explicitly selected legacy HMAC registration, or set "
                "allow_unauthenticated_webhooks=True only for receivers isolated from "
                "untrusted networks"
            )

        # Select the canonical/legacy surface from the body only after signed
        # callbacks have been replaced by their authenticated raw bytes.
        payload_task_type = payload.get("task_type")
        if not preserve_legacy_identity and payload_task_type in _LEGACY_ONLY_CREATIVE_TASKS:
            raise ValueError(
                f"{payload_task_type} webhook payloads carry legacy creative identity; use "
                "handle_webhook_legacy()"
            )

        # Validate and parse MCP webhook payload
        webhook = McpWebhookPayload.model_validate(payload)
        authenticated_task_type = webhook.task_type.value
        authenticated_operation_id = webhook.operation_id

        if preserve_legacy_identity:
            if authenticated_task_type not in _LEGACY_CREATIVE_TASKS:
                raise ValueError(
                    f"{authenticated_task_type} is not a legacy-only callback; use "
                    "handle_webhook()"
                )

        # Emit activity for monitoring
        self._emit_activity(
            Activity(
                type=ActivityType.WEBHOOK_RECEIVED,
                operation_id=authenticated_operation_id,
                agent_id=self.agent_config.id,
                task_type=authenticated_task_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "task_id": webhook.task_id,
                    "status": webhook.status.value,
                    "protocol": "mcp",
                },
            )
        )

        # Extract fields and parse result
        return self._parse_webhook_result(
            task_id=webhook.task_id,
            task_type=authenticated_task_type,
            operation_id=authenticated_operation_id,
            status=webhook.status,
            # Keep the authenticated JSON mapping, not the parsed RootModel:
            # proposal terms digests depend on exact wire lexemes.
            result=payload.get("result"),
            timestamp=webhook.timestamp,
            message=webhook.message,
            context_id=webhook.context_id,
            preserve_legacy_identity=preserve_legacy_identity,
        )

    async def _handle_a2a_webhook(
        self,
        payload: Task | TaskStatusUpdateEvent,
        task_type: str,
        operation_id: str,
        preserve_legacy_identity: bool = False,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Handle A2A webhook delivered through Task or TaskStatusUpdateEvent.

        Per A2A specification:
        - Terminated statuses (completed, failed): Payload is Task with artifacts[].parts[]
        - Intermediate statuses (working, input-required, submitted):
        Payload is TaskStatusUpdateEvent with status.message.parts[]

        Args:
            payload: A2A Task or TaskStatusUpdateEvent object
            task_type: Task type from application routing
            operation_id: Operation identifier from application routing

        Returns:
            TaskResult with parsed task-specific response data

        Note:
            Signature verification is NOT applicable for A2A webhooks
            as they arrive through authenticated A2A connections, not HTTP.
        """
        from a2a import types as _pb
        from google.protobuf.json_format import MessageToDict as _MessageToDict

        def _a2a_part_data_dict(part: _pb.Part) -> Any:
            if part.WhichOneof("content") != "data":
                return None
            return _MessageToDict(part.data)

        def _a2a_part_text(part: _pb.Part) -> str | None:
            if part.WhichOneof("content") != "text":
                return None
            return part.text

        def _a2a_state_to_string(state_value: int) -> str:
            """Map ``TaskState`` int → spec string (``TASK_STATE_COMPLETED`` → ``completed``)."""
            name = _pb.TaskState.Name(state_value)
            if name.startswith("TASK_STATE_"):
                return name[len("TASK_STATE_") :].lower().replace("_", "-")
            return name.lower()

        def _a2a_timestamp(ts: Any) -> datetime | str:
            """Convert a proto Timestamp (or string) to datetime/ISO string."""
            if ts is None:
                return datetime.now(timezone.utc)
            if isinstance(ts, str):
                return ts or datetime.now(timezone.utc)
            try:
                return cast(datetime, ts.ToDatetime().replace(tzinfo=timezone.utc))
            except AttributeError:
                return datetime.now(timezone.utc)

        adcp_data: Any = None
        text_message: str | None = None
        task_id: str
        context_id: str | None
        status_state: str
        timestamp: datetime | str

        # Type detection and extraction based on payload type
        if isinstance(payload, TaskStatusUpdateEvent):
            task_id = payload.task_id
            context_id = payload.context_id or None
            has_status = payload.HasField("status")
            status_state = _a2a_state_to_string(payload.status.state) if has_status else "failed"
            timestamp = (
                _a2a_timestamp(payload.status.timestamp)
                if has_status and payload.status.HasField("timestamp")
                else datetime.now(timezone.utc)
            )

            if has_status and payload.status.HasField("message") and payload.status.message.parts:
                data_parts = [
                    d
                    for d in (_a2a_part_data_dict(p) for p in payload.status.message.parts)
                    if d is not None
                ]
                if data_parts:
                    adcp_data = data_parts[-1]
                    if isinstance(adcp_data, dict) and "response" in adcp_data:
                        adcp_data = adcp_data["response"]

                for part in payload.status.message.parts:
                    text = _a2a_part_text(part)
                    if text is not None:
                        text_message = text
                        break

        else:
            task_id = payload.id
            context_id = payload.context_id or None
            has_status = payload.HasField("status")
            status_state = _a2a_state_to_string(payload.status.state) if has_status else "failed"
            timestamp = (
                _a2a_timestamp(payload.status.timestamp)
                if has_status and payload.status.HasField("timestamp")
                else datetime.now(timezone.utc)
            )

            if payload.artifacts:
                target_artifact = payload.artifacts[-1]
                if target_artifact.parts:
                    data_parts = [
                        d
                        for d in (_a2a_part_data_dict(p) for p in target_artifact.parts)
                        if d is not None
                    ]
                    if data_parts:
                        adcp_data = data_parts[-1]
                        if isinstance(adcp_data, dict) and "response" in adcp_data:
                            adcp_data = adcp_data["response"]

                    for part in target_artifact.parts:
                        text = _a2a_part_text(part)
                        if text is not None:
                            text_message = text
                            break

        # Map A2A status.state to GeneratedTaskStatus enum
        status_map = {
            "completed": GeneratedTaskStatus.completed,
            "submitted": GeneratedTaskStatus.submitted,
            "working": GeneratedTaskStatus.working,
            "failed": GeneratedTaskStatus.failed,
            "input-required": GeneratedTaskStatus.input_required,
            "input_required": GeneratedTaskStatus.input_required,  # Handle both formats
        }
        mapped_status = status_map.get(status_state, GeneratedTaskStatus.failed)

        # Emit activity for monitoring
        self._emit_activity(
            Activity(
                type=ActivityType.WEBHOOK_RECEIVED,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=task_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "task_id": task_id,
                    "protocol": "a2a",
                    "payload_type": (
                        "TaskStatusUpdateEvent"
                        if isinstance(payload, TaskStatusUpdateEvent)
                        else "Task"
                    ),
                },
            )
        )

        # Parse and return typed result by passing extracted fields directly
        return self._parse_webhook_result(
            task_id=task_id,
            task_type=task_type,
            operation_id=operation_id,
            status=mapped_status,
            result=adcp_data,
            timestamp=timestamp,
            message=text_message,
            context_id=context_id,
            preserve_legacy_identity=preserve_legacy_identity,
        )

    async def handle_webhook(
        self,
        payload: dict[str, Any] | Task | TaskStatusUpdateEvent,
        task_type: str,
        operation_id: str,
        signature: str | None = None,
        timestamp: str | None = None,
        raw_body: bytes | str | None = None,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Handle incoming webhook and return typed result.

        This method provides a unified interface for handling webhooks from both
        MCP and A2A protocols:

        - MCP Webhooks: HTTP POST with dict payload; the deprecated HMAC fallback
          requires a signature, timestamp, and raw body
        - A2A Webhooks: Task or TaskStatusUpdateEvent objects based on status

        The method automatically detects the protocol type and routes to the
        appropriate handler. Both protocols return a consistent TaskResult
        structure with typed AdCP response data.

        Args:
            payload: Webhook payload - one of:
                - dict[str, Any]: MCP webhook payload from HTTP POST
                - Task: A2A webhook for terminated statuses (completed, failed)
                - TaskStatusUpdateEvent: A2A webhook for intermediate statuses
                  (working, input-required, submitted)
            task_type: Task type from application routing for A2A callbacks. For
                MCP callbacks, the validated payload's authenticated ``task_type``
                controls parsing and activity correlation.
            operation_id: Operation identifier from application routing. For MCP
                callbacks, the authenticated payload value controls correlation
                when present; this argument is a compatibility fallback for old
                payloads that omit it.
            signature: HMAC-SHA256 signature from X-AdCP-Signature. Required when
                ``webhook_secret`` configures the deprecated HMAC fallback and
                ignored for A2A callbacks.
            timestamp: Unix timestamp from X-AdCP-Timestamp. Required with the
                deprecated HMAC fallback and ignored for A2A callbacks.
            raw_body: Raw HTTP request body captured before JSON parsing. Required
                with the deprecated HMAC fallback and ignored for A2A callbacks.

        Returns:
            TaskResult with parsed task-specific response data. The structure
            is identical regardless of protocol.

        Raises:
            ADCPWebhookSignatureError: If MCP signature verification fails
            ValidationError: If MCP payload doesn't match WebhookPayload schema

        Note:
            AdCP-conformant public MCP endpoints should use
            :class:`adcp.webhooks.WebhookReceiver`, which verifies RFC 9421,
            deduplicates retries, and parses the authenticated body. This method's
            HMAC mode exists only for explicitly selected legacy registrations.

        Examples:
            MCP webhook (HTTP endpoint):
            >>> @app.post("/webhook/{task_type}/{agent_id}/{operation_id}")
            >>> async def webhook_handler(task_type: str, operation_id: str, request: Request):
            >>>     raw_body = await request.body()
            >>>     payload = json.loads(raw_body)
            >>>     signature = request.headers.get("X-AdCP-Signature")
            >>>     timestamp = request.headers.get("X-AdCP-Timestamp")
            >>>     result = await client.handle_webhook(
            >>>         payload, task_type, operation_id, signature, timestamp,
            >>>         raw_body=raw_body,
            >>>     )
            >>>     if result.success:
            >>>         print(f"Task completed: {result.data}")

            A2A webhook with Task (terminated status):
            >>> async def on_task_completed(task: Task):
            >>>     # Extract task_type and operation_id from your app's task tracking
            >>>     task_type = your_task_registry.get_type(task.id)
            >>>     operation_id = your_task_registry.get_operation_id(task.id)
            >>>     result = await client.handle_webhook(
            >>>         task, task_type, operation_id
            >>>     )
            >>>     if result.success:
            >>>         print(f"Task completed: {result.data}")

            A2A webhook with TaskStatusUpdateEvent (intermediate status):
            >>> async def on_task_update(event: TaskStatusUpdateEvent):
            >>>     # Extract task_type and operation_id from your app's task tracking
            >>>     task_type = your_task_registry.get_type(event.task_id)
            >>>     operation_id = your_task_registry.get_operation_id(event.task_id)
            >>>     result = await client.handle_webhook(
            >>>         event, task_type, operation_id
            >>>     )
            >>>     if result.status == GeneratedTaskStatus.working:
            >>>         print(f"Task still working: {result.metadata.get('message')}")
        """
        if (
            isinstance(payload, (Task, TaskStatusUpdateEvent))
            and task_type in _LEGACY_ONLY_CREATIVE_TASKS
        ):
            raise ValueError(
                f"{task_type} webhook payloads carry legacy creative identity; use "
                "handle_webhook_legacy()"
            )
        result = await self._dispatch_webhook(
            payload,
            task_type,
            operation_id,
            signature,
            timestamp,
            raw_body,
            preserve_legacy_identity=False,
        )
        sanitized = strip_legacy_creative_identity(result.model_dump(mode="python"))
        return TaskResult[AdcpAsyncResponseData].model_validate(sanitized)

    async def handle_webhook_legacy(
        self,
        payload: dict[str, Any] | Task | TaskStatusUpdateEvent,
        task_type: str,
        operation_id: str,
        signature: str | None = None,
        timestamp: str | None = None,
        raw_body: bytes | str | None = None,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """Parse a callback for a task whose protocol shape is explicitly legacy-only."""

        if isinstance(payload, (Task, TaskStatusUpdateEvent)) and task_type not in (
            _LEGACY_CREATIVE_TASKS
        ):
            raise ValueError(f"{task_type} is not a legacy-only callback; use handle_webhook()")
        self._warn_legacy_creative_api("handle_webhook_legacy")
        return await self._dispatch_webhook(
            payload,
            task_type,
            operation_id,
            signature,
            timestamp,
            raw_body,
            preserve_legacy_identity=True,
        )

    async def _dispatch_webhook(
        self,
        payload: dict[str, Any] | Task | TaskStatusUpdateEvent,
        task_type: str,
        operation_id: str,
        signature: str | None,
        timestamp: str | None,
        raw_body: bytes | str | None,
        *,
        preserve_legacy_identity: bool,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """Route a callback after the public canonical/legacy boundary is selected."""

        # Detect protocol type and route to appropriate handler
        if isinstance(payload, (Task, TaskStatusUpdateEvent)):
            # A2A webhook (Task or TaskStatusUpdateEvent)
            return await self._handle_a2a_webhook(
                payload,
                task_type,
                operation_id,
                preserve_legacy_identity,
            )
        else:
            # MCP webhook (dict payload)
            return await self._handle_mcp_webhook(
                payload,
                task_type,
                operation_id,
                signature,
                timestamp,
                raw_body,
                preserve_legacy_identity,
            )


class ADCPMultiAgentClient:
    """Client for managing multiple AdCP agents."""

    def __init__(
        self,
        agents: list[AgentConfig],
        webhook_url_template: str | None = None,
        webhook_secret: str | None = None,
        on_activity: Callable[[Activity], None] | None = None,
        handlers: dict[str, Callable[..., Any]] | None = None,
        signing: SigningConfig | None = None,
        adcp_version: str | dict[str, str] | None = None,
        legacy_format_converter: LegacyFormatConverter | None = None,
        canonical_format_legacy_resolver: CanonicalFormatLegacyResolver | None = None,
        allow_unauthenticated_webhooks: bool | Mapping[str, bool] = False,
        httpx_client_factory: MCPHttpxClientFactory | None = None,
    ):
        """
        Initialize multi-agent client.

        Args:
            agents: List of agent configurations
            webhook_url_template: Template for webhook URLs
            webhook_secret: Shared secret for the deprecated HMAC-SHA256 webhook
                fallback. Configure only for registrations that explicitly
                selected legacy HMAC; use ``WebhookReceiver`` for RFC 9421.
            on_activity: Callback for activity events
            handlers: Task completion handlers
            signing: Optional RFC 9421 signing config forwarded to every
                per-agent ADCPClient. The same identity signs traffic to
                all agents. See ADCPClient.__init__ for details.
            httpx_client_factory: Optional MCP HTTP client factory forwarded
                to each MCP agent. A2A agents in a mixed collection do not use
                it. See :class:`ADCPClient` for the enforced security contract.
            allow_unauthenticated_webhooks: Explicit compatibility escape.
                A mapping scopes the opt-in by agent ID; omitted IDs remain
                protected. A uniform True is accepted only for a single-agent
                collection. Defaults to False.
            adcp_version: AdCP protocol release pin. Three forms:

                - ``None`` (default): every per-agent ADCPClient resolves
                  the SDK's compile-time pin.
                - ``str`` (e.g. ``"3.1"``): every agent uses this pin.
                - ``dict[str, str]`` (e.g.
                  ``{"seller_a": "3.0", "seller_b": "3.1"}``): per-agent
                  override map keyed by ``agent.id``. Agents missing
                  from the map fall back to the SDK default — useful
                  for holdco/multi-tenant operators where one seller is
                  ahead of the others on the upgrade cadence.

                See ADCPClient.__init__ for per-instance semantics.
                Cross-major pins raise ConfigurationError at construction.
        """
        agent_ids = {agent.id for agent in agents}
        if httpx_client_factory is not None and not callable(httpx_client_factory):
            raise TypeError("httpx_client_factory must be callable")
        if httpx_client_factory is not None and not any(
            agent.protocol == Protocol.MCP for agent in agents
        ):
            raise TypeError("httpx_client_factory requires at least one MCP agent")
        if isinstance(allow_unauthenticated_webhooks, Mapping):
            unknown_ids = set(allow_unauthenticated_webhooks) - agent_ids
            if unknown_ids:
                unknown = ", ".join(sorted(unknown_ids))
                raise ValueError(
                    "allow_unauthenticated_webhooks contains unknown agent IDs: " + unknown
                )
            if any(type(value) is not bool for value in allow_unauthenticated_webhooks.values()):
                raise TypeError("allow_unauthenticated_webhooks mapping values must be bools")
            per_agent_unauthenticated = dict(allow_unauthenticated_webhooks)
        else:
            if type(allow_unauthenticated_webhooks) is not bool:
                raise TypeError(
                    "allow_unauthenticated_webhooks must be a bool or mapping of agent IDs to bools"
                )
            if allow_unauthenticated_webhooks is True and len(agents) > 1:
                raise ValueError(
                    "allow_unauthenticated_webhooks=True cannot be applied to multiple agents; "
                    "pass a mapping keyed by the isolated agent IDs"
                )
            per_agent_unauthenticated = {
                agent.id: allow_unauthenticated_webhooks for agent in agents
            }

        # Per-agent map → resolve each pin individually for the dict form;
        # otherwise use the uniform pin for all agents.
        if isinstance(adcp_version, dict):
            self._adcp_version: str | None = None  # mixed pins
            self._per_agent_versions: dict[str, str] = {
                agent_id: resolve_adcp_version(pin) for agent_id, pin in adcp_version.items()
            }
            default_pin = resolve_adcp_version(None)
            self.agents = {
                agent.id: ADCPClient(
                    agent,
                    webhook_url_template=webhook_url_template,
                    webhook_secret=webhook_secret,
                    on_activity=on_activity,
                    signing=signing,
                    httpx_client_factory=(
                        httpx_client_factory if agent.protocol == Protocol.MCP else None
                    ),
                    adcp_version=self._per_agent_versions.get(agent.id, default_pin),
                    legacy_format_converter=legacy_format_converter,
                    canonical_format_legacy_resolver=canonical_format_legacy_resolver,
                    allow_unauthenticated_webhooks=per_agent_unauthenticated.get(agent.id, False),
                )
                for agent in agents
            }
        else:
            self._adcp_version = resolve_adcp_version(adcp_version)
            self._per_agent_versions = {}
            self.agents = {
                agent.id: ADCPClient(
                    agent,
                    webhook_url_template=webhook_url_template,
                    webhook_secret=webhook_secret,
                    on_activity=on_activity,
                    signing=signing,
                    httpx_client_factory=(
                        httpx_client_factory if agent.protocol == Protocol.MCP else None
                    ),
                    adcp_version=self._adcp_version,
                    legacy_format_converter=legacy_format_converter,
                    canonical_format_legacy_resolver=canonical_format_legacy_resolver,
                    allow_unauthenticated_webhooks=per_agent_unauthenticated.get(agent.id, False),
                )
                for agent in agents
            }
        self.handlers = handlers or {}

    def get_adcp_version(self) -> str:
        """Return the AdCP protocol release pin for this multi-client.

        Returns the uniform pin when all agents share one. Raises
        :class:`ValueError` when agents have heterogeneous pins (the
        ``dict[str, str]`` constructor form) — in that case, query
        the per-agent pin via ``multi.agent(agent_id).get_adcp_version()``.
        """
        if self._adcp_version is not None:
            return self._adcp_version
        # Heterogeneous: surface uniformly if all agents agree at runtime.
        versions = {client.get_adcp_version() for client in self.agents.values()}
        if len(versions) == 1:
            return next(iter(versions))
        raise ValueError(
            "Multi-agent client has heterogeneous adcp_version pins; "
            "use multi.agent(agent_id).get_adcp_version() to read per-agent. "
            f"Pins by agent: { {a: c.get_adcp_version() for a, c in self.agents.items()} }"
        )

    def agent(self, agent_id: str) -> ADCPClient:
        """Get client for specific agent."""
        if agent_id not in self.agents:
            raise ValueError(f"Agent not found: {agent_id}")
        return self.agents[agent_id]

    @property
    def agent_ids(self) -> list[str]:
        """Get list of agent IDs."""
        return list(self.agents.keys())

    async def close(self) -> None:
        """Close all agent clients and clean up resources."""
        import asyncio

        logger.debug("Closing all agent clients in multi-agent client")
        close_tasks = [client.close() for client in self.agents.values()]
        await asyncio.gather(*close_tasks, return_exceptions=True)

    async def __aenter__(self) -> ADCPMultiAgentClient:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def get_products(
        self,
        request: GetProductsRequest,
    ) -> list[TaskResult[GetProductsResponse]]:
        """
        Execute get_products across all agents in parallel.

        Args:
            request: Request parameters

        Returns:
            List of TaskResults containing GetProductsResponse for each agent
        """
        import asyncio

        tasks = [agent.get_products(request) for agent in self.agents.values()]
        return await asyncio.gather(*tasks)

    async def list_products(
        self, request: ListProductsRequest
    ) -> list[TaskResult[ListProductsResponse]]:
        """Execute compact product discovery across all agents."""
        import asyncio

        return await asyncio.gather(
            *(agent.list_products(request) for agent in self.agents.values())
        )

    async def request_proposals(
        self, request: RequestProposalsRequest
    ) -> list[TaskResult[RequestProposalsResponse]]:
        """Request proposals from all agents."""
        import asyncio

        return await asyncio.gather(
            *(agent.request_proposals(request) for agent in self.agents.values())
        )

    async def refine_proposals(
        self, request: RefineProposalsRequest
    ) -> list[TaskResult[RefineProposalsResponse]]:
        """Refine proposals across all agents."""
        import asyncio

        return await asyncio.gather(
            *(agent.refine_proposals(request) for agent in self.agents.values())
        )

    async def decline_proposals(
        self, request: DeclineProposalsRequest
    ) -> list[TaskResult[DeclineProposalsResponse]]:
        """Decline proposals across all agents."""
        import asyncio

        return await asyncio.gather(
            *(agent.decline_proposals(request) for agent in self.agents.values())
        )

    async def buy_products(
        self, request: BuyProductsRequest
    ) -> list[TaskResult[BuyProductsResponse]]:
        """Commit direct purchases across all agents."""
        import asyncio

        return await asyncio.gather(
            *(agent.buy_products(request) for agent in self.agents.values())
        )

    async def accept_proposal(
        self, request: AcceptProposalRequest
    ) -> list[TaskResult[AcceptProposalResponse]]:
        """Accept proposals across all agents."""
        import asyncio

        return await asyncio.gather(
            *(agent.accept_proposal(request) for agent in self.agents.values())
        )

    async def control_media_buy(
        self, request: ControlMediaBuyRequest
    ) -> list[TaskResult[ControlMediaBuyResponse]]:
        """Control media buys across all agents."""
        import asyncio

        return await asyncio.gather(
            *(agent.control_media_buy(request) for agent in self.agents.values())
        )

    async def get_products_legacy(
        self, request: LegacyGetProductsRequest
    ) -> list[TaskResult[LegacyGetProductsResponse]]:
        """Execute the explicit raw discovery adapter across all agents."""

        import asyncio

        return await asyncio.gather(
            *(agent.get_products_legacy(request) for agent in self.agents.values())
        )

    async def execute_task(self, task_name: str, request: BaseModel) -> list[TaskResult[Any]]:
        """Execute a canonical task across all configured agents."""

        import asyncio

        return await asyncio.gather(
            *(agent.execute_task(task_name, request) for agent in self.agents.values())
        )

    async def execute_task_legacy(
        self, task_name: str, request: BaseModel
    ) -> list[TaskResult[Any]]:
        """Execute an explicit raw task adapter across all configured agents."""

        import asyncio

        return await asyncio.gather(
            *(agent.execute_task_legacy(task_name, request) for agent in self.agents.values())
        )

    @classmethod
    def from_env(cls) -> ADCPMultiAgentClient:
        """Create client from environment variables."""
        agents_json = os.getenv("ADCP_AGENTS")
        if not agents_json:
            raise ValueError("ADCP_AGENTS environment variable not set")

        agents_data = json.loads(agents_json)
        agents = [AgentConfig(**agent) for agent in agents_data]

        return cls(
            agents=agents,
            webhook_url_template=os.getenv("WEBHOOK_URL_TEMPLATE"),
            webhook_secret=os.getenv("WEBHOOK_SECRET"),
        )
