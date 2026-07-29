"""Decorator-based server builder for ADCP.

An alternative to the class-based ADCPHandler for simple agents:

    from adcp.server import adcp_server, serve
    from adcp.server.responses import capabilities_response, products_response

    server = adcp_server("my-seller", version="1.0.0")

    @server.get_products
    async def get_products(params, context=None):
        return products_response(MY_PRODUCTS)

    @server.get_adcp_capabilities
    async def capabilities(params, context=None):
        return capabilities_response(["media_buy"])

    if __name__ == "__main__":
        serve(server, name="my-seller")
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from adcp.server.base import ADCPHandler

# Maps handler method names to their ADCP capability domain.
# Used by auto-capabilities to detect which domains the agent supports.
HANDLER_TO_DOMAIN: dict[str, str] = {
    # Media buy
    "get_products": "media_buy",
    "create_media_buy": "media_buy",
    "update_media_buy": "media_buy",
    "get_media_buys": "media_buy",
    "get_media_buy_delivery": "media_buy",
    "provide_performance_feedback": "media_buy",
    "list_creative_formats": "media_buy",
    "sync_creatives": "media_buy",
    "list_creatives": "media_buy",
    "sync_event_sources": "media_buy",
    "log_event": "media_buy",
    "sync_audiences": "media_buy",
    "sync_catalogs": "media_buy",
    # Creative
    "build_creative": "creative",
    "preview_creative": "creative",
    "validate_input": "creative",
    "get_creative_delivery": "creative",
    "get_creative_features": "creative",
    "list_transformers": "creative",
    # Protocol
    "get_task_status": "protocol",
    "list_tasks": "protocol",
    # Signals
    "get_signals": "signals",
    "activate_signal": "signals",
    # Account
    "list_accounts": "media_buy",
    "sync_accounts": "media_buy",
    "get_account_financials": "media_buy",
    "report_usage": "media_buy",
    "sync_governance": "media_buy",
    # Governance
    "create_property_list": "governance",
    "update_property_list": "governance",
    "get_property_list": "governance",
    "list_property_lists": "governance",
    "delete_property_list": "governance",
    "create_content_standards": "governance",
    "update_content_standards": "governance",
    "get_content_standards": "governance",
    "list_content_standards": "governance",
    "calibrate_content": "governance",
    "validate_content_delivery": "governance",
    "get_media_buy_artifacts": "governance",
    "sync_plans": "governance",
    "check_governance": "governance",
    "report_plan_outcome": "governance",
    "get_plan_audit_logs": "governance",
    # Sponsored Intelligence
    "si_get_offering": "sponsored_intelligence",
    "si_initiate_session": "sponsored_intelligence",
    "si_send_message": "sponsored_intelligence",
    "si_terminate_session": "sponsored_intelligence",
    # Brand
    "get_brand_identity": "brand",
    "verify_brand_claim": "brand",
    "verify_brand_claims": "brand",
    "get_rights": "brand",
    "acquire_rights": "brand",
    "update_rights": "brand",
    # Collection Lists
    "create_collection_list": "governance",
    "get_collection_list": "governance",
    "list_collection_lists": "governance",
    "update_collection_list": "governance",
    "delete_collection_list": "governance",
    # TMP
    "context_match": "media_buy",
    "identity_match": "media_buy",
    # Compliance
    "comply_test_controller": "media_buy",
}

# Public wire names stay fixed by the protocol, while adopter-facing methods
# that expose raw named-format identity are conspicuously legacy-named.
LEGACY_ADOPTER_TO_WIRE: dict[str, str] = {
    "build_creative_legacy": "build_creative",
    "list_creative_formats_legacy": "list_creative_formats",
    "preview_creative_legacy": "preview_creative",
}
_LEGACY_ONLY_WIRE_NAMES = frozenset(LEGACY_ADOPTER_TO_WIRE.values())


class ADCPServerBuilder:
    """Declarative server builder using decorators.

    Use ``adcp_server()`` to create an instance, then register handlers
    with decorators. The builder can be passed directly to ``serve()``.

    Example::

        server = adcp_server("my-seller")

        @server.get_products
        async def get_products(params, context=None):
            return products_response(MY_PRODUCTS)

        serve(server, name="my-seller")
    """

    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        adcp_version: str | None = None,
    ) -> None:
        from adcp._version import resolve_adcp_version

        self.name = name
        self.version = version
        self._adcp_version: str = resolve_adcp_version(adcp_version)
        self._handlers: dict[str, Callable[..., Any]] = {}

    def get_adcp_version(self) -> str:
        """Return the AdCP protocol release this server is pinned to.

        Resolved at construction from the ``adcp_version`` kwarg, with
        fallback to the SDK's compile-time pin (``ADCP_VERSION``
        packaged with the wheel). Stage 2 plumbing — Stage 3 will use
        this to select which schema set the server validates handler
        responses against and which capability shape it advertises.
        """
        return self._adcp_version

    def __getattr__(self, task_name: str) -> Callable[..., Any]:
        """Return a decorator that registers a handler for the given task."""
        if task_name.startswith("_"):
            raise AttributeError(task_name)

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if task_name in _LEGACY_ONLY_WIRE_NAMES:
                raise ValueError(
                    f"'{task_name}' carries legacy creative identity; register "
                    f"'@server.{task_name}_legacy' instead"
                )
            wire_name = LEGACY_ADOPTER_TO_WIRE.get(task_name, task_name)
            if wire_name not in HANDLER_TO_DOMAIN and wire_name != "get_adcp_capabilities":
                raise ValueError(f"'{task_name}' is not a known ADCP task. " f"Check for typos.")
            self._handlers[task_name] = fn
            return fn

        return decorator

    def _detect_domains(self) -> list[str]:
        """Detect which ADCP domains the registered handlers cover."""
        domains: set[str] = set()
        for handler_name in self._handlers:
            domain = HANDLER_TO_DOMAIN.get(LEGACY_ADOPTER_TO_WIRE.get(handler_name, handler_name))
            if domain:
                domains.add(domain)
        return sorted(domains)

    def build_handler(self) -> ADCPHandler[Any]:
        """Build an ADCPHandler from registered decorators.

        If ``get_adcp_capabilities`` is not registered, it will be
        auto-generated from the detected domains.
        """
        handlers = dict(self._handlers)

        # Auto-generate capabilities if not provided
        if "get_adcp_capabilities" not in handlers:
            domains = self._detect_domains()
            if domains:
                from adcp.server.responses import capabilities_response

                pinned_version = self._adcp_version

                async def auto_capabilities(params: Any, context: Any = None) -> dict[str, Any]:
                    return capabilities_response(
                        domains,
                        adcp_version=pinned_version,
                    )

                handlers["get_adcp_capabilities"] = auto_capabilities

        # Create a dynamic subclass. ``ADCPHandler[Any]`` because the
        # decorator-builder path doesn't thread a specific ToolContext
        # subclass — callers who want typed context go through the
        # class-based ``ADCPHandler[MyContext]`` route instead.
        class DynamicHandler(ADCPHandler[Any]):
            pass

        for task_name, fn in handlers.items():
            # Wrap standalone functions to accept self
            async def _bound_method(
                self: Any,
                params: Any,
                context: Any = None,
                _fn: Callable[..., Any] = fn,
            ) -> Any:
                return await _fn(params, context)

            setattr(DynamicHandler, task_name, _bound_method)

        return DynamicHandler()


def adcp_server(name: str, **kwargs: Any) -> ADCPServerBuilder:
    """Create a decorator-based ADCP server builder.

    Args:
        name: Server name.
        **kwargs: Additional configuration (e.g., version="1.0.0").

    Returns:
        An ADCPServerBuilder instance.
    """
    return ADCPServerBuilder(name, **kwargs)
