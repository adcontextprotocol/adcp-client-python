"""Unit tests for adcp.decisioning.serve.

Covers:

* :func:`create_adcp_server_from_platform` — builds the handler +
  validates the platform + wires executor + registry.
* :func:`serve` — one-call wrapper smoke (we don't actually start
  an MCP server in tests; the wrapper composition is verified by
  inspecting that ``create_adcp_server_from_platform`` would have
  been called with the right kwargs via mock).
* D5 executor configurability — BYO ``executor=`` AND ``thread_pool_size=``
  are mutually exclusive; default fires ``min(32, cpu+4)``.
* Emma #8 production-mode gate — ``ADCP_ENV in {prod, production}``
  with ``InMemoryTaskRegistry`` raises unless
  ``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1``.
"""

from __future__ import annotations

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from inspect import signature
from unittest.mock import MagicMock, patch

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.serve import (
    _default_thread_pool_size,
    _is_production_env,
    create_adcp_server_from_platform,
    serve,
)
from adcp.decisioning.serve import (
    serve as serve_platform,
)
from adcp.exceptions import ConfigurationError
from adcp.server.mcp_tools import _resolve_handler_adcp_version


class _BarePlatform(DecisioningPlatform):
    # Declares ``supported_protocols=["media_buy"]`` explicitly via the
    # override path — the test platform has no business-logic methods,
    # so we can't claim ``sales-non-guaranteed`` (would trip the
    # SalesPlatform method-coverage validator). The override is the
    # 5%-case escape hatch for platforms claiming a protocol without
    # an enumerated specialism. (Pre-#479 the handler silently defaulted
    # ``supported_protocols`` to ``["media_buy"]`` when no specialism
    # was declared; that masked storyboard-commitment lies. The new
    # projection emits empty list and the boot validator rejects it,
    # forcing adopters — including this fixture — to be explicit.)
    # ``supported_billing`` is required by the spec when ``media_buy``
    # is claimed.
    from adcp.decisioning.capabilities import SupportedProtocol  # noqa: PLC0415

    capabilities = DecisioningCapabilities(
        supported_protocols=[SupportedProtocol.media_buy],
        supported_billing=("agent",),
    )
    accounts = SingletonAccounts(account_id="hello")


class _SalesPlatformWithRequiredMethods(DecisioningPlatform):
    """Sales-non-guaranteed platform that exposes ``create_media_buy``
    et al. — used for F12 boot-time webhook gate tests. The five
    required SalesPlatform methods are stubbed so ``validate_platform``
    passes; the test focuses on the webhook gate."""

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        # supported_billing required by the boot-time capabilities-shape
        # validator (DX #422) whenever media_buy is claimed.
        supported_billing=("operator",),
    )
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "x", "status": "active"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}


# ---- _is_production_env ----


def test_is_production_env_default_false() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ADCP_ENV", None)
        assert _is_production_env() is False


@pytest.mark.parametrize("value", ["prod", "production", "PROD", "Production"])
def test_is_production_env_recognizes_prod_aliases(value: str) -> None:
    with patch.dict(os.environ, {"ADCP_ENV": value}):
        assert _is_production_env() is True


@pytest.mark.parametrize("value", ["dev", "staging", "test", "preprod", ""])
def test_is_production_env_rejects_non_prod_values(value: str) -> None:
    with patch.dict(os.environ, {"ADCP_ENV": value}):
        assert _is_production_env() is False


# ---- _default_thread_pool_size ----


def test_default_thread_pool_size_capped_at_32() -> None:
    """Stdlib's ThreadPoolExecutor uses min(32, cpu+4) — we mirror
    that. The 32 ceiling matters on large machines so the framework
    doesn't accidentally over-allocate threads."""
    size = _default_thread_pool_size()
    assert 1 <= size <= 32


# ---- create_adcp_server_from_platform — happy path ----


def test_create_returns_handler_executor_registry_tuple() -> None:
    """Returns the 3-tuple per the public contract."""
    platform = _BarePlatform()
    handler, executor, registry = create_adcp_server_from_platform(platform)
    assert handler is not None
    assert isinstance(executor, ThreadPoolExecutor)
    assert isinstance(registry, InMemoryTaskRegistry)
    executor.shutdown(wait=True)


