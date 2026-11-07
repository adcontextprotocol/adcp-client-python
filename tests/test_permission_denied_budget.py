"""Unit + integration tests for the PERMISSION_DENIED timing budget.

Covers issue #772 part 1 (the narrow follow-up extracted from #392/#735):

* Env-var hardening: every invalid value falls back to the default,
  logs WARNING, and never silently disables the timing-oracle defense.
* Deadline-relative sleep: branches that complete before the budget
  expires get padded to the budget; branches that overrun pass through
  immediately.
* Branch parity through ``_resolve_buyer_agent``: the two
  ``PERMISSION_DENIED`` branches (registry-miss and unknown-status
  default-reject) both wait for the budget, so a synthetic slow
  registry on one branch and a fast registry on the other no longer
  produce a timing oracle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from adcp.decisioning import (
    AuthInfo,
    BuyerAgent,
    BuyerAgentRegistry,
    HttpSigCredential,
)
from adcp.decisioning import _permission_denied_budget as budget_module
from adcp.decisioning._permission_denied_budget import (
    BUDGET_ENV_VAR,
    DEFAULT_BUDGET_MS,
    PermissionDeniedBudget,
    parse_budget_ms,
)
from adcp.decisioning.handler import _resolve_buyer_agent
from adcp.decisioning.types import AdcpError

# ---------------------------------------------------------------------------
# parse_budget_ms — env-var hardening
# ---------------------------------------------------------------------------


def test_parse_unset_returns_default():
    assert parse_budget_ms(None) == DEFAULT_BUDGET_MS


@pytest.mark.parametrize(
    "raw",
    ["100", "100.0", "1.5", "0.1", "10000"],
)
def test_parse_valid_positive_finite(raw: str):
    assert parse_budget_ms(raw) == float(raw)


@pytest.mark.parametrize(
    "raw",
    ["abc", "", "  ", "100ms", "fifty"],
)
def test_parse_non_numeric_falls_back_with_warning(raw: str, caplog):
    with caplog.at_level(logging.WARNING, logger="adcp.decisioning._permission_denied_budget"):
        assert parse_budget_ms(raw) == DEFAULT_BUDGET_MS
    assert any("not numeric" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "-100.5", "nan", "NaN", "inf", "-inf", "Infinity"],
)
def test_parse_non_positive_or_non_finite_falls_back_with_warning(raw: str, caplog):
    with caplog.at_level(logging.WARNING, logger="adcp.decisioning._permission_denied_budget"):
        assert parse_budget_ms(raw) == DEFAULT_BUDGET_MS
    assert any("positive finite" in r.message for r in caplog.records)


def test_env_var_read_when_no_explicit_arg(monkeypatch):
    monkeypatch.setenv(BUDGET_ENV_VAR, "123.5")
    assert parse_budget_ms() == 123.5


def test_env_var_invalid_still_falls_back(monkeypatch, caplog):
    monkeypatch.setenv(BUDGET_ENV_VAR, "nan")
    with caplog.at_level(logging.WARNING, logger="adcp.decisioning._permission_denied_budget"):
        assert parse_budget_ms() == DEFAULT_BUDGET_MS
    assert any("positive finite" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# PermissionDeniedBudget — deadline-relative sleep
# ---------------------------------------------------------------------------


class _VirtualClock:
    """Deterministic time for the budget module.

    Upper bounds and differences between real elapsed times depend on how
    busy the machine is (``pytest -n auto`` saturates it), so the tests that
    assert them run the budget on this clock instead: ``perf_counter`` reads
    it, and ``sleep`` and the fake registry advance it, exactly.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def perf_counter(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(seconds, 0.0)
        await asyncio.sleep(0)

    def elapsed_ms_since(self, start: float) -> float:
        return (self.now - start) * 1000.0


@pytest.fixture
def virtual_clock(monkeypatch) -> _VirtualClock:
    clock = _VirtualClock()
    monkeypatch.setattr(budget_module, "time", SimpleNamespace(perf_counter=clock.perf_counter))
    monkeypatch.setattr(budget_module, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return clock


@pytest.mark.asyncio
async def test_enforce_sleeps_to_budget_when_branch_is_fast(monkeypatch):
    """A branch that does no work waits the full budget."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "20")
    budget = PermissionDeniedBudget()
    start = time.perf_counter()
    await budget.enforce()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    # Allow a little slack for asyncio.sleep overhead but require the
    # bulk of the budget to have been honored.
    assert elapsed_ms >= 18.0, f"budget not honored: only {elapsed_ms:.1f} ms elapsed"


@pytest.mark.asyncio
async def test_enforce_does_not_sleep_when_already_overrun(monkeypatch, virtual_clock):
    """A branch that already exceeded the budget passes through immediately."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "5")
    budget = PermissionDeniedBudget()
    # Burn more than the budget before enforce().
    virtual_clock.now += 0.020
    await budget.enforce()
    assert virtual_clock.sleeps == []


@pytest.mark.asyncio
async def test_invalid_env_var_still_enforces_default_budget(monkeypatch, caplog):
    """The defense must not be silently disabled by a malformed env var."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "0")
    with caplog.at_level(logging.WARNING, logger="adcp.decisioning._permission_denied_budget"):
        budget = PermissionDeniedBudget()
    assert budget.budget_seconds == DEFAULT_BUDGET_MS / 1000.0
    start = time.perf_counter()
    await budget.enforce()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms >= DEFAULT_BUDGET_MS - 5.0


@pytest.mark.asyncio
async def test_inf_does_not_hang(monkeypatch):
    """Regression: `inf` previously could have produced asyncio.sleep(inf)."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "inf")
    budget = PermissionDeniedBudget()
    # The constructor falls back to the default; the budget is now
    # ~50 ms, not infinity. Use a wall-clock timeout to catch a hang.
    await asyncio.wait_for(budget.enforce(), timeout=0.5)


# ---------------------------------------------------------------------------
# Integration through _resolve_buyer_agent: branch parity
# ---------------------------------------------------------------------------


class _SlowRegistry(BuyerAgentRegistry):
    """A registry whose resolve methods take a configurable amount of time.

    With ``clock``, the delay advances that virtual clock instead of sleeping.
    """

    def __init__(
        self, delay_ms: float, agent: BuyerAgent | None, *, clock: _VirtualClock | None = None
    ) -> None:
        self._delay_seconds = delay_ms / 1000.0
        self._agent = agent
        self._clock = clock

    async def _delay(self) -> None:
        if self._clock is not None:
            await self._clock.sleep(self._delay_seconds)
        else:
            await asyncio.sleep(self._delay_seconds)

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        await self._delay()
        return self._agent

    async def resolve_by_credential(self, credential):  # pragma: no cover - not used here
        await self._delay()
        return self._agent


def _http_sig_auth(agent_url: str) -> AuthInfo:
    return AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig",
            keyid="kid-1",
            agent_url=agent_url,
            verified_at=1700000000.0,
        ),
    )


