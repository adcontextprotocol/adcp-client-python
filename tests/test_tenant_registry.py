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