def test_create_pins_handler_adcp_version() -> None:
    handler, executor, _ = create_adcp_server_from_platform(_BarePlatform(), adcp_version="3.1")
    try:
        assert handler.get_adcp_version() == "3.1"
        assert _resolve_handler_adcp_version(handler, None) == "3.1"
        assert _resolve_handler_adcp_version(handler, "3.2") == "3.2"
    finally:
        executor.shutdown(wait=True)


def test_create_pin_selects_versioned_mcp_discovery_schema() -> None:
    from adcp.server.mcp_tools import get_tools_for_handler

    handler, executor, _ = create_adcp_server_from_platform(
        _BarePlatform(), adcp_version="3.1", advertise_all=True
    )
    try:
        tools = {tool["name"]: tool for tool in get_tools_for_handler(handler, advertise_all=True)}
        properties = tools["list_creatives"]["inputSchema"]["properties"]
        assert "assignment_projection" not in properties
        assert "assignment_limit" not in properties
    finally:
        executor.shutdown(wait=True)


def test_create_defaults_to_packaged_adcp_version() -> None:
    from adcp._version import resolve_adcp_version

    handler, executor, _ = create_adcp_server_from_platform(_BarePlatform())
    try:
        assert handler.get_adcp_version() == resolve_adcp_version(None)
    finally:
        executor.shutdown(wait=True)


def test_create_rejects_invalid_adcp_version() -> None:
    with pytest.raises(ConfigurationError, match="adcp_version"):
        create_adcp_server_from_platform(_BarePlatform(), adcp_version="invalid")


def test_create_default_executor_uses_named_threads() -> None:
    """Framework-allocated default executor sets a thread_name_prefix
    for operator visibility (D5)."""
    platform = _BarePlatform()
    _, executor, _ = create_adcp_server_from_platform(platform)
    # We can't easily inspect ThreadPoolExecutor's prefix without
    # submitting a task — verify via thread name lookup.
    fut = executor.submit(lambda: __import__("threading").current_thread().name)
    name = fut.result(timeout=2.0)
    assert name.startswith("adcp-decisioning-"), f"Expected adcp-decisioning- prefix, got: {name}"
    executor.shutdown(wait=True)


# ---- D5 — executor / thread_pool_size mutually exclusive ----


def test_create_rejects_both_executor_and_thread_pool_size() -> None:
    platform = _BarePlatform()
    custom = ThreadPoolExecutor(max_workers=2)
    try:
        with pytest.raises(ValueError, match="not both"):
            create_adcp_server_from_platform(
                platform,
                executor=custom,
                thread_pool_size=8,
            )
    finally:
        custom.shutdown(wait=True)


def test_create_uses_byo_executor_unchanged() -> None:
    """Operator-supplied executor is wired through verbatim — same
    instance the caller passed in."""
    platform = _BarePlatform()
    custom = ThreadPoolExecutor(max_workers=2, thread_name_prefix="byo-")
    try:
        _, executor, _ = create_adcp_server_from_platform(
            platform,
            executor=custom,
            timed_sync_get_products_limit=1,
        )
        assert executor is custom
    finally:
        custom.shutdown(wait=True)


def test_create_requires_admission_limit_for_byo_executor() -> None:
    custom = ThreadPoolExecutor(max_workers=64)
    try:
        with pytest.raises(ValueError, match="timed_sync_get_products_limit"):
            create_adcp_server_from_platform(_BarePlatform(), executor=custom)
    finally:
        custom.shutdown(wait=True)


def test_create_projects_resolved_admission_limit_to_handler() -> None:
    handler, executor, _ = create_adcp_server_from_platform(
        _BarePlatform(),
        thread_pool_size=4,
    )
    try:
        assert handler._timed_sync_get_products_admission.limit == 2  # noqa: SLF001
    finally:
        executor.shutdown(wait=True)


def test_create_projects_explicit_admission_limit_to_handler() -> None:
    handler, executor, _ = create_adcp_server_from_platform(
        _BarePlatform(),
        timed_sync_get_products_limit=1,
    )
    try:
        assert handler._timed_sync_get_products_admission.limit == 1  # noqa: SLF001
    finally:
        executor.shutdown(wait=True)