@pytest.mark.asyncio
async def test_unrecognized_branch_honors_budget(monkeypatch):
    """A fast registry-miss must still wait for the budget before raising."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "30")
    registry = _SlowRegistry(delay_ms=0.0, agent=None)
    start = time.perf_counter()
    with pytest.raises(AdcpError) as exc:
        await _resolve_buyer_agent(registry, _http_sig_auth("https://buyer.example"))
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert exc.value.code == "PERMISSION_DENIED"
    assert elapsed_ms >= 28.0, f"unrecognized branch returned in {elapsed_ms:.1f} ms"


@pytest.mark.asyncio
async def test_unknown_status_branch_honors_budget(monkeypatch):
    """A fast registry hit with an unknown status must also wait."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "30")
    # Construct a BuyerAgent with a status the framework doesn't recognize.
    # The default-reject branch projects PERMISSION_DENIED with no details.
    agent = BuyerAgent(
        agent_url="https://buyer.example",
        display_name="Test Buyer",
        status="pending_review",  # not active/suspended/blocked
    )
    registry = _SlowRegistry(delay_ms=0.0, agent=agent)
    start = time.perf_counter()
    with pytest.raises(AdcpError) as exc:
        await _resolve_buyer_agent(registry, _http_sig_auth("https://buyer.example"))
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert exc.value.code == "PERMISSION_DENIED"
    assert elapsed_ms >= 28.0, f"unknown-status branch returned in {elapsed_ms:.1f} ms"


