"""Tests for :class:`adcp.server.TenantRegistry`.

Covers:
* Basic register / unregister / resolve_by_host lifecycle
* Health state transitions (pending → healthy, disabled)
* recheck state machine (success and failure arms)
* Per-tenant asyncio.Lock — concurrent rechecks don't corrupt state
* Host normalization (port stripping, case folding)
* Validator (sync and async) is invoked correctly
* resolve_by_host returns None for unknown / unregistered hosts
* registered_tenants snapshot is correct after mutations
* register_lazy + resolve: lazy platform construction on first resolve()
* resolve() fast path for eagerly-registered tenants
* Lazy concurrent first-hit — only one factory invocation
* Factory failure → health=disabled, resolve returns None
* Lazy unregister-during-resolve — no zombie state
* Re-registering eager→lazy and lazy→eager
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from adcp.server import (
    TenantRegistry,
    TenantResolution,
)

# ---------------------------------------------------------------------------
# Minimal mock DecisioningPlatform — only needs to be an object.
# ---------------------------------------------------------------------------


def _mock_platform(name: str = "platform") -> Any:
    p = MagicMock()
    p.__repr__ = lambda self: f"<MockPlatform {name}>"
    return p


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_sets_pending_health() -> None:
    registry = TenantRegistry()
    await registry.register("acme", agent_url="https://acme.example.com", platform=_mock_platform())
    assert registry.health("acme") == "pending"


@pytest.mark.asyncio
async def test_register_with_await_validation_no_validator_goes_healthy() -> None:
    registry = TenantRegistry(validator=None)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "healthy"


@pytest.mark.asyncio
async def test_register_with_sync_validator_healthy() -> None:
    registry = TenantRegistry(validator=lambda tid, url: True)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "healthy"


@pytest.mark.asyncio
async def test_register_with_sync_validator_disabled() -> None:
    registry = TenantRegistry(validator=lambda tid, url: False)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "disabled"


@pytest.mark.asyncio
async def test_register_with_async_validator_healthy() -> None:
    async def async_validator(tid: str, url: str) -> bool:
        return True

    registry = TenantRegistry(validator=async_validator)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "healthy"


@pytest.mark.asyncio
async def test_register_validator_raises_sets_disabled() -> None:
    def bad_validator(tid: str, url: str) -> bool:
        raise RuntimeError("connection refused")

    registry = TenantRegistry(validator=bad_validator)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "disabled"


@pytest.mark.asyncio
async def test_unregister_removes_tenant() -> None:
    registry = TenantRegistry()
    await registry.register("acme", agent_url="https://acme.example.com", platform=_mock_platform())
    registry.unregister("acme")
    assert registry.health("acme") is None
    assert registry.resolve_by_host("acme.example.com") is None


@pytest.mark.asyncio
async def test_unregister_noop_for_unknown_tenant() -> None:
    registry = TenantRegistry()
    registry.unregister("nonexistent")  # must not raise


@pytest.mark.asyncio
async def test_health_returns_none_for_unknown_tenant() -> None:
    registry = TenantRegistry()
    assert registry.health("ghost") is None


# ---------------------------------------------------------------------------
# resolve_by_host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_by_host_returns_none_for_unknown() -> None:
    registry = TenantRegistry()
    assert registry.resolve_by_host("unknown.example.com") is None


@pytest.mark.asyncio
async def test_resolve_by_host_returns_tenant_resolution() -> None:
    platform = _mock_platform("acme")
    registry = TenantRegistry()
    await registry.register("acme", agent_url="https://acme.example.com", platform=platform)

    result = registry.resolve_by_host("acme.example.com")
    assert result is not None
    assert isinstance(result, TenantResolution)
    assert result.tenant_id == "acme"
    assert result.health == "pending"
    assert result.platform is platform


@pytest.mark.asyncio
async def test_resolve_by_host_strips_port() -> None:
    platform = _mock_platform()
    registry = TenantRegistry()
    await registry.register("acme", agent_url="https://acme.example.com", platform=platform)

    result = registry.resolve_by_host("acme.example.com:443")
    assert result is not None
    assert result.tenant_id == "acme"


@pytest.mark.asyncio
async def test_resolve_by_host_case_insensitive() -> None:
    platform = _mock_platform()
    registry = TenantRegistry()
    await registry.register("acme", agent_url="https://acme.example.com", platform=platform)

    result = registry.resolve_by_host("ACME.EXAMPLE.COM")
    assert result is not None
    assert result.tenant_id == "acme"


@pytest.mark.asyncio
async def test_resolve_by_host_after_url_change() -> None:
    platform = _mock_platform()
    registry = TenantRegistry()
    await registry.register("acme", agent_url="https://old.example.com", platform=platform)
    await registry.register("acme", agent_url="https://new.example.com", platform=platform)

    assert registry.resolve_by_host("old.example.com") is None
    result = registry.resolve_by_host("new.example.com")
    assert result is not None and result.tenant_id == "acme"


# ---------------------------------------------------------------------------
# recheck state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recheck_pending_to_healthy() -> None:
    registry = TenantRegistry(validator=lambda tid, url: True)
    await registry.register("acme", agent_url="https://acme.example.com", platform=_mock_platform())
    assert registry.health("acme") == "pending"

    await registry.recheck("acme")
    assert registry.health("acme") == "healthy"


@pytest.mark.asyncio
async def test_recheck_disabled_to_healthy() -> None:
    calls = [False, True]

    def toggling(tid: str, url: str) -> bool:
        return calls.pop(0)

    registry = TenantRegistry(validator=toggling)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "disabled"

    await registry.recheck("acme")
    assert registry.health("acme") == "healthy"


@pytest.mark.asyncio
async def test_recheck_healthy_failure_goes_unverified() -> None:
    calls = [True, False]

    def toggling(tid: str, url: str) -> bool:
        return calls.pop(0)

    registry = TenantRegistry(validator=toggling)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "healthy"

    await registry.recheck("acme")
    assert registry.health("acme") == "unverified"


@pytest.mark.asyncio
async def test_recheck_unverified_failure_goes_disabled() -> None:
    # Start healthy → unverified → disabled
    calls = [True, False, False]

    def toggling(tid: str, url: str) -> bool:
        return calls.pop(0)

    registry = TenantRegistry(validator=toggling)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    await registry.recheck("acme")  # healthy → unverified
    assert registry.health("acme") == "unverified"

    await registry.recheck("acme")  # unverified → disabled
    assert registry.health("acme") == "disabled"


@pytest.mark.asyncio
async def test_recheck_raises_for_unknown_tenant() -> None:
    registry = TenantRegistry()
    with pytest.raises(KeyError, match="ghost"):
        await registry.recheck("ghost")


@pytest.mark.asyncio
async def test_recheck_validator_raises_updates_state_then_reraises() -> None:
    call_count = 0

    def first_ok_then_raise(tid: str, url: str) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return True
        raise RuntimeError("validator exploded")

    registry = TenantRegistry(validator=first_ok_then_raise)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("acme") == "healthy"

    with pytest.raises(RuntimeError, match="validator exploded"):
        await registry.recheck("acme")

    # Was healthy before the failed recheck → unverified (graceful-degrade).
    assert registry.health("acme") == "unverified"


# ---------------------------------------------------------------------------
# Validator receives correct arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_receives_tenant_id_and_agent_url() -> None:
    received: list[tuple[str, str]] = []

    def capture(tid: str, url: str) -> bool:
        received.append((tid, url))
        return True

    registry = TenantRegistry(validator=capture)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com/agent",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert received == [("acme", "https://acme.example.com/agent")]


# ---------------------------------------------------------------------------
# registered_tenants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_tenants_reflects_mutations() -> None:
    registry = TenantRegistry()
    await registry.register("a", agent_url="https://a.example.com", platform=_mock_platform())
    await registry.register("b", agent_url="https://b.example.com", platform=_mock_platform())
    assert registry.registered_tenants == {"a", "b"}

    registry.unregister("a")
    assert registry.registered_tenants == {"b"}


# ---------------------------------------------------------------------------
# Concurrency — per-tenant lock prevents TOCTOU on state transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_rechecks_same_tenant_do_not_corrupt_state() -> None:
    """Two concurrent rechecks on the same tenant complete without
    corrupting state — the per-tenant lock serializes them.

    The validator yields (``asyncio.sleep(0)``) to give the event loop
    a chance to interleave. The lock ensures only one transition runs
    at a time, so both complete and the list is fully consumed.
    """
    results = [True, True]

    async def async_validator(tid: str, url: str) -> bool:
        await asyncio.sleep(0)  # yield to event loop — maximises interleave opportunity
        return results.pop(0)

    registry = TenantRegistry(
        validator=lambda tid, url: async_validator(tid, url),
    )
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=_mock_platform(),
    )

    await asyncio.gather(registry.recheck("acme"), registry.recheck("acme"))
    # Both rechecks ran (list fully consumed) and final state is healthy.
    assert results == []
    assert registry.health("acme") == "healthy"


@pytest.mark.asyncio
async def test_unregister_during_recheck_no_zombie_health_entry() -> None:
    """unregister() called while recheck() is awaiting the validator must
    not leave a zombie _health entry — health() and registered_tenants
    must reflect the clean removal."""
    recheck_started = asyncio.Event()
    allow_recheck = asyncio.Event()

    async def blocking_validator(tid: str, url: str) -> bool:
        recheck_started.set()
        await allow_recheck.wait()
        return True

    registry = TenantRegistry(validator=blocking_validator)
    await registry.register("acme", agent_url="https://acme.example.com", platform=_mock_platform())

    async def do_recheck() -> None:
        await registry.recheck("acme")

    recheck_task = asyncio.create_task(do_recheck())
    await recheck_started.wait()  # recheck is now suspended inside the validator

    registry.unregister("acme")   # race: remove while validator is awaited

    allow_recheck.set()
    await recheck_task            # recheck completes without raising

    # No zombie: tenant is fully gone.
    assert registry.health("acme") is None
    assert "acme" not in registry.registered_tenants


@pytest.mark.asyncio
async def test_multiple_tenants_independent() -> None:
    """Health state for one tenant does not affect another."""
    registry = TenantRegistry(validator=lambda tid, url: tid == "good")
    await registry.register(
        "good",
        agent_url="https://good.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    await registry.register(
        "bad",
        agent_url="https://bad.example.com",
        platform=_mock_platform(),
        await_first_validation=True,
    )
    assert registry.health("good") == "healthy"
    assert registry.health("bad") == "disabled"


# ---------------------------------------------------------------------------
# Lazy registration — register_lazy + resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_lazy_sets_pending_health() -> None:
    registry = TenantRegistry()

    async def factory(tid: str) -> Any:
        return _mock_platform(tid)

    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)
    assert registry.health("acme") == "pending"
    assert "acme" in registry.registered_tenants


@pytest.mark.asyncio
async def test_resolve_by_host_returns_none_for_lazy_unresolved() -> None:
    """resolve_by_host (sync) returns None until the lazy platform is built."""
    registry = TenantRegistry()

    async def factory(tid: str) -> Any:
        return _mock_platform(tid)

    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)
    assert registry.resolve_by_host("acme.example.com") is None


@pytest.mark.asyncio
async def test_resolve_builds_lazy_platform_on_first_call() -> None:
    platform = _mock_platform("acme")
    call_count = 0

    async def factory(tid: str) -> Any:
        nonlocal call_count
        call_count += 1
        return platform

    registry = TenantRegistry(validator=None)
    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)

    result = await registry.resolve("acme.example.com")
    assert result is not None
    assert result.tenant_id == "acme"
    assert result.health == "healthy"
    assert result.platform is platform
    assert call_count == 1

    # Second call must use the cached platform — factory not called again.
    result2 = await registry.resolve("acme.example.com")
    assert result2 is not None
    assert result2.platform is platform
    assert call_count == 1


@pytest.mark.asyncio
async def test_resolve_fast_path_for_eager_tenant() -> None:
    """resolve() with an eager tenant does not invoke any factory."""
    platform = _mock_platform()
    registry = TenantRegistry(validator=None)
    await registry.register("acme", agent_url="https://acme.example.com", platform=platform,
                             await_first_validation=True)

    result = await registry.resolve("acme.example.com")
    assert result is not None
    assert result.health == "healthy"
    assert result.platform is platform


@pytest.mark.asyncio
async def test_resolve_returns_none_for_unknown_host() -> None:
    registry = TenantRegistry()
    assert await registry.resolve("unknown.example.com") is None


@pytest.mark.asyncio
async def test_register_lazy_await_first_validation_builds_immediately() -> None:
    platform = _mock_platform()
    factory_called = False

    async def factory(tid: str) -> Any:
        nonlocal factory_called
        factory_called = True
        return platform

    registry = TenantRegistry(validator=None)
    await registry.register_lazy(
        "acme",
        agent_url="https://acme.example.com",
        factory=factory,
        await_first_validation=True,
    )

    assert factory_called
    assert registry.health("acme") == "healthy"

    # resolve() must hit the fast path — no second factory invocation.
    result = await registry.resolve("acme.example.com")
    assert result is not None
    assert result.health == "healthy"


@pytest.mark.asyncio
async def test_register_lazy_await_first_validation_factory_raises_disabled() -> None:
    async def bad_factory(tid: str) -> Any:
        raise RuntimeError("KMS unreachable")

    registry = TenantRegistry()
    await registry.register_lazy(
        "acme",
        agent_url="https://acme.example.com",
        factory=bad_factory,
        await_first_validation=True,
    )
    assert registry.health("acme") == "disabled"


@pytest.mark.asyncio
async def test_resolve_factory_raises_sets_disabled_returns_none() -> None:
    async def bad_factory(tid: str) -> Any:
        raise RuntimeError("factory exploded")

    registry = TenantRegistry()
    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=bad_factory)
    assert registry.health("acme") == "pending"

    result = await registry.resolve("acme.example.com")
    assert result is None
    assert registry.health("acme") == "disabled"


@pytest.mark.asyncio
async def test_resolve_validator_fails_sets_disabled_returns_none() -> None:
    async def factory(tid: str) -> Any:
        return _mock_platform(tid)

    registry = TenantRegistry(validator=lambda tid, url: False)
    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)

    result = await registry.resolve("acme.example.com")
    assert result is None
    assert registry.health("acme") == "disabled"


@pytest.mark.asyncio
async def test_resolve_concurrent_first_hit_invokes_factory_once() -> None:
    """Concurrent first-hit resolves serialize on the per-tenant lock;
    only one factory invocation occurs."""
    factory_call_count = 0

    async def factory(tid: str) -> Any:
        nonlocal factory_call_count
        factory_call_count += 1
        await asyncio.sleep(0)  # yield to maximise interleave opportunity
        return _mock_platform(tid)

    registry = TenantRegistry(validator=None)
    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)

    results = await asyncio.gather(
        registry.resolve("acme.example.com"),
        registry.resolve("acme.example.com"),
        registry.resolve("acme.example.com"),
    )
    assert all(r is not None for r in results)
    assert factory_call_count == 1


@pytest.mark.asyncio
async def test_lazy_unregister_during_resolve_no_zombie() -> None:
    """unregister() called while resolve() is awaiting the factory must
    not leave a zombie health entry."""
    factory_started = asyncio.Event()
    allow_factory = asyncio.Event()

    async def blocking_factory(tid: str) -> Any:
        factory_started.set()
        await allow_factory.wait()
        return _mock_platform(tid)

    registry = TenantRegistry(validator=None)
    await registry.register_lazy("acme", agent_url="https://acme.example.com",
                                  factory=blocking_factory)

    resolve_task = asyncio.create_task(registry.resolve("acme.example.com"))
    await factory_started.wait()

    registry.unregister("acme")

    allow_factory.set()
    result = await resolve_task

    # The tenant was removed mid-flight — result must be None and no zombie.
    assert result is None
    assert registry.health("acme") is None
    assert "acme" not in registry.registered_tenants


@pytest.mark.asyncio
async def test_reregister_eager_after_lazy_clears_factory() -> None:
    """Re-registering a lazy tenant as eager clears the factory and uses
    the pre-built platform immediately."""
    platform = _mock_platform("eager")
    factory_called = False

    async def factory(tid: str) -> Any:
        nonlocal factory_called
        factory_called = True
        return _mock_platform("lazy")

    registry = TenantRegistry(validator=None)
    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)
    # Now re-register as eager.
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=platform,
        await_first_validation=True,
    )

    result = await registry.resolve("acme.example.com")
    assert result is not None
    assert result.platform is platform
    assert not factory_called


@pytest.mark.asyncio
async def test_reregister_lazy_after_eager_clears_platform() -> None:
    """Re-registering an eager tenant as lazy clears the cached platform;
    resolve() must invoke the new factory."""
    old_platform = _mock_platform("old-eager")
    new_platform = _mock_platform("new-lazy")

    async def factory(tid: str) -> Any:
        return new_platform

    registry = TenantRegistry(validator=None)
    await registry.register(
        "acme",
        agent_url="https://acme.example.com",
        platform=old_platform,
        await_first_validation=True,
    )
    # Verify sync path sees the old platform.
    assert registry.resolve_by_host("acme.example.com") is not None

    # Re-register as lazy — should clear the old platform.
    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)
    # Sync resolve_by_host now returns None (platform cleared).
    assert registry.resolve_by_host("acme.example.com") is None

    # Async resolve builds the new platform.
    result = await registry.resolve("acme.example.com")
    assert result is not None
    assert result.platform is new_platform


@pytest.mark.asyncio
async def test_register_lazy_await_first_validation_validator_false_does_not_cache() -> None:
    """When validator returns False in register_lazy(await_first_validation=True),
    platform must NOT be cached and factory must be cleared — mirrors resolve()
    cold-path behavior so disabled tenants are consistent regardless of how they
    were registered."""
    platform = _mock_platform()

    async def factory(tid: str) -> Any:
        return platform

    registry = TenantRegistry(validator=lambda tid, url: False)
    await registry.register_lazy(
        "acme",
        agent_url="https://acme.example.com",
        factory=factory,
        await_first_validation=True,
    )
    assert registry.health("acme") == "disabled"
    # Sync path must return None (platform not cached).
    assert registry.resolve_by_host("acme.example.com") is None
    # Async path must also return None (factory was cleared, no retry).
    assert await registry.resolve("acme.example.com") is None


@pytest.mark.asyncio
async def test_resolve_factory_failure_does_not_retry_on_subsequent_calls() -> None:
    """After factory failure sets health=disabled, subsequent resolve() calls
    must not re-invoke the factory — disabled tenants need operator recheck()."""
    call_count = 0

    async def bad_factory(tid: str) -> Any:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("factory exploded")

    registry = TenantRegistry()
    await registry.register_lazy("acme", agent_url="https://acme.example.com",
                                  factory=bad_factory)

    # First resolve: factory invoked, sets disabled.
    result1 = await registry.resolve("acme.example.com")
    assert result1 is None
    assert registry.health("acme") == "disabled"
    assert call_count == 1

    # Subsequent resolves: factory must NOT be called again.
    result2 = await registry.resolve("acme.example.com")
    assert result2 is None
    assert call_count == 1


@pytest.mark.asyncio
async def test_unregister_lazy_tenant_removes_factory() -> None:
    """Unregistering a lazy tenant removes the factory; resolve() returns None."""
    async def factory(tid: str) -> Any:
        return _mock_platform(tid)

    registry = TenantRegistry()
    await registry.register_lazy("acme", agent_url="https://acme.example.com", factory=factory)
    registry.unregister("acme")

    assert registry.health("acme") is None
    assert await registry.resolve("acme.example.com") is None