def test_serve_forwards_timed_sync_admission_limit() -> None:
    handler = MagicMock()
    executor = MagicMock()
    registry = MagicMock()
    media_buy_store = MagicMock()
    decisioning_serve_module = importlib.import_module("adcp.decisioning.serve")
    server_serve_module = importlib.import_module("adcp.server.serve")
    with (
        patch.object(
            decisioning_serve_module,
            "create_adcp_server_from_platform",
            return_value=(handler, executor, registry),
        ) as create,
        patch.object(server_serve_module, "serve") as server_serve,
    ):
        serve_platform(
            _BarePlatform(),
            timed_sync_get_products_limit=3,
            media_buy_store=media_buy_store,
            adcp_version="3.1",
            validate_at_init=False,
        )

    assert create.call_args.kwargs["timed_sync_get_products_limit"] == 3
    assert create.call_args.kwargs["media_buy_store"] is media_buy_store
    assert create.call_args.kwargs["adcp_version"] == "3.1"
    server_serve.assert_called_once()


def test_create_thread_pool_size_overrides_default() -> None:
    """``thread_pool_size=`` sizes the framework-allocated default
    executor."""
    platform = _BarePlatform()
    _, executor, _ = create_adcp_server_from_platform(platform, thread_pool_size=2)
    assert executor._max_workers == 2  # type: ignore[attr-defined]
    executor.shutdown(wait=True)


# ---- Emma #8 production-mode gate ----


def test_create_raises_in_production_with_default_in_memory_registry() -> None:
    """ADCP_ENV=production + default InMemoryTaskRegistry + no opt-in
    → AdcpError. Sales-broadcast-tv adopters depend on the registry;
    silent in-memory fallback would lose tasks across restarts."""
    platform = _BarePlatform()
    with patch.dict(
        os.environ,
        {"ADCP_ENV": "production"},
        clear=False,
    ):
        os.environ.pop("ADCP_DECISIONING_ALLOW_INMEMORY_TASKS", None)
        with pytest.raises(AdcpError) as exc_info:
            create_adcp_server_from_platform(platform)
    assert exc_info.value.code == "INVALID_REQUEST"
    msg = str(exc_info.value)
    assert "InMemoryTaskRegistry" in msg
    assert "ADCP_DECISIONING_ALLOW_INMEMORY_TASKS" in msg


def test_create_passes_in_production_with_explicit_opt_in() -> None:
    """The opt-in env var lets adopters explicitly accept in-memory
    tasks in prod (e.g., for single-process pilots). Setting it to
    '1' bypasses the gate."""
    platform = _BarePlatform()
    with patch.dict(
        os.environ,
        {
            "ADCP_ENV": "production",
            "ADCP_DECISIONING_ALLOW_INMEMORY_TASKS": "1",
        },
    ):
        handler, executor, registry = create_adcp_server_from_platform(platform)
    assert isinstance(registry, InMemoryTaskRegistry)
    executor.shutdown(wait=True)


def test_create_passes_in_production_with_custom_durable_registry() -> None:
    """When the operator supplies a registry with ``is_durable=True``,
    the gate doesn't fire — a v6.1-style PostgresTaskRegistry would
    be accepted in prod without the opt-in. The marker is what the
    gate reads (NOT isinstance checks; subclasses of
    InMemoryTaskRegistry inherit is_durable=False)."""

    class _DurableStub:
        is_durable = True  # the marker the gate reads

        async def issue(self, *, account_id, task_type):
            return "task_x"

        async def update_progress(self, task_id, progress):
            pass

        async def complete(self, task_id, result):
            pass

        async def fail(self, task_id, error):
            pass

        async def get(self, task_id, *, expected_account_id=None):
            return None

    platform = _BarePlatform()
    custom_reg = _DurableStub()
    with patch.dict(os.environ, {"ADCP_ENV": "production"}):
        os.environ.pop("ADCP_DECISIONING_ALLOW_INMEMORY_TASKS", None)
        handler, executor, registry = create_adcp_server_from_platform(
            platform, registry=custom_reg  # type: ignore[arg-type]
        )
    assert registry is custom_reg
    executor.shutdown(wait=True)


