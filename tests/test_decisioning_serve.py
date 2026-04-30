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

import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

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
)


class _BarePlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities()
    accounts = SingletonAccounts(account_id="hello")


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
        _, executor, _ = create_adcp_server_from_platform(platform, executor=custom)
        assert executor is custom
    finally:
        custom.shutdown(wait=True)


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


def test_create_raises_when_duck_typed_non_durable_used_in_production() -> None:
    """Custom registry with no is_durable marker (defaults False via
    getattr) trips the gate. Adopters MUST explicitly opt into
    is_durable=True; safe-by-default."""

    class _BareRegistry:
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
                platform, registry=_BareRegistry()  # type: ignore[arg-type]
            )
    assert exc_info.value.code == "INVALID_REQUEST"


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
