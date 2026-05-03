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
import os
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from adcp.server.base import ToolContext
    from adcp.server.serve import ContextFactory


class _AccountResolver(Protocol):
    """Async-or-sync callable that resolves a wire ``account`` ref to a
    framework :class:`Account`-shaped object.

    Adopters who use the v6 :class:`DecisioningPlatform` get this hooked
    automatically by ``register_test_controller`` — the framework wraps
    ``platform.accounts.resolve`` so the comply controller can apply the
    sandbox-authority gate against the resolved account.

    Returns the resolved account on success. Raises (any exception) on
    miss / unauthorized / other resolution failure — the caller treats
    raises as "no account resolved" and falls through to the wire-ref /
    env fallback gates. This is deliberately permissive: the gate's
    fail-closed posture ensures unresolved accounts get denied unless
    the request carries ``account.sandbox: true`` or the env fallback
    is in effect.
    """

    def __call__(self, ref: dict[str, Any] | None) -> Any: ...


# Scenario names — must match the AdCP comply_test_controller schema
SCENARIOS = [
    "force_creative_status",
    "force_account_status",
    "force_media_buy_status",
    "force_create_media_buy_arm",
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
    "seed_creative_format",
]

_MAX_TASK_ID = 128
_MAX_MESSAGE = 2000
_MAX_RESULT_BYTES = 256 * 1024  # 256 KB soft cap per AdCP 3.0.1


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

    async def force_creative_status(
        self,
        creative_id: str,
        status: str,
        rejection_reason: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Force a creative to a given status.

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
        conversions: int | None = None,
        reported_spend: dict[str, Any] | None = None,
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


async def _apply_sandbox_gate(
    params: dict[str, Any],
    account_resolver: _AccountResolver | None,
) -> dict[str, Any] | None:
    """Phase 1 sandbox-authority gate for ``comply_test_controller``.

    Order of admission (mirrors JS PR #1453):

    1. Resolve the account via ``account_resolver`` when wired. The
       framework reads the wire ref from top-level ``account`` (extended
       shape) or ``context.account`` (canonical AdCP). On resolution
       success, ``mode in {sandbox, mock}`` (or legacy ``sandbox=True``)
       admits; ``mode='live'`` denies regardless of any wire signal.

    2. When the resolver returned no account (no resolver wired, or
       resolver raised), consult the request's ``account.sandbox`` /
       ``context.sandbox`` wire flag — capability-probe / pre-bootstrap
       fallback. The buyer's wire claim NEVER overrides a resolved live
       account.

    3. Env fallback: ``ADCP_SANDBOX=1`` admits (deprecated, kept for
       back-compat with adopters who haven't migrated to ``mode``).

    4. Otherwise refuse with a controller-shaped FORBIDDEN envelope.

    **Fail-closed env-fallback guard.** When the env fallback is the
    only signal that would admit AND this process has resolved any
    explicit ``mode='live'`` account, the function raises a runtime
    error loudly. That pairing is a deployment misconfiguration —
    silent admission would unlock the comply controller for live
    principals. See :mod:`adcp.decisioning.observed_modes`.

    Returns ``None`` when admitted (caller proceeds to dispatch); a
    controller-shaped error dict when refused.
    """
    # Pre-Phase-1 back-compat: when no resolver is wired AND
    # ADCP_SANDBOX is not set, the gate is dormant — adopters who
    # haven't opted into the new wiring keep the previous behavior
    # (any caller can hit the controller, same as before this PR).
    # Adopters opt in by either passing ``account_resolver=`` to
    # ``register_test_controller`` (decisioning-platform serve does
    # this automatically) OR by setting ``ADCP_SANDBOX=1``. JS PR
    # #1453 takes the equivalent posture: the gate lives at the
    # framework boundary, not inside store-direct dispatch.
    env_sandbox_raw = os.environ.get("ADCP_SANDBOX") == "1"
    if account_resolver is None and not env_sandbox_raw:
        return None

    # 1. Resolve account
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
        try:
            ref_arg: dict[str, Any] | None = (
                account_ref if isinstance(account_ref, dict) and account_ref else None
            )
            result = account_resolver(ref_arg)
            if inspect.iscoroutine(result):
                resolved_account = await result
            else:
                resolved_account = result
        except Exception:
            # Resolver miss / unauthorized / any failure: treat as
            # "no account resolved" and fall through to the wire-ref
            # and env fallbacks. The fail-closed posture below ensures
            # we don't accidentally admit when the caller intended to
            # gate on resolution.
            resolved_account = None

    # 2. Compute admission signals (each independent so we can apply the
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

    # 3. Fail-closed guard on env fallback. ADCP_SANDBOX=1 + observed
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

    if not allowed:
        return _controller_error(
            "PERMISSION_DENIED",
            "comply_test_controller requires a sandbox or mock account; "
            "resolved account is in live mode (or no account resolved).",
        )

    return None


async def _handle_test_controller(
    store: TestControllerStore,
    params: dict[str, Any],
    context: ToolContext | None = None,
    account_resolver: _AccountResolver | None = None,
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
    """
    scenario = params.get("scenario")
    implemented = _list_scenarios(store)

    if scenario == "list_scenarios":
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
    gate_response = await _apply_sandbox_gate(params, account_resolver)
    if gate_response is not None:
        return gate_response

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

    extra: dict[str, Any] = {}
    if context is not None and _accepts_context_kwarg(method):
        extra["context"] = context
    account = params.get("account")
    if account is not None and _accepts_kwarg(method, "account"):
        extra["account"] = account

    try:
        if scenario == "force_creative_status":
            result = await method(
                creative_id=scenario_params["creative_id"],
                status=scenario_params["status"],
                rejection_reason=scenario_params.get("rejection_reason"),
                **extra,
            )
        elif scenario == "force_account_status":
            result = await method(
                account_id=scenario_params["account_id"],
                status=scenario_params["status"],
                **extra,
            )
        elif scenario == "force_media_buy_status":
            result = await method(
                media_buy_id=scenario_params["media_buy_id"],
                status=scenario_params["status"],
                rejection_reason=scenario_params.get("rejection_reason"),
                **extra,
            )
        elif scenario == "force_session_status":
            result = await method(
                session_id=scenario_params["session_id"],
                status=scenario_params["status"],
                termination_reason=scenario_params.get("termination_reason"),
                **extra,
            )
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
            result = await method(
                arm=arm,
                task_id=task_id,
                message=message,
                **extra,
            )
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
            result = await method(
                task_id=task_id,
                result=result_value,
                **extra,
            )
        elif scenario == "simulate_delivery":
            result = await method(
                media_buy_id=scenario_params["media_buy_id"],
                impressions=scenario_params.get("impressions"),
                clicks=scenario_params.get("clicks"),
                conversions=scenario_params.get("conversions"),
                reported_spend=scenario_params.get("reported_spend"),
                **extra,
            )
        elif scenario == "simulate_budget_spend":
            result = await method(
                spend_percentage=scenario_params["spend_percentage"],
                account_id=scenario_params.get("account_id"),
                media_buy_id=scenario_params.get("media_buy_id"),
                **extra,
            )
        elif scenario == "seed_product":
            result = await method(
                fixture=scenario_params.get("fixture"),
                product_id=scenario_params.get("product_id"),
                **extra,
            )
        elif scenario == "seed_pricing_option":
            result = await method(
                fixture=scenario_params.get("fixture"),
                product_id=scenario_params.get("product_id"),
                pricing_option_id=scenario_params.get("pricing_option_id"),
                **extra,
            )
        elif scenario == "seed_creative":
            result = await method(
                fixture=scenario_params.get("fixture"),
                creative_id=scenario_params.get("creative_id"),
                **extra,
            )
        elif scenario == "seed_plan":
            result = await method(
                fixture=scenario_params.get("fixture"),
                plan_id=scenario_params.get("plan_id"),
                **extra,
            )
        elif scenario == "seed_media_buy":
            result = await method(
                fixture=scenario_params.get("fixture"),
                media_buy_id=scenario_params.get("media_buy_id"),
                **extra,
            )
        elif scenario == "seed_creative_format":
            result = await method(
                fixture=scenario_params.get("fixture"),
                format_id=scenario_params.get("format_id"),
                **extra,
            )
        else:
            return _controller_error("UNKNOWN_SCENARIO", f"Unknown scenario: {scenario}")
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
        account_resolver: Optional async-or-sync callable that resolves
            a wire account ref to a framework :class:`Account`. When
            supplied, the comply controller applies the Phase 1
            sandbox-authority gate against the resolved account: only
            accounts with ``mode in {'sandbox', 'mock'}`` (or legacy
            ``sandbox=True``) are admitted; ``mode='live'`` is denied
            regardless of wire signals. v6 :class:`DecisioningPlatform`
            adopters get this hooked automatically by
            ``decisioning.serve``. Adopters wiring the controller
            manually pass a closure over their own account store. See
            ``docs/proposals/lifecycle-state-and-sandbox-authority.md``.

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

    from mcp.server.fastmcp.tools import Tool
    from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
    from pydantic import ConfigDict

    from adcp.server.base import ToolContext as _ToolContext
    from adcp.server.serve import RequestMetadata as _RequestMetadata

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
        )

    tool = Tool.from_function(
        comply_test_controller,
        name="comply_test_controller",
        description="Compliance test controller. Sandbox only, not for production use.",
    )

    # Override schema with the proper comply_test_controller inputSchema.
    # Derived from SCENARIOS so it can't drift from the dispatcher.
    tool.parameters = {
        "type": "object",
        "properties": {
            "account": {"type": "object"},
            "scenario": {
                "type": "string",
                # Derived from SCENARIOS so the enum never drifts from the dispatcher.
                "enum": ["list_scenarios"] + SCENARIOS,
            },
            "params": {"type": "object"},
            "context": {"type": "object"},
        },
        "required": ["scenario"],
    }

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