def test_create_raises_when_inmemory_subclass_used_in_production() -> None:
    """Adopter subclassing InMemoryTaskRegistry for instrumentation
    inherits is_durable=False — gate fires, no bypass via subclass.
    This is the regression for the round-4 review's `isinstance`
    bypass concern."""

    class _InstrumentedInMemoryRegistry(InMemoryTaskRegistry):
        pass

    platform = _BarePlatform()
    with patch.dict(os.environ, {"ADCP_ENV": "production"}):
        os.environ.pop("ADCP_DECISIONING_ALLOW_INMEMORY_TASKS", None)
        with pytest.raises(AdcpError) as exc_info:
            create_adcp_server_from_platform(platform, registry=_InstrumentedInMemoryRegistry())
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "_InstrumentedInMemoryRegistry" in str(exc_info.value)


def test_create_raises_when_registry_missing_is_durable_marker() -> None:
    """Round-5 Emma P1: a custom registry without the ``is_durable``
    marker fails fast at server boot — the framework refuses to guess
    whether the registry is durable. The diagnostic distinguishes
    "marker absent" (programmer error) from "marker=False in prod"
    (deployment misconfig). Without this guard, the prod gate's
    ``getattr(..., False)`` would treat the missing marker as
    non-durable and emit a misleading "non-durable refused" error."""

    class _BareRegistry:
        # NO is_durable declared — programmer error.

        async def issue(self, *, account_id, task_type):
            return "task_x"

        async def update_progress(self, task_id, progress):
            pass

        async def complete(self, task_id, result):
            pass

        async def fail(self, task_id, error):
            pass

        async def get(self, task_id, *, expected_account_id=None):
            return None

    platform = _BarePlatform()
    # Fires regardless of env — the marker is the programmer-facing
    # contract, not the deployment gate.
    with patch.dict(os.environ, {"ADCP_ENV": "dev"}):
        os.environ.pop("ADCP_DECISIONING_ALLOW_INMEMORY_TASKS", None)
        with pytest.raises(AdcpError) as exc_info:
            create_adcp_server_from_platform(
                platform, registry=_BareRegistry()  # type: ignore[arg-type]
            )
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "is_durable" in str(exc_info.value)
    assert "missing" in str(exc_info.value).lower()


def test_create_raises_when_duck_typed_non_durable_used_in_production() -> None:
    """Custom registry that explicitly declares is_durable=False trips
    the prod gate. Distinct from the missing-marker case above — this
    one is a deployment misconfig, not a programmer error."""

    class _ExplicitlyNonDurableRegistry:
        is_durable = False  # explicit opt-out, just no opt-in env var

        async def issue(self, *, account_id, task_type):
            return "task_x"

        async def update_progress(self, task_id, progress):
            pass

        async def complete(self, task_id, result):
            pass

        async def fail(self, task_id, error):
            pass

        async def get(self, task_id, *, expected_account_id=None):
            return None

    platform = _BarePlatform()
    with patch.dict(os.environ, {"ADCP_ENV": "production"}):
        os.environ.pop("ADCP_DECISIONING_ALLOW_INMEMORY_TASKS", None)
        with pytest.raises(AdcpError) as exc_info:
            create_adcp_server_from_platform(
                platform,
                registry=_ExplicitlyNonDurableRegistry(),  # type: ignore[arg-type]
            )
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "Non-durable" in str(exc_info.value)


def test_create_passes_in_dev_env_with_default_registry() -> None:
    """No prod gate — defaults work in local dev / CI."""
    platform = _BarePlatform()
    with patch.dict(os.environ, {"ADCP_ENV": "dev"}):
        handler, executor, registry = create_adcp_server_from_platform(platform)
    assert isinstance(registry, InMemoryTaskRegistry)
    executor.shutdown(wait=True)


# ---- Validation pass-through ----