@pytest.mark.asyncio
async def test_dedicated_code_branches_skip_budget(monkeypatch):
    """AGENT_SUSPENDED / AGENT_BLOCKED don't pay the budget — the code IS the discriminator."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "500")  # big budget so we'd notice
    for status, expected_code in [("suspended", "AGENT_SUSPENDED"), ("blocked", "AGENT_BLOCKED")]:
        agent = BuyerAgent(
            agent_url="https://buyer.example",
            display_name="Test Buyer",
            status=status,
        )
        registry = _SlowRegistry(delay_ms=0.0, agent=agent)
        start = time.perf_counter()
        with pytest.raises(AdcpError) as exc:
            await _resolve_buyer_agent(registry, _http_sig_auth("https://buyer.example"))
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert exc.value.code == expected_code
        assert (
            elapsed_ms < 200.0
        ), f"{status} branch waited {elapsed_ms:.1f} ms — should skip the budget"


@pytest.mark.asyncio
async def test_active_branch_skips_budget(monkeypatch):
    """The happy path obviously doesn't pay the budget."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "500")
    agent = BuyerAgent(
        agent_url="https://buyer.example",
        display_name="Test Buyer",
        status="active",
    )
    registry = _SlowRegistry(delay_ms=0.0, agent=agent)
    start = time.perf_counter()
    resolved = await _resolve_buyer_agent(registry, _http_sig_auth("https://buyer.example"))
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert resolved is agent
    assert elapsed_ms < 200.0


@pytest.mark.asyncio
async def test_branch_parity_absorbs_registry_variance(monkeypatch, virtual_clock):
    """The whole point: fast registry-miss + slow real-row read should
    both come out at the budget, not (0 ms, registry_delay)."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "50")

    async def elapsed_ms(registry: _SlowRegistry) -> float:
        start = virtual_clock.now
        with pytest.raises(AdcpError) as exc:
            await _resolve_buyer_agent(registry, _http_sig_auth("https://buyer.example"))
        assert exc.value.code == "PERMISSION_DENIED"
        return virtual_clock.elapsed_ms_since(start)

    # Branch 1: registry-miss path with a fast cache-miss-returning-None.
    fast_miss = _SlowRegistry(delay_ms=1.0, agent=None, clock=virtual_clock)
    # Branch 2: unknown-status path with a slower registry read for a real row.
    unknown_agent = BuyerAgent(
        agent_url="https://buyer.example",
        display_name="Test Buyer",
        status="pending_review",
    )
    slow_hit = _SlowRegistry(delay_ms=10.0, agent=unknown_agent, clock=virtual_clock)

    # Both branches end exactly at the 50 ms budget: the 9 ms registry-delay
    # difference that would leak without the budget is absorbed into it.
    assert await elapsed_ms(fast_miss) == pytest.approx(50.0)
    assert await elapsed_ms(slow_hit) == pytest.approx(50.0)
    # A registry read slower than the budget is not padded further.
    slower_than_budget = _SlowRegistry(delay_ms=80.0, agent=unknown_agent, clock=virtual_clock)
    assert await elapsed_ms(slower_than_budget) == pytest.approx(80.0)
