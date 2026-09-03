"""Built-in comply_test_controller for ADCP servers.

Provides TestControllerStore and register_test_controller() so that
storyboard tests can manipulate server state (force status transitions,
simulate delivery, etc.) without agents needing to implement the
comply_test_controller tool by hand.

Usage:
    from adcp.server import serve, ADCPHandler
    from adcp.server.test_controller import TestControllerStore, register_test_controller

    class MyStore(TestControllerStore):
        async def force_account_status(self, account_id, status):
            old = self.accounts[account_id]["status"]
            self.accounts[account_id]["status"] = status
            return {"previous_state": old, "current_state": status}

    store = MyStore()
    serve(MySeller(), name="my-agent", test_controller=store)

Header-driven compatibility:
    Store methods MAY accept a keyword-only ``context: ToolContext | None``
    parameter. When the server was configured with a ``context_factory``,
    the dispatcher calls the factory per request and threads the
    resulting ``ToolContext`` into the store method. This lets sellers
    whose test runtime reads request headers (e.g.
    ``AdCPTestContext.from_headers(request.headers)``) compose the
    storyboard-driven ``comply_test_controller`` skill with their
    existing header-driven mock state — populate the test context in
    the ``context_factory`` (from a ContextVar set by your HTTP
    middleware) and read it off ``context.metadata`` inside the store.
    Stores that don't declare ``context`` on a method keep working
    unchanged — the dispatcher only passes ``context`` to methods whose
    signature accepts it.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from adcp.decisioning.context import AuthInfo
    from adcp.server.base import ToolContext
    from adcp.server.serve import ContextFactory


logger = logging.getLogger(__name__)


class _AccountResolver(Protocol):
    """Async-or-sync callable that resolves a wire ``account`` ref to a
    framework :class:`Account`-shaped object.

    Adopters who use the v6 :class:`DecisioningPlatform` get this hooked
    automatically by ``register_test_controller`` — the framework wraps
    ``platform.accounts.resolve`` so the comply controller can apply the
    sandbox-authority gate against the resolved account.

    The resolver receives the request's verified ``auth_info`` (None for
    capability-probe / pre-bootstrap calls). Adopters using
    ``FromAuthAccounts`` rely on auth_info to find the principal's
    account; resolvers that don't need it ignore the kwarg.

    Returns the resolved account on success. Raises on miss /
    unauthorized / other resolution failure — the gate treats a raise as
    fail-closed (DENY), NOT as "no account, fall through to wire flag".
    Resolution success returning ``None`` (genuine "no account on this
    request" case, e.g. capability probes that don't carry an account
    ref) is the only path that consults the wire-ref / context.sandbox
    fallback.
    """

    def __call__(self, ref: dict[str, Any] | None, *, auth_info: AuthInfo | None = None) -> Any: ...


class _InsecureAllowAllSentinel:
    """Marker type for :data:`INSECURE_ALLOW_ALL`.

    The gate checks for this exact sentinel object via ``is`` and
    short-circuits to admit. Implementing as a class (not a plain
    sentinel object) lets the type system express the intent and keeps
    the wire-protocol-compatible callable shape if anything in the
    framework probes it.
    """

    def __call__(self, ref: dict[str, Any] | None, *, auth_info: AuthInfo | None = None) -> None:
        return None


# Sentinel resolver — admits every request unconditionally, BYPASSING
# the sandbox-authority gate entirely. ONLY for use in tests / dev
# fixtures where the harness explicitly opts out of the gate. Adopter
# production code MUST NOT pass this; it bypasses the trust boundary
# that protects live principals from the comply controller.
INSECURE_ALLOW_ALL: _AccountResolver = _InsecureAllowAllSentinel()


# Scenario names — must match the AdCP comply_test_controller schema
SCENARIOS = [
    "expire_account_change_cursor",
    "force_creative_status",
    "force_creative_purge",
    "force_account_status",
    "force_media_buy_status",
    "force_create_media_buy_arm",
    "force_get_products_arm",
    "force_get_signals_arm",
    "force_task_completion",
    "force_session_status",
    "simulate_delivery",
    "simulate_budget_spend",
    # seed_* scenarios pre-populate storyboard fixtures (AdCP 3.0.1)
    "seed_product",
    "seed_pricing_option",
    "seed_creative",
    "seed_plan",
    "seed_media_buy",
    "seed_account",
    "seed_rights_grant",
    "seed_creative_format",
    "seed_measurement_catalog",
    "query_upstream_traffic",
    "query_provenance_audit_observations",
    "force_upstream_unavailable",
    "catalog_item_availability_probe",
    "compact_product_lifecycle_probe",
    "compact_direct_buy_lifecycle_probe",
]

_MAX_TASK_ID = 128
_MAX_MESSAGE = 2000
_MAX_RESULT_BYTES = 256 * 1024  # 256 KB soft cap per AdCP 3.0.1

# Before the dispatcher became signature-driven, these optional arguments
# were always supplied with ``None`` when absent. Preserve that behavior for
# existing store overrides whose signatures made the arguments positional/
# required even though the wire fields are optional.
_LEGACY_OPTIONAL_SCENARIO_PARAMS: dict[str, tuple[str, ...]] = {
    "force_creative_status": ("rejection_reason",),
    "force_media_buy_status": ("rejection_reason",),
    "force_session_status": ("termination_reason",),
    "force_create_media_buy_arm": ("task_id", "message"),
    "simulate_delivery": (
        "impressions",
        "clicks",
        "conversions",
        "reported_spend",
    ),
    "simulate_budget_spend": ("account_id", "media_buy_id"),
    "seed_product": ("fixture", "product_id"),
    "seed_pricing_option": ("fixture", "product_id", "pricing_option_id"),
    "seed_creative": ("fixture", "creative_id"),
    "seed_plan": ("fixture", "plan_id"),
    "seed_media_buy": ("fixture", "media_buy_id"),
    "seed_creative_format": ("fixture", "format_id"),
}


class TestControllerError(Exception):
    """Typed error for test controller store methods.

    Raise this from your TestControllerStore methods to return structured
    error responses. The dispatcher catches it and converts to the AdCP
    comply_test_controller error format.

    Example:
        async def force_media_buy_status(self, media_buy_id, status, rejection_reason=None):
            prev = self.media_buys.get(media_buy_id)
            if prev is None:
                raise TestControllerError("NOT_FOUND", f"Media buy {media_buy_id} not found")
            if prev in ("completed", "rejected", "canceled"):
                raise TestControllerError(
                    "INVALID_TRANSITION",
                    f"Cannot transition from {prev}",
                    current_state=prev,
                )
            self.media_buys[media_buy_id] = status
            return {"previous_state": prev, "current_state": status}
    """

    def __init__(self, code: str, message: str, current_state: str | None = None):
        super().__init__(message)
        self.code = code
        self.current_state = current_state


class TestControllerStore:
    """Base class for test controller state management.

    Subclass this and override the methods for scenarios your agent supports.
    Methods you don't override will be reported as unsupported scenarios
    and excluded from list_scenarios.

    Raise TestControllerError for structured error responses.

    Methods MAY declare an optional keyword-only ``context: ToolContext |
    None = None`` parameter. When present, the dispatcher threads the
    ``ToolContext`` built by the server's ``context_factory`` into the
    call — header-driven mock state (e.g. ``AdCPTestContext.from_headers``)
    populated in the factory is readable off ``context.metadata``.
    Stores that don't declare ``context`` keep working unchanged.
    """

    async def expire_account_change_cursor(
        self,
        account_id: str,
        *,
        account: dict[str, Any] | None = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Rotate an account's authorization-scope epoch.

        Returns a state transition describing the previous and current
        cursor epochs. ``account`` is the verified sandbox account assertion
        carried by the controller request.
        """
        raise NotImplementedError

    async def force_creative_status(
        self,
        creative_id: str,
        status: str,
        rejection_reason: str | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Force a creative to a given status.

        Returns:
            {"previous_state": str, "current_state": str}
        """
        raise NotImplementedError

    async def force_creative_purge(
        self,
        creative_id: str,
        purge_kind: str | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Soft-delete or permanently purge a sandbox creative.

        Returns:
            {"previous_state": str, "current_state": str}
        """
        raise NotImplementedError

    async def force_account_status(
        self,
        account_id: str,
        status: str,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Force an account to a given status.

        Returns:
            {"previous_state": str, "current_state": str}
        """
        raise NotImplementedError

    async def force_media_buy_status(
        self,
        media_buy_id: str,
        status: str,
        rejection_reason: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Force a media buy to a given status.

        Returns:
            {"previous_state": str, "current_state": str}
        """
        raise NotImplementedError

    async def force_session_status(
        self,
        session_id: str,
        status: str,
        termination_reason: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Force a session to a given status.

        Returns:
            {"previous_state": str, "current_state": str}
        """
        raise NotImplementedError

    async def force_create_media_buy_arm(
        self,
        arm: str,
        task_id: str | None = None,
        message: str | None = None,
        *,
        account: dict[str, Any] | None = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Register a single-shot directive for the next create_media_buy call.

        The directive is consumed by the next create_media_buy call from the
        same authenticated sandbox account, then cleared. A second registration
        before consumption overwrites the first.

        Args:
            arm: Response arm — ``'submitted'`` or ``'input-required'``.
            task_id: Required when ``arm='submitted'``. The seller MUST emit
                this exact value on the next create_media_buy task envelope
                and accept it on subsequent tasks/get calls within the same
                sandbox account. Max 128 chars.
            message: Optional plain-text note surfaced on the response.
                Max 2000 chars.
            account: Caller-supplied account object from the MCP request.
                Implementations use this for single-shot-per-account isolation.
            context: Optional ToolContext from the server's context_factory.

        Returns:
            ForcedDirectiveSuccess::

                {"success": True, "forced": {"arm": str, "task_id"?: str}}

        Raises:
            TestControllerError: with code ``"NOT_FOUND"`` if the caller
                account is not recognized, or ``"INVALID_PARAMS"`` on
                validation failure.
        """
        raise NotImplementedError

    async def force_get_products_arm(
        self,
        arm: str,
        task_id: str | None = None,
        message: str | None = None,
        reason: str | None = None,
        suggestions: list[str] | None = None,
        *,
        account: dict[str, Any] | None = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Shape the next ``get_products`` call into a deterministic arm.

        ``submitted`` requires ``task_id``; ``rejected`` requires ``reason``
        and may include buyer-facing ``suggestions``.

        Returns:
            {"forced": {"arm": str, ...}}
        """
        raise NotImplementedError

    async def force_get_signals_arm(
        self,
        arm: str,
        task_id: str,
        message: str | None = None,
        *,
        account: dict[str, Any] | None = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Shape the next ``get_signals`` call into the submitted arm.

        Returns:
            {"forced": {"arm": "submitted", "task_id": str}}
        """
        raise NotImplementedError

    async def force_task_completion(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        account: dict[str, Any] | None = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Resolve a previously-submitted task to ``'completed'``.

        Isolation and idempotency contract:

        - **Cross-account replay** — raise ``TestControllerError("NOT_FOUND", ...)``
          when the task_id was registered by a different sandbox account.
        - **Identical-params replay** — idempotent; return the same
          ``StateTransitionSuccess``.
        - **Diverging-params replay** against a terminal task — raise
          ``TestControllerError("INVALID_TRANSITION", ...,
          current_state="completed")``.

        Args:
            task_id: Task handle to resolve. Max 128 chars.
            result: Completion payload (non-empty object). Implementations
                SHOULD validate it against the response branch for the task's
                original method and MUST reject payloads that fail that check
                with ``TestControllerError("INVALID_PARAMS", ...)``.
            account: Caller-supplied account object from the MCP request.
                Used for cross-account isolation.
            context: Optional ToolContext from the server's context_factory.

        Returns:
            StateTransitionSuccess::

                {"success": True, "previous_state": "submitted",
                 "current_state": "completed"}

        Raises:
            TestControllerError: with code ``"NOT_FOUND"`` if the task_id
                is unknown or owned by a different account,
                ``"INVALID_TRANSITION"`` if the task is already terminal and
                params diverge, or ``"INVALID_PARAMS"`` on validation failure.
        """
        raise NotImplementedError

    async def simulate_delivery(
        self,
        media_buy_id: str,
        impressions: int | None = None,
        clicks: int | None = None,
        plays: int | None = None,
        dooh_metrics: dict[str, Any] | None = None,
        conversions: int | None = None,
        delivery_date: str | None = None,
        conversion_value: float | None = None,
        commissionable_value: float | None = None,
        reported_spend: dict[str, Any] | None = None,
        reach: float | None = None,
        frequency: float | None = None,
        reach_window: dict[str, Any] | None = None,
        viewability: dict[str, Any] | None = None,
        vendor_metric_values: list[dict[str, Any]] | None = None,
        vendor_metric_values_by_package: dict[str, list[dict[str, Any]]] | None = None,
        not_yet_measurable_vendor_metrics: list[dict[str, Any]] | None = None,
        not_yet_measurable_vendor_metrics_by_package: dict[str, list[dict[str, Any]]] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Simulate delivery metrics for a media buy.

        Returns:
            {"simulated": {...}, "cumulative": {...} | None}
        """
        raise NotImplementedError

    async def simulate_budget_spend(
        self,
        spend_percentage: float,
        account_id: str | None = None,
        media_buy_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Simulate budget spend to a percentage.

        Returns:
            {"simulated": {...}}
        """
        raise NotImplementedError

    async def seed_product(
        self,
        fixture: dict[str, Any] | None = None,
        product_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate a product fixture for storyboard tests (AdCP 3.0.1).

        Returns:
            {"product_id": str}
        """
        raise NotImplementedError

    async def seed_pricing_option(
        self,
        fixture: dict[str, Any] | None = None,
        product_id: str | None = None,
        pricing_option_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate a pricing option fixture for storyboard tests (AdCP 3.0.1).

        Returns:
            {"pricing_option_id": str}
        """
        raise NotImplementedError

    async def seed_creative(
        self,
        fixture: dict[str, Any] | None = None,
        creative_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate a creative fixture for storyboard tests (AdCP 3.0.1).

        Returns:
            {"creative_id": str}
        """
        raise NotImplementedError

    async def seed_plan(
        self,
        fixture: dict[str, Any] | None = None,
        plan_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate a plan fixture for storyboard tests (AdCP 3.0.1).

        Returns:
            {"plan_id": str}
        """
        raise NotImplementedError

    async def seed_media_buy(
        self,
        fixture: dict[str, Any] | None = None,
        media_buy_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate a media buy fixture for storyboard tests (AdCP 3.0.1).

        Returns:
            {"media_buy_id": str}
        """
        raise NotImplementedError

    async def seed_account(
        self,
        account_id: str,
        fixture: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate an advertiser account fixture."""
        raise NotImplementedError

    async def seed_rights_grant(
        self,
        rights_id: str,
        fixture: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate a rights-grant fixture."""
        raise NotImplementedError

    async def seed_creative_format(
        self,
        fixture: dict[str, Any] | None = None,
        format_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Pre-populate a creative format fixture for storyboard tests (AdCP 3.0.1).

        The seller MUST expose the seeded format_id in list_creative_formats
        responses for the duration of the compliance session.

        Returns:
            {"format_id": str}
        """
        raise NotImplementedError

    async def seed_measurement_catalog(
        self,
        vendor: dict[str, Any],
        metrics: list[dict[str, Any]],
        fixture: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Seed a measurement vendor's metric catalog."""
        raise NotImplementedError

    async def query_upstream_traffic(
        self,
        since_timestamp: str | None = None,
        endpoint_pattern: str | None = None,
        limit: int | None = None,
        attestation_mode: str | None = None,
        identifier_value_digests: list[str] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Return caller-scoped outbound calls recorded by the sandbox."""
        raise NotImplementedError

    async def query_provenance_audit_observations(
        self,
        creative_id: str,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Return sandbox audit observations recorded for a creative."""
        raise NotImplementedError

    async def force_upstream_unavailable(
        self,
        tool: str,
        upstream_name: str | None = None,
        cache_age_seconds: int | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Mark a tool's upstream dependency unavailable for the session."""
        raise NotImplementedError

    async def catalog_item_availability_probe(
        self,
        operation: str,
        catalog_id: str,
        item_id: str,
        catalog_generation: str | None = None,
        target_time: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Apply a deterministic catalog availability probe operation."""
        raise NotImplementedError

    async def compact_product_lifecycle_probe(
        self,
        operation: str,
        product_id: str | None = None,
        proposal_id: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Prepare compact proposal lifecycle state or expire a proposal."""
        raise NotImplementedError

    async def compact_direct_buy_lifecycle_probe(
        self,
        operation: str,
        product_id: str,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Prepare deterministic compact direct-buy lifecycle state."""
        raise NotImplementedError


def _list_scenarios(store: TestControllerStore) -> list[str]:
    """Detect which scenarios a store actually implements.

    Checks whether each scenario method is overridden in the store's
    own class (not just inherited from TestControllerStore).
    """
    implemented = []
    store_cls = type(store)
    for scenario in SCENARIOS:
        # Check if this class or any non-TestControllerStore ancestor defines it
        for cls in store_cls.__mro__:
            if cls is TestControllerStore:
                break
            if scenario in cls.__dict__:
                implemented.append(scenario)
                break
    return implemented


def _controller_error(error: str, detail: str, current_state: str | None = None) -> dict[str, Any]:
    """Format a test controller error response."""
    resp: dict[str, Any] = {
        "success": False,
        "error": error,
        "error_detail": detail,
    }
    if current_state is not None:
        resp["current_state"] = current_state
    return resp


def _accepts_kwarg(method: Any, name: str) -> bool:
    """True when ``method``'s signature accepts ``name`` as a keyword argument.

    Used by the dispatcher to decide whether to pass optional kwargs
    (``context``, ``account``) to store methods. Methods that don't
    declare the kwarg keep working unchanged; methods that do get the
    value threaded in.

    Counts as an opt-in:

    - ``*, name: ...`` — keyword-only (the documented recipe).
    - ``name: ...`` as a regular positional-or-keyword parameter.
    - ``**kwargs`` — accepts any keyword.

    Does **not** count:

    - ``name`` as positional-only (before ``/``).
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    allowed = {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in allowed:
            return True
    return False


def _accepts_context_kwarg(method: Any) -> bool:
    """True when ``method``'s signature accepts ``context=`` by keyword."""
    return _accepts_kwarg(method, "context")


def _accepted_scenario_kwargs(method: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Return wire scenario params accepted by a store override.

    Scenario payloads are additive: the protocol can introduce an optional
    parameter before the SDK publishes a matching base-class signature.  The
    dispatcher therefore follows the override's signature instead of keeping
    a second, hand-maintained parameter allowlist.  Explicit parameters and
    ``**kwargs`` are both opt-ins; transport-level ``account`` and ``context``
    are reserved for the dispatcher's separately verified values.
    """
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return {}

    reserved = {"account", "context"}
    parameters = signature.parameters.values()
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters):
        return {name: value for name, value in params.items() if name not in reserved}

    allowed_kinds = {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    accepted = {
        param.name
        for param in signature.parameters.values()
        if param.kind in allowed_kinds and param.name not in reserved
    }
    return {name: value for name, value in params.items() if name in accepted}


def _require_scenario_params(params: dict[str, Any], *names: str) -> None:
    """Raise ``KeyError`` when a scenario omits one of its required params."""
    for name in names:
        if name not in params:
            raise KeyError(name)


def _extract_auth_info_from_context(context: ToolContext | None) -> AuthInfo | None:
    """Pull verified ``AuthInfo`` from the ``ToolContext.metadata``.

    Mirrors :meth:`PlatformHandler._extract_auth_info` so the comply
    gate's resolver call sees the same auth signal the dispatch path
    threads into ``platform.accounts.resolve``. Returns ``None`` when
    no auth metadata is present (capability probes / pre-bootstrap
    requests). The sandbox-gate's resolver MUST handle ``None``
    correctly — :class:`FromAuthAccounts` raises ``AUTH_INVALID``,
    which the gate now treats as DENY (not fall-through).
    """
    if context is None:
        return None
    raw = context.metadata.get("adcp.auth_info") if context.metadata else None
    if raw is None:
        return None
    # Late import to avoid circular at module import time.
    from adcp.decisioning.context import AuthInfo as _AuthInfo

    if isinstance(raw, _AuthInfo):
        return raw
    if isinstance(raw, dict):
        return _AuthInfo._from_legacy_dict(raw)
    return None


async def _apply_sandbox_gate(
    params: dict[str, Any],
    account_resolver: _AccountResolver | None,
    auth_info: AuthInfo | None = None,
) -> dict[str, Any] | None:
    """Phase 1 sandbox-authority gate for ``comply_test_controller``.

    Order of admission (mirrors JS PR #1453):

    1. ``INSECURE_ALLOW_ALL`` sentinel: explicit opt-out — admit
       unconditionally. Tests / dev fixtures only.

    2. Resolve the account via ``account_resolver`` (passing the
       request's verified ``auth_info``). The framework reads the wire
       ref from top-level ``account`` (extended shape) or
       ``context.account`` (canonical AdCP).

       - Resolver raises → DENY (fail-closed). A misbehaving resolver
         MUST NOT fall through to the wire flag — the buyer-supplied
         ``account.sandbox: true`` is not a trust signal once a real
         resolver is wired.
       - Resolver returns an account → ``mode in {sandbox, mock}`` (or
         legacy ``sandbox=True``) admits; ``mode='live'`` denies
         regardless of any wire signal.
       - Resolver returns ``None`` → no account on this request
         (capability probe / pre-bootstrap). Fall through to wire-ref /
         context.sandbox / env fallback.

    3. Wire-ref / context.sandbox fallback: ONLY consulted when the
       resolver cleanly returned ``None`` (or no resolver was wired).
       The buyer's wire claim NEVER overrides a resolved live account
       AND never supplants a resolver error.

    4. Env fallback: ``ADCP_SANDBOX=1`` admits (deprecated, kept for
       back-compat with adopters who haven't migrated to ``mode``).

    5. Default: DENY. When no resolver is wired AND ``ADCP_SANDBOX`` is
       unset, the gate fail-closes — adopters who manually wire the
       comply controller (subclassing :class:`ADCPHandler` /
       :class:`ComplianceHandler` without going through
       ``decisioning.serve``) are protected by default. To bypass for
       tests / dev, pass ``account_resolver=INSECURE_ALLOW_ALL`` or set
       ``ADCP_SANDBOX=1``.

    **Fail-closed env-fallback guard.** When the env fallback is the
    only signal that would admit AND this process has resolved any
    explicit ``mode='live'`` account, the function raises a runtime
    error loudly. That pairing is a deployment misconfiguration —
    silent admission would unlock the comply controller for live
    principals. See :mod:`adcp.decisioning.observed_modes`.

    Returns ``None`` when admitted (caller proceeds to dispatch); a
    controller-shaped error dict when refused.
    """
    # 1. Explicit insecure opt-out — admit unconditionally. Tests / dev
    # fixtures pass this sentinel to bypass the gate.
    if account_resolver is INSECURE_ALLOW_ALL:
        return None

    env_sandbox_raw = os.environ.get("ADCP_SANDBOX") == "1"

    # 5. Default fail-closed when no resolver AND no env opt-in. Adopters
    # who have not wired a resolver get protection by default — buyers
    # cannot hit the controller without the operator explicitly opting
    # in via ``account_resolver=`` or ``ADCP_SANDBOX=1``.
    if account_resolver is None and not env_sandbox_raw:
        return _controller_error(
            "PERMISSION_DENIED",
            "comply_test_controller is gated by sandbox-authority. No "
            "account_resolver is wired and ADCP_SANDBOX is unset. Wire a "
            "resolver via decisioning.serve(test_controller=...), set "
            "ADCP_SANDBOX=1 in dev, or pass "
            "account_resolver=INSECURE_ALLOW_ALL in tests.",
        )

    # 2. Resolve account
    account_ref = params.get("account")
    if not isinstance(account_ref, dict) or not account_ref:
        # AdCP canonical routing puts the ref at context.account; the
        # extended top-level shape is what existing storyboards use.
        # First non-null wins.
        wire_context = params.get("context")
        if isinstance(wire_context, dict):
            ctx_ref = wire_context.get("account")
            if isinstance(ctx_ref, dict) and ctx_ref:
                account_ref = ctx_ref

    resolved_account: Any | None = None
    if account_resolver is not None:
        ref_arg: dict[str, Any] | None = (
            account_ref if isinstance(account_ref, dict) and account_ref else None
        )
        try:
            result = _invoke_account_resolver(account_resolver, ref_arg, auth_info=auth_info)
            if inspect.iscoroutine(result):
                resolved_account = await result
            else:
                resolved_account = result
        except Exception:
            # Resolver-raised → fail closed. A resolver that raises is
            # signaling "I cannot affirm this account is sandbox" — the
            # only safe response is DENY. Never fall through to the
            # buyer-supplied wire flag, which would let a misbehaving
            # FromAuthAccounts impl admit live principals.
            logger.warning(
                "comply_test_controller: account resolver raised; denying",
                exc_info=True,
            )
            return _controller_error(
                "PERMISSION_DENIED",
                "comply_test_controller account resolver could not affirm "
                "sandbox status; denying. See server logs for details.",
            )

    # 3. Compute admission signals (each independent so we can apply the
    # observed-modes fail-closed guard precisely).
    from adcp.decisioning.account_mode import is_sandbox_or_mock_account
    from adcp.decisioning.observed_modes import has_observed_live_mode

    account_is_sandbox = resolved_account is not None and is_sandbox_or_mock_account(
        resolved_account
    )

    # Wire ref `sandbox: true` only consulted when no account resolved.
    # If the resolver names the account, the resolver wins. The buyer's
    # wire claim NEVER overrides a resolved live account.
    ref_sandbox = (
        resolved_account is None
        and isinstance(account_ref, dict)
        and account_ref.get("sandbox") is True
    )

    # `context.sandbox: true` — secondary fallback for unresolved accounts
    # (capability probes / conformance bootstrap that don't carry an
    # account ref at all).
    wire_context = params.get("context")
    context_sandbox = (
        resolved_account is None
        and isinstance(wire_context, dict)
        and wire_context.get("sandbox") is True
    )

    env_sandbox = os.environ.get("ADCP_SANDBOX") == "1"

    would_admit_only_via_env = env_sandbox and not (
        account_is_sandbox or ref_sandbox or context_sandbox
    )

    # 4. Fail-closed guard on env fallback. ADCP_SANDBOX=1 + observed
    # explicit live mode = misconfig; refuse loudly so operators notice.
    if would_admit_only_via_env and has_observed_live_mode():
        raise RuntimeError(
            "comply_test_controller: ADCP_SANDBOX=1 is set but this process has "
            "resolved at least one live-mode account from "
            "platform.accounts.resolve. Remove ADCP_SANDBOX from your prod "
            "environment; gate the controller via mode='sandbox' on resolved "
            "sandbox accounts instead. See "
            "docs/proposals/lifecycle-state-and-sandbox-authority.md."
        )

    allowed = account_is_sandbox or ref_sandbox or context_sandbox or env_sandbox

    if resolved_account is not None and not account_is_sandbox:
        return _controller_error(
            "FORBIDDEN",
            "comply_test_controller requires a sandbox or mock account; "
            "resolved account is in live mode.",
        )

    if not allowed:
        return _controller_error(
            "PERMISSION_DENIED",
            "comply_test_controller requires a sandbox or mock account; "
            "no account resolved and no sandbox signal present.",
        )

    return None


def _invoke_account_resolver(
    resolver: _AccountResolver,
    ref: dict[str, Any] | None,
    *,
    auth_info: AuthInfo | None,
) -> Any:
    """Call a resolver, threading ``auth_info`` only when its signature
    accepts it.

    Resolvers conforming to the current Protocol accept the kwarg.
    Adopters with legacy single-arg resolvers keep working — the gate
    detects the absence of ``auth_info=`` in their signature and elides
    the kwarg. The legacy callsite then sees the same behavior it always
    saw; the auth_info threading is purely additive.
    """
    if _accepts_kwarg(resolver, "auth_info"):
        return resolver(ref, auth_info=auth_info)
    return resolver(ref)


def _canonical_validation_error(params: dict[str, Any]) -> dict[str, Any] | None:
    """Return a controller error when the canonical request schema rejects input."""
    from adcp.validation.schema_validator import format_issues, validate_request

    validation = validate_request("comply_test_controller", params)
    if validation.valid:
        return None
    return _controller_error("INVALID_PARAMS", format_issues(validation.issues))


async def _handle_test_controller(
    store: TestControllerStore,
    params: dict[str, Any],
    context: ToolContext | None = None,
    account_resolver: _AccountResolver | None = None,
    *,
    validate_schema: bool = False,
) -> dict[str, Any]:
    """Dispatch a comply_test_controller request to the store.

    When ``context`` is supplied and the store's scenario method accepts
    a ``context`` keyword, it's passed through — enabling header-driven
    mock behavior composed with storyboard-driven compliance testing.
    Methods without ``context`` in their signature keep working
    unchanged.

    When ``account_resolver`` is supplied, the comply controller runs
    the Phase 1 sandbox-authority gate before dispatching to the store
    (see :func:`_apply_sandbox_gate`). When ``None``, falls through to
    the legacy ``ADCP_SANDBOX=1`` env-fallback (with the fail-closed
    observation guard). Phase 1 of the lifecycle-state-and-sandbox-
    authority proposal — see
    ``docs/proposals/lifecycle-state-and-sandbox-authority.md``.

    Public MCP and A2A registrations set ``validate_schema=True`` so the
    bundled canonical conditional schema is enforced before dispatch. The
    default remains false for this private helper's legacy in-process test
    callers, which exercise individual dispatch and sandbox-gate branches
    with intentionally partial envelopes.
    """
    scenario = params.get("scenario")
    implemented = _list_scenarios(store)

    if scenario == "list_scenarios":
        if validate_schema:
            validation_error = _canonical_validation_error(params)
            if validation_error is not None:
                return validation_error
        # Capability probe — exempt from the sandbox gate. Returning the
        # implemented-scenarios list is a discovery surface every buyer
        # needs to call regardless of mode.
        return {
            "success": True,
            "scenarios": implemented,
        }

    # Phase 1 sandbox-authority gate — refuse for live-mode accounts.
    # Runs BEFORE scenario validation so a buyer probing with garbage
    # scenarios on a live account gets PERMISSION_DENIED, not
    # UNKNOWN_SCENARIO (which would leak which scenarios exist).
    auth_info = _extract_auth_info_from_context(context)
    gate_response = await _apply_sandbox_gate(
        params,
        account_resolver,
        auth_info=auth_info,
    )
    if gate_response is not None:
        return gate_response

    if validate_schema:
        validation_error = _canonical_validation_error(params)
        if validation_error is not None:
            return validation_error

    if scenario not in SCENARIOS:
        return _controller_error(
            "UNKNOWN_SCENARIO",
            f"Unknown scenario: {scenario}",
        )

    if scenario not in implemented:
        return _controller_error(
            "UNKNOWN_SCENARIO",
            f"Scenario {scenario} is not implemented by this agent",
        )

    method = getattr(store, scenario)
    scenario_params = params.get("params", {})
    if not isinstance(scenario_params, dict):
        return _controller_error("INVALID_PARAMS", "params must be an object")

    extra: dict[str, Any] = {}
    if context is not None and _accepts_context_kwarg(method):
        extra["context"] = context
    account = params.get("account")
    if account is not None and _accepts_kwarg(method, "account"):
        extra["account"] = account

    try:
        method_kwargs = _accepted_scenario_kwargs(method, scenario_params)
        for name in _LEGACY_OPTIONAL_SCENARIO_PARAMS.get(str(scenario), ()):
            if name not in method_kwargs and _accepts_kwarg(method, name):
                method_kwargs[name] = None
        if scenario == "expire_account_change_cursor":
            if not isinstance(account, dict) or not isinstance(account.get("account_id"), str):
                return _controller_error(
                    "INVALID_PARAMS",
                    "account.account_id is required for expire_account_change_cursor",
                )
            if _accepts_kwarg(method, "account_id"):
                method_kwargs["account_id"] = account["account_id"]
        elif scenario == "force_creative_status":
            _require_scenario_params(scenario_params, "creative_id", "status")
        elif scenario == "force_creative_purge":
            _require_scenario_params(scenario_params, "creative_id")
        elif scenario == "force_account_status":
            _require_scenario_params(scenario_params, "account_id", "status")
        elif scenario == "force_media_buy_status":
            _require_scenario_params(scenario_params, "media_buy_id", "status")
        elif scenario == "force_session_status":
            _require_scenario_params(scenario_params, "session_id", "status")
        elif scenario == "force_create_media_buy_arm":
            arm = scenario_params.get("arm") or ""
            if arm not in ("submitted", "input-required"):
                return _controller_error(
                    "INVALID_PARAMS",
                    "arm must be 'submitted' or 'input-required'",
                )
            raw_task_id = scenario_params.get("task_id")
            task_id: str | None = raw_task_id.strip() if isinstance(raw_task_id, str) else None
            if not task_id:
                task_id = None
            if arm == "submitted" and not task_id:
                return _controller_error(
                    "INVALID_PARAMS",
                    "task_id is required when arm is 'submitted'",
                )
            if task_id and len(task_id) > _MAX_TASK_ID:
                return _controller_error(
                    "INVALID_PARAMS",
                    f"task_id must be at most {_MAX_TASK_ID} characters",
                )
            # Forced.task_id is only valid for arm='submitted'; strip it for
            # 'input-required' so stores can't inadvertently echo it into the
            # Forced object (which has extra="forbid" in the response schema).
            if arm == "input-required":
                task_id = None
            message = scenario_params.get("message")
            if message is not None and (
                not isinstance(message, str) or len(message) > _MAX_MESSAGE
            ):
                return _controller_error(
                    "INVALID_PARAMS",
                    f"message must be a string of at most {_MAX_MESSAGE} characters",
                )
            for name, value in (("arm", arm), ("task_id", task_id), ("message", message)):
                if _accepts_kwarg(method, name):
                    method_kwargs[name] = value
        elif scenario == "force_get_products_arm":
            arm = scenario_params.get("arm")
            if arm not in ("submitted", "rejected"):
                return _controller_error(
                    "INVALID_PARAMS",
                    "arm must be 'submitted' or 'rejected'",
                )
            message = scenario_params.get("message")
            if message is not None and (
                not isinstance(message, str) or len(message) > _MAX_MESSAGE
            ):
                return _controller_error(
                    "INVALID_PARAMS",
                    f"message must be a string of at most {_MAX_MESSAGE} characters",
                )
            if arm == "submitted":
                raw_task_id = scenario_params.get("task_id")
                task_id = raw_task_id.strip() if isinstance(raw_task_id, str) else None
                if not task_id:
                    return _controller_error(
                        "INVALID_PARAMS",
                        "task_id is required when arm is 'submitted'",
                    )
                if len(task_id) > _MAX_TASK_ID:
                    return _controller_error(
                        "INVALID_PARAMS",
                        f"task_id must be at most {_MAX_TASK_ID} characters",
                    )
                if "reason" in scenario_params or "suggestions" in scenario_params:
                    return _controller_error(
                        "INVALID_PARAMS",
                        "reason and suggestions are not allowed when arm is 'submitted'",
                    )
                if _accepts_kwarg(method, "task_id"):
                    method_kwargs["task_id"] = task_id
            else:
                reason = scenario_params.get("reason")
                if not isinstance(reason, str) or not reason or len(reason) > _MAX_MESSAGE:
                    return _controller_error(
                        "INVALID_PARAMS",
                        f"reason must be a string of 1 to {_MAX_MESSAGE} characters",
                    )
                if "task_id" in scenario_params or "message" in scenario_params:
                    return _controller_error(
                        "INVALID_PARAMS",
                        "task_id and message are not allowed when arm is 'rejected'",
                    )
                suggestions = scenario_params.get("suggestions")
                if suggestions is not None and (
                    not isinstance(suggestions, list)
                    or not 1 <= len(suggestions) <= 20
                    or any(
                        not isinstance(item, str) or not item or len(item) > 1000
                        for item in suggestions
                    )
                ):
                    return _controller_error(
                        "INVALID_PARAMS",
                        "suggestions must contain 1 to 20 non-empty strings "
                        "of at most 1000 characters",
                    )
        elif scenario == "force_get_signals_arm":
            if scenario_params.get("arm") != "submitted":
                return _controller_error("INVALID_PARAMS", "arm must be 'submitted'")
            message = scenario_params.get("message")
            if message is not None and (
                not isinstance(message, str) or len(message) > _MAX_MESSAGE
            ):
                return _controller_error(
                    "INVALID_PARAMS",
                    f"message must be a string of at most {_MAX_MESSAGE} characters",
                )
            raw_task_id = scenario_params.get("task_id")
            task_id = raw_task_id.strip() if isinstance(raw_task_id, str) else None
            if not task_id:
                return _controller_error(
                    "INVALID_PARAMS",
                    "task_id is required when arm is 'submitted'",
                )
            if len(task_id) > _MAX_TASK_ID:
                return _controller_error(
                    "INVALID_PARAMS",
                    f"task_id must be at most {_MAX_TASK_ID} characters",
                )
            if _accepts_kwarg(method, "task_id"):
                method_kwargs["task_id"] = task_id
        elif scenario == "force_task_completion":
            raw_task_id = scenario_params.get("task_id")
            task_id = raw_task_id.strip() if isinstance(raw_task_id, str) else None
            if not task_id:
                return _controller_error(
                    "INVALID_PARAMS",
                    "Missing required parameter: 'task_id'",
                )
            if len(task_id) > _MAX_TASK_ID:
                return _controller_error(
                    "INVALID_PARAMS",
                    f"task_id must be at most {_MAX_TASK_ID} characters",
                )
            result_value = scenario_params.get("result")
            if not isinstance(result_value, dict) or not result_value:
                return _controller_error(
                    "INVALID_PARAMS",
                    "result must be a non-empty object",
                )
            result_bytes = len(json.dumps(result_value).encode("utf-8"))
            if result_bytes > _MAX_RESULT_BYTES:
                return _controller_error(
                    "INVALID_PARAMS",
                    f"result payload exceeds {_MAX_RESULT_BYTES // 1024} KB limit",
                )
            if _accepts_kwarg(method, "task_id"):
                method_kwargs["task_id"] = task_id
            if _accepts_kwarg(method, "result"):
                method_kwargs["result"] = result_value
        elif scenario == "simulate_delivery":
            _require_scenario_params(scenario_params, "media_buy_id")
        elif scenario == "simulate_budget_spend":
            _require_scenario_params(scenario_params, "spend_percentage")
        elif scenario == "seed_account":
            _require_scenario_params(scenario_params, "account_id")
        elif scenario == "seed_rights_grant":
            _require_scenario_params(scenario_params, "rights_id")
        elif scenario == "seed_measurement_catalog":
            _require_scenario_params(scenario_params, "vendor", "metrics")
        elif scenario == "query_provenance_audit_observations":
            _require_scenario_params(scenario_params, "creative_id")
        elif scenario == "force_upstream_unavailable":
            _require_scenario_params(scenario_params, "tool")
        elif scenario == "catalog_item_availability_probe":
            _require_scenario_params(scenario_params, "operation", "catalog_id", "item_id")
            operation = scenario_params["operation"]
            if operation not in {
                "seed_inaccessible_item",
                "query_eligibility",
                "advance_time",
                "recreate_catalog",
            }:
                return _controller_error(
                    "INVALID_PARAMS",
                    "Unsupported catalog_item_availability_probe operation",
                )
            if operation in {"query_eligibility", "advance_time", "recreate_catalog"}:
                _require_scenario_params(scenario_params, "catalog_generation")
            if operation == "advance_time":
                _require_scenario_params(scenario_params, "target_time")
        elif scenario == "compact_product_lifecycle_probe":
            _require_scenario_params(scenario_params, "operation")
            operation = scenario_params["operation"]
            if operation == "prepare":
                _require_scenario_params(scenario_params, "product_id")
                if "proposal_id" in scenario_params:
                    return _controller_error(
                        "INVALID_PARAMS",
                        "proposal_id is not allowed for the prepare operation",
                    )
            elif operation == "expire_proposal":
                _require_scenario_params(scenario_params, "proposal_id")
                forbidden = {"expires_at", "target_time", "product_id"} & scenario_params.keys()
                if forbidden:
                    return _controller_error(
                        "INVALID_PARAMS",
                        f"Fields not allowed for expire_proposal: {', '.join(sorted(forbidden))}",
                    )
            else:
                return _controller_error(
                    "INVALID_PARAMS",
                    "operation must be 'prepare' or 'expire_proposal'",
                )
        elif scenario == "compact_direct_buy_lifecycle_probe":
            _require_scenario_params(scenario_params, "operation", "product_id")
            if scenario_params["operation"] != "prepare":
                return _controller_error("INVALID_PARAMS", "operation must be 'prepare'")
            forbidden = {"proposal_id", "expires_at", "target_time"} & scenario_params.keys()
            if forbidden:
                return _controller_error(
                    "INVALID_PARAMS",
                    f"Fields not allowed for prepare: {', '.join(sorted(forbidden))}",
                )
        elif scenario not in {
            "seed_product",
            "seed_pricing_option",
            "seed_creative",
            "seed_plan",
            "seed_media_buy",
            "seed_creative_format",
            "query_upstream_traffic",
        }:
            return _controller_error("UNKNOWN_SCENARIO", f"Unknown scenario: {scenario}")

        result = await method(**method_kwargs, **extra)
    except TestControllerError as e:
        return _controller_error(e.code, str(e), current_state=e.current_state)
    except KeyError as e:
        return _controller_error("INVALID_PARAMS", f"Missing required parameter: {e}")
    except NotImplementedError:
        return _controller_error(
            "UNKNOWN_SCENARIO",
            f"Scenario {scenario} is not implemented by this agent",
        )
    except Exception as e:
        return _controller_error("INTERNAL_ERROR", str(e))

    # Wrap in success=True if the store didn't include it
    if isinstance(result, dict) and "success" not in result:
        result["success"] = True

    # Echo the wire ``context`` field per the spec's
    # comply-test-controller-response shape. Storyboards thread state
    # across steps via the context object; sellers that don't echo
    # break the storyboard runner's ``$context.<field>`` resolution
    # for downstream steps. Skip when the store already populated
    # ``context`` itself (an explicit override wins).
    wire_context = params.get("context")
    if isinstance(result, dict) and "context" not in result and isinstance(wire_context, dict):
        result["context"] = dict(wire_context)

    return dict(result)


def register_test_controller(
    mcp: Any,
    store: TestControllerStore,
    *,
    context_factory: ContextFactory | None = None,
    account_resolver: _AccountResolver | None = None,
) -> None:
    """Register the comply_test_controller tool on an MCP server.

    This is the Python equivalent of the JS SDK's registerTestController().
    It adds the comply_test_controller MCP tool backed by your TestControllerStore.

    Args:
        mcp: A FastMCP server instance.
        store: Your TestControllerStore implementation.
        context_factory: Optional ``ContextFactory`` invoked per call to
            build a :class:`ToolContext`. When set, the context is
            threaded into store methods that declare a ``context``
            keyword — which is how sellers whose test runtime reads
            request headers (``AdCPTestContext.from_headers``) combine
            header-driven mock state with the storyboard-driven
            ``comply_test_controller`` skill. Wire the same factory you
            pass to :func:`create_mcp_server` so both paths see the
            same per-request context.
        account_resolver: Async-or-sync callable that resolves a wire
            account ref to a framework :class:`Account`, OR the
            :data:`INSECURE_ALLOW_ALL` sentinel for tests that opt out
            of the gate. The comply controller applies the Phase 1
            sandbox-authority gate against the resolved account: only
            accounts with ``mode in {'sandbox', 'mock'}`` (or legacy
            ``sandbox=True``) are admitted; ``mode='live'`` is denied
            regardless of wire signals. v6 :class:`DecisioningPlatform`
            adopters get this hooked automatically by
            ``decisioning.serve``. Adopters wiring the controller
            manually pass a closure over their own account store.

            **Default fail-closed.** When ``None`` AND ``ADCP_SANDBOX``
            is unset, every comply call is denied — manually-wired
            ``ADCPHandler`` / :class:`ComplianceHandler` deployments are
            protected by default. Tests that intentionally bypass the
            gate pass ``account_resolver=INSECURE_ALLOW_ALL``; dev
            servers can set ``ADCP_SANDBOX=1`` instead.

            See ``docs/proposals/lifecycle-state-and-sandbox-authority.md``.

    Example:
        from adcp.server.test_controller import TestControllerStore, register_test_controller

        class MyStore(TestControllerStore):
            async def force_account_status(self, account_id, status):
                old = self.accounts[account_id]["status"]
                self.accounts[account_id]["status"] = status
                return {"previous_state": old, "current_state": status}

        mcp = create_mcp_server(MySeller(), name="my-agent")
        register_test_controller(mcp, MyStore())
        mcp.run(transport="streamable-http")
    """

    from mcp.server.mcpserver.tools import Tool
    from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
    from pydantic import ConfigDict

    from adcp.server.base import ToolContext as _ToolContext
    from adcp.server.serve import RequestMetadata as _RequestMetadata
    from adcp.validation.schema_loader import get_mcp_schema

    controller_schema = get_mcp_schema("comply_test_controller", "request")
    if controller_schema is None:
        raise RuntimeError("bundled comply_test_controller request schema is unavailable")

    async def comply_test_controller(**kwargs: Any) -> dict[str, Any]:
        context: _ToolContext | None = None
        if context_factory is not None:
            meta = _RequestMetadata(tool_name="comply_test_controller", transport="mcp")
            context = context_factory(meta)
            if not isinstance(context, _ToolContext):
                raise TypeError(
                    "context_factory for comply_test_controller returned "
                    f"{type(context).__name__}, not a ToolContext instance"
                )
        return await _handle_test_controller(
            store,
            kwargs,
            context=context,
            account_resolver=account_resolver,
            validate_schema=True,
        )

    tool = Tool.from_function(
        comply_test_controller,
        name="comply_test_controller",
        description="Compliance test controller. Sandbox only, not for production use.",
    )

    # Advertise the same canonical, self-contained schema enforced above.
    # Scenario-specific conditionals therefore evolve with the bundled AdCP
    # schema instead of a second hand-maintained dispatcher contract.
    tool.parameters = controller_schema

    # Override fn_metadata with a permissive model
    class _ControllerArgs(ArgModelBase):
        model_config = ConfigDict(extra="allow")

        def model_dump_one_level(self) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for field_name in self.__class__.model_fields:
                result[field_name] = getattr(self, field_name)
            if self.model_extra:
                result.update(self.model_extra)
            return result

    tool.fn_metadata = FuncMetadata(
        arg_model=_ControllerArgs,
        output_schema=tool.fn_metadata.output_schema,
        output_model=tool.fn_metadata.output_model,
        wrap_output=tool.fn_metadata.wrap_output,
    )

    mcp._tool_manager._tools["comply_test_controller"] = tool