def test_create_propagates_validate_platform_failure() -> None:
    """validate_platform's failure (missing required methods, etc.)
    propagates from create_adcp_server_from_platform — the caller
    sees the structured AdcpError before any wiring is exposed."""

    class _PartialSalesPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="x")
        # Missing all 5 required sales-* methods.

    with pytest.raises(AdcpError) as exc_info:
        create_adcp_server_from_platform(_PartialSalesPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "missing" in str(exc_info.value).lower()


def test_create_propagates_governance_opt_in_failure() -> None:
    """D15 governance fail-fast surfaces from
    create_adcp_server_from_platform."""

    class _UnsafeGovernancePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-spend-authority"],
            governance_aware=False,
        )
        accounts = SingletonAccounts(account_id="x")

    with pytest.raises(AdcpError) as exc_info:
        create_adcp_server_from_platform(_UnsafeGovernancePlatform())
    assert "governance" in str(exc_info.value).lower()


# ---- Custom state_reader / resource_resolver plumbing (D15) ----


def test_create_threads_state_reader_to_handler() -> None:
    """Custom StateReader impl flows through to the handler so when
    the handler hydrates RequestContext per request, adopter platform
    methods see ``ctx.state.<custom-impl-method>`` instead of the
    v6.0 stub."""

    class _CustomStateReader:
        def find_by_object(self, t, i):
            return ()

        def find_proposal_by_id(self, p):
            return None

        def governance_context(self):
            return None

        def workflow_steps(self):
            return ()

    custom = _CustomStateReader()
    platform = _BarePlatform()
    handler, executor, _ = create_adcp_server_from_platform(platform, state_reader=custom)
    assert handler._state_reader is custom
    executor.shutdown(wait=True)


def test_create_threads_resource_resolver_to_handler() -> None:
    class _CustomResolver:
        async def property_list(self, list_id):
            return None

        async def collection_list(self, list_id):
            return None

        async def creative_format(self, format_id, *, revalidate=False):
            return None

    custom = _CustomResolver()
    platform = _BarePlatform()
    handler, executor, _ = create_adcp_server_from_platform(platform, resource_resolver=custom)
    assert handler._resource_resolver is custom
    executor.shutdown(wait=True)


# ---- Legacy sync-completion compatibility boot gate ----


def test_serve_fails_fast_when_sales_platform_missing_webhook_sender() -> None:
    """Sales-non-guaranteed exposes create_media_buy + sync_creatives,
    both in SPEC_WEBHOOK_TASK_TYPES. With no webhook_sender wired and
    legacy sync auto-emit explicitly enabled, the framework MUST fail at
    boot rather than accept a compatibility mode it cannot deliver.

    The gate raises ``AdcpError("INVALID_REQUEST")`` for parity with
    ``validate_platform``'s sibling boot-time gates (governance opt-in,
    missing required methods) so adopter ``except AdcpError`` clauses
    catch all platform-config failures uniformly (per
    adtech-product-expert review on PR #339)."""
    platform = _SalesPlatformWithRequiredMethods()
    with pytest.raises(AdcpError) as exc_info:
        create_adcp_server_from_platform(platform, auto_emit_completion_webhooks=True)
    assert exc_info.value.code == "INVALID_REQUEST"
    msg = str(exc_info.value)
    assert "webhook_sender" in msg
    assert "silently dropped" in msg
    assert "create_media_buy" in msg
    # Structured details so adopter harnesses can programmatically
    # surface the exact missing piece + eligible tool list.
    assert exc_info.value.details["missing"] == "webhook_sender_or_supervisor"
    assert "create_media_buy" in exc_info.value.details["webhook_eligible_tools"]


def test_serve_passes_with_webhook_sender_wired() -> None:
    """Same platform, but webhook_sender provided → no fail-fast."""
    from unittest.mock import MagicMock

    platform = _SalesPlatformWithRequiredMethods()
    sender = MagicMock()
    handler, executor, _ = create_adcp_server_from_platform(
        platform,
        webhook_sender=sender,
        auto_emit_completion_webhooks=True,
    )
    assert handler._webhook_sender is sender
    executor.shutdown(wait=True)


def test_create_adcp_server_defaults_sync_completion_auto_emit_off() -> None:
    """The public builder defaults to conformant sync webhook behavior."""
    platform = _SalesPlatformWithRequiredMethods()
    handler, executor, _ = create_adcp_server_from_platform(platform)
    assert handler._auto_emit_completion_webhooks is False
    assert handler._auto_emit_task_webhooks is True
    executor.shutdown(wait=True)


