from __future__ import annotations

"""Base protocol adapter interface."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from adcp.types.core import AgentConfig, TaskResult, TaskStatus
from adcp.utils.response_parser import parse_json_or_text, parse_mcp_content
from adcp.validation.client_hooks import ValidationHookConfig, ValidationMode

if TYPE_CHECKING:
    import httpx

T = TypeVar("T", bound=BaseModel)


class ProtocolAdapter(ABC):
    """
    Base class for protocol adapters.

    Each adapter implements the ADCP protocol methods and handles
    protocol-specific translation (MCP/A2A) while returning properly
    typed responses.
    """

    def __init__(self, agent_config: AgentConfig):
        """Initialize adapter with agent configuration."""
        self.agent_config = agent_config
        # Optional hook; ADCPClient sets this when strict_idempotency is enabled.
        # Invoked before each mutating tool call to verify the seller declared
        # adcp.idempotency.replay_ttl_seconds in capabilities. None = no check.
        self.idempotency_capability_check: Callable[[], Awaitable[None]] | None = None
        # Unique token for this adapter's owning client — used to scope
        # ``use_idempotency_key`` so a key pinned on one client does not bleed
        # to sibling clients (cross-seller correlation risk per AdCP #2315).
        self.idempotency_client_token: str | None = None
        # Optional httpx request event hook. ADCPClient installs one when a
        # SigningConfig is present; the hook attaches RFC 9421 Signature-Input
        # / Signature / Content-Digest headers to outgoing requests that the
        # seller's capability policy says should be signed. A2A consumes this
        # via its httpx client's event_hooks; MCP consumes it via a custom
        # httpx_client_factory passed to streamablehttp_client.
        self.signing_request_hook: Callable[[httpx.Request], Awaitable[None]] | None = None
        # Optional preflight paired with ``signing_request_hook``. Transport
        # adapters invoke it before handing a request to an httpx writer task
        # so the event hook never needs to fetch capabilities recursively on
        # the same MCP session.
        self.signing_capability_check: Callable[[], Awaitable[None]] | None = None
        # Schema validation modes — resolved by the owning ADCPClient via
        # ``configure_validation``. Class defaults match the TS port: warn
        # on requests (don't block partial payloads in error-path tests),
        # strict on responses (agent drift fails the task on first call).
        # Adapters instantiated directly without an ADCPClient inherit
        # these defaults; production callers flip responses to warn via
        # ``ADCPClient(validation=...)`` or an env override.
        self.request_validation_mode: ValidationMode = "warn"
        self.response_validation_mode: ValidationMode = "strict"
        # Optional hook applied to every outbound request params dict
        # before validation/send. The owning ADCPClient installs one to
        # auto-inject ``adcp_version`` from the per-instance pin. Returns
        # a new dict (the original is not mutated). Caller-supplied
        # values on the original dict win — the enricher is the default,
        # not an override.
        #
        # Contract: the validator runs on the enriched dict, so any field
        # the enricher injects must be either (a) declared in the request
        # schema, or (b) tolerated by the schema's ``additionalProperties``
        # policy. Top-level Request models in this SDK declare
        # ``extra="allow"`` (see ``AdCPBaseModel`` overrides in generated
        # types) — flipping any of them to ``extra="forbid"`` would break
        # this assumption silently.
        self.envelope_enricher: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def _enrich_outgoing_params(self, params: Any) -> Any:
        """Apply ``envelope_enricher`` to an outbound params dict.

        No-op for non-dict params (rare — most tool methods pass dicts
        from ``model_dump()``) and when no enricher is installed.
        """
        if self.envelope_enricher is None or not isinstance(params, dict):
            return params
        return self.envelope_enricher(params)

    def configure_validation(self, config: ValidationHookConfig | None) -> None:
        """Apply a client's :class:`ValidationHookConfig` to this adapter."""
        from adcp.validation.client_hooks import resolve_validation_modes

        req, resp = resolve_validation_modes(config)
        self.request_validation_mode = req
        self.response_validation_mode = resp

    # ========================================================================
    # Helper methods for response parsing
    # ========================================================================

    def _parse_response(
        self, raw_result: TaskResult[Any], response_type: type[T] | Any
    ) -> TaskResult[T]:
        """
        Parse raw TaskResult into typed TaskResult.

        Handles both MCP content arrays and A2A dict responses.
        Supports both single types and Union types (for oneOf discriminated unions).

        Args:
            raw_result: Raw TaskResult from adapter
            response_type: Expected Pydantic response type (can be a Union type)

        Returns:
            Typed TaskResult
        """
        # Handle failed results or interim states without data
        # For A2A: interim states (submitted/working) have data=None but success=True
        # For MCP: completed tasks always have data, missing data indicates failure
        if not raw_result.success or raw_result.data is None:
            # If already marked as unsuccessful, preserve that
            # If successful but no data (A2A interim state), preserve success=True
            return TaskResult[T](
                status=raw_result.status,
                data=None,
                message=raw_result.message,
                success=raw_result.success,  # Preserve original success state
                error=raw_result.error,  # Only use error if one was set
                metadata=raw_result.metadata,
                debug_info=raw_result.debug_info,
                idempotency_key=raw_result.idempotency_key,
                replayed=raw_result.replayed,
            )

        try:
            # Handle MCP content arrays
            if isinstance(raw_result.data, list):
                parsed_data = parse_mcp_content(raw_result.data, response_type)
            else:
                # Handle A2A or direct responses
                parsed_data = parse_json_or_text(raw_result.data, response_type)

            return TaskResult[T](
                status=raw_result.status,
                data=parsed_data,
                message=raw_result.message,  # Preserve human-readable message from protocol
                success=raw_result.success,
                error=raw_result.error,
                metadata=raw_result.metadata,
                debug_info=raw_result.debug_info,
                idempotency_key=raw_result.idempotency_key,
                replayed=raw_result.replayed,
            )
        except ValueError as e:
            # Parsing failed - return error result. Preserve idempotency_key
            # and replayed so callers can still correlate/suppress side-effects
            # even when response parsing fails.
            return TaskResult[T](
                status=TaskStatus.FAILED,
                error=f"Failed to parse response: {e}",
                message=raw_result.message,
                success=False,
                debug_info=raw_result.debug_info,
                idempotency_key=raw_result.idempotency_key,
                replayed=raw_result.replayed,
            )

    # ========================================================================
    # ADCP Protocol Methods - Type-safe, spec-aligned interface
    # Each adapter MUST implement these methods explicitly.
    # ========================================================================

    @abstractmethod
    async def get_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get advertising products."""
        pass

    async def list_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List products using the compact lifecycle."""
        raise NotImplementedError("list_products is not implemented by this protocol adapter")

    async def request_proposals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Request product proposals."""
        raise NotImplementedError("request_proposals is not implemented by this protocol adapter")

    async def refine_proposals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Refine product proposals."""
        raise NotImplementedError("refine_proposals is not implemented by this protocol adapter")

    async def decline_proposals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Decline product proposals."""
        raise NotImplementedError("decline_proposals is not implemented by this protocol adapter")

    async def buy_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Buy products directly."""
        raise NotImplementedError("buy_products is not implemented by this protocol adapter")

    async def accept_proposal(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Accept a product proposal."""
        raise NotImplementedError("accept_proposal is not implemented by this protocol adapter")

    async def control_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Control an existing media buy."""
        raise NotImplementedError("control_media_buy is not implemented by this protocol adapter")

    @abstractmethod
    async def list_creative_formats(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List supported creative formats."""
        pass

    @abstractmethod
    async def sync_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync creatives."""
        pass

    @abstractmethod
    async def list_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creatives."""
        pass

    @abstractmethod
    async def get_media_buy_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buy delivery."""
        pass

    @abstractmethod
    async def get_media_buys(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buys with status, creative approval state, and optional delivery snapshots."""
        pass

    @abstractmethod
    async def get_signals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get signals."""
        pass

    @abstractmethod
    async def activate_signal(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Activate signal."""
        pass

    @abstractmethod
    async def provide_performance_feedback(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Provide performance feedback."""
        pass

    @abstractmethod
    async def log_event(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Log event."""
        pass

    @abstractmethod
    async def sync_event_sources(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync event sources."""
        pass

    @abstractmethod
    async def sync_audiences(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync audiences."""
        pass

    @abstractmethod
    async def sync_catalogs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync catalogs."""
        pass

    @abstractmethod
    async def create_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create media buy."""
        pass

    @abstractmethod
    async def update_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update media buy."""
        pass

    @abstractmethod
    async def build_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Build creative."""
        pass

    @abstractmethod
    async def preview_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Preview creative."""
        pass

    @abstractmethod
    async def validate_input(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Validate creative input against a format declaration."""
        pass

    @abstractmethod
    async def get_creative_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get creative delivery."""
        pass

    @abstractmethod
    async def list_transformers(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creative transformers."""
        pass

    @abstractmethod
    async def list_accounts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List accounts."""
        pass

    @abstractmethod
    async def sync_accounts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync accounts."""
        pass

    @abstractmethod
    async def get_account_financials(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get account financials."""
        pass

    @abstractmethod
    async def report_usage(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report account usage."""
        pass

    @abstractmethod
    async def list_tools(self) -> list[str]:
        """
        List available tools from the agent.

        Returns:
            List of tool names
        """
        pass

    @abstractmethod
    async def get_agent_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata.

        Returns agent card information including:
        - Agent name, description, version
        - AdCP version (from extensions.adcp.adcp_version)
        - Supported protocols (from extensions.adcp.protocols_supported)
        - Available tools/skills

        Returns:
            Dictionary with agent metadata including AdCP extension fields
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Close the adapter and clean up resources.

        Implementations should close any open connections, clients, or other resources.
        """
        pass

    # ========================================================================
    # V3 Protocol Methods - Protocol Discovery
    # ========================================================================

    @abstractmethod
    async def get_adcp_capabilities(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get AdCP capabilities from the agent."""
        pass

    async def sync_agent_notification_configs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Replace caller-scoped agent notification subscribers."""
        raise NotImplementedError(
            "sync_agent_notification_configs is not implemented by this protocol adapter"
        )

    @abstractmethod
    async def get_task_status(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get task status from the agent."""
        pass

    @abstractmethod
    async def list_tasks(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List tasks from the agent."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Content Standards
    # ========================================================================

    @abstractmethod
    async def create_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create content standards configuration."""
        pass

    @abstractmethod
    async def get_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get content standards configuration."""
        pass

    @abstractmethod
    async def list_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List content standards configurations."""
        pass

    @abstractmethod
    async def update_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update content standards configuration."""
        pass

    @abstractmethod
    async def calibrate_content(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Calibrate content against standards."""
        pass

    @abstractmethod
    async def validate_content_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Validate content delivery against standards."""
        pass

    @abstractmethod
    async def get_media_buy_artifacts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get artifacts associated with a media buy."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Governance
    # ========================================================================

    @abstractmethod
    async def get_creative_features(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Evaluate governance features for a creative."""
        pass

    @abstractmethod
    async def sync_plans(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync campaign governance plans."""
        pass

    @abstractmethod
    async def check_governance(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Check an action against campaign governance."""
        pass

    @abstractmethod
    async def report_plan_outcome(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report the outcome of a governed action."""
        pass

    async def report_plan_adjustment(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Report or review an adjustment to a governed outcome."""
        raise NotImplementedError(
            "report_plan_adjustment is not implemented by this protocol adapter"
        )

    @abstractmethod
    async def get_plan_audit_logs(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Retrieve governance audit logs for plans."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Sponsored Intelligence
    # ========================================================================

    @abstractmethod
    async def si_get_offering(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get sponsored intelligence offering."""
        pass

    @abstractmethod
    async def si_initiate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Initiate sponsored intelligence session."""
        pass

    @abstractmethod
    async def si_send_message(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Send message in sponsored intelligence session."""
        pass

    @abstractmethod
    async def si_terminate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Terminate sponsored intelligence session."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Governance (Property Lists)
    # ========================================================================

    @abstractmethod
    async def create_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create a property list for governance."""
        pass

    @abstractmethod
    async def get_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get a property list with optional resolution."""
        pass

    @abstractmethod
    async def list_property_lists(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List property lists."""
        pass

    @abstractmethod
    async def update_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update a property list."""
        pass

    @abstractmethod
    async def delete_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Delete a property list."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Governance (Collection Lists)
    # ========================================================================

    @abstractmethod
    async def create_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create a collection list for governance."""
        pass

    @abstractmethod
    async def get_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get a collection list with optional resolution."""
        pass

    @abstractmethod
    async def list_collection_lists(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List collection lists."""
        pass

    @abstractmethod
    async def update_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update a collection list."""
        pass

    @abstractmethod
    async def delete_collection_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Delete a collection list."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Governance (Sync Governance)
    # ========================================================================

    @abstractmethod
    async def sync_governance(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync governance agents attached to an account."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Temporal Matching Protocol (TMP)
    # ========================================================================

    @abstractmethod
    async def context_match(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Match ad context to buyer packages."""
        pass

    @abstractmethod
    async def identity_match(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Match user identity for package eligibility."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Brand Rights
    # ========================================================================

    @abstractmethod
    async def get_brand_identity(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get brand identity information."""
        pass

    @abstractmethod
    async def get_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get available rights for licensing."""
        pass

    @abstractmethod
    async def acquire_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Acquire rights for brand content usage."""
        pass

    @abstractmethod
    async def update_rights(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update terms of an existing rights acquisition."""
        pass

    @abstractmethod
    async def verify_brand_claim(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Verify a single brand claim."""
        pass

    @abstractmethod
    async def verify_brand_claims(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Verify multiple brand claims."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Compliance
    # ========================================================================

    @abstractmethod
    async def comply_test_controller(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Compliance test controller (sandbox only)."""
        pass