def test_public_server_entrypoints_default_sync_completion_auto_emit_off() -> None:
    """Both production entrypoints expose the same conformant default."""
    assert (
        signature(create_adcp_server_from_platform)
        .parameters["auto_emit_completion_webhooks"]
        .default
        is False
    )
    assert signature(serve).parameters["auto_emit_completion_webhooks"].default is False
    assert signature(serve).parameters["auto_emit_task_webhooks"].default is True


def test_serve_does_not_fire_gate_for_platform_without_webhook_eligible_tools() -> None:
    """Bare platform claiming no specialism → no per-instance webhook
    surface → gate doesn't fire. Test fixtures and discovery-only
    agents stay valid."""
    platform = _BarePlatform()
    handler, executor, _ = create_adcp_server_from_platform(platform)
    assert handler._webhook_sender is None
    executor.shutdown(wait=True)


# ---- advertise_all kwarg + get_advertised_tools method ----


def test_get_advertised_tools_filters_to_claimed_specialisms() -> None:
    """``handler.get_advertised_tools()`` returns the effective set
    ``serve()`` would advertise — per-instance specialism filter +
    protocol/discovery always-ons. Materially smaller than the
    handler's class-level tool universe."""
    platform = _SalesPlatformWithRequiredMethods()
    handler, executor, _ = create_adcp_server_from_platform(
        platform, auto_emit_completion_webhooks=False
    )
    advertised = handler.get_advertised_tools()
    # The five overridden sales methods appear.
    for tool in (
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
    ):
        assert tool in advertised
    # ``get_adcp_capabilities`` is always-on (protocol discovery).
    assert "get_adcp_capabilities" in advertised
    # Tools from specialisms the platform didn't claim are filtered out.
    assert "build_creative" not in advertised  # creative-builder
    assert "acquire_rights" not in advertised  # brand-rights
    # Effective set is materially smaller than the class-level universe.
    assert len(advertised) < len(type(handler).advertised_tools)
    executor.shutdown(wait=True)


def test_get_advertised_tools_per_call_override_wins_over_configured_default() -> None:
    """The ``advertise_all`` kwarg on the method overrides the value
    configured at factory time. Lets adopters inspect both modes from
    a single handler."""
    platform = _SalesPlatformWithRequiredMethods()
    handler, executor, _ = create_adcp_server_from_platform(
        platform, advertise_all=False, auto_emit_completion_webhooks=False
    )
    forced_universe = handler.get_advertised_tools(advertise_all=True)
    forced_filtered = handler.get_advertised_tools(advertise_all=False)
    assert forced_universe >= forced_filtered
    executor.shutdown(wait=True)


def test_get_advertised_tools_returns_frozenset() -> None:
    """API guarantee: ``get_advertised_tools()`` returns a frozenset so
    callers can intersect/union with other sets without worrying about
    mutation."""
    platform = _SalesPlatformWithRequiredMethods()
    handler, executor, _ = create_adcp_server_from_platform(
        platform, auto_emit_completion_webhooks=False
    )
    advertised = handler.get_advertised_tools()
    assert isinstance(advertised, frozenset)
    executor.shutdown(wait=True)


def test_create_adcp_server_from_platform_stores_advertise_all_on_handler() -> None:
    """``advertise_all=True`` on the factory threads to the handler's
    configured default for :meth:`get_advertised_tools`."""
    platform = _SalesPlatformWithRequiredMethods()
    handler, executor, _ = create_adcp_server_from_platform(
        platform, advertise_all=True, auto_emit_completion_webhooks=False
    )
    assert handler._advertise_all is True
    executor.shutdown(wait=True)


def test_create_adcp_server_from_platform_advertise_all_default_false() -> None:
    """``advertise_all`` defaults to False, matching :func:`serve`."""
    platform = _SalesPlatformWithRequiredMethods()
    handler, executor, _ = create_adcp_server_from_platform(
        platform, auto_emit_completion_webhooks=False
    )
    assert handler._advertise_all is False
    executor.shutdown(wait=True)
