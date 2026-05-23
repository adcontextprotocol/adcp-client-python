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

import pytest

from adcp.decisioning import (
    AuthInfo,
    BuyerAgent,
    BuyerAgentRegistry,
    HttpSigCredential,
)
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
async def test_enforce_does_not_sleep_when_already_overrun(monkeypatch):
    """A branch that already exceeded the budget passes through immediately."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "5")
    budget = PermissionDeniedBudget()
    # Burn more than the budget before enforce().
    await asyncio.sleep(0.020)
    start = time.perf_counter()
    await budget.enforce()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms < 5.0, f"enforce() should be a no-op once overrun, got {elapsed_ms:.1f} ms"


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
    """A registry whose resolve methods take a configurable amount of time."""

    def __init__(self, delay_ms: float, agent: BuyerAgent | None) -> None:
        self._delay_seconds = delay_ms / 1000.0
        self._agent = agent

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        await asyncio.sleep(self._delay_seconds)
        return self._agent

    async def resolve_by_credential(self, credential):  # pragma: no cover - not used here
        await asyncio.sleep(self._delay_seconds)
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
async def test_branch_parity_absorbs_registry_variance(monkeypatch):
    """The whole point: fast registry-miss + slow real-row read should
    both come out at ≈ budget, not (0 ms, registry_delay)."""
    monkeypatch.setenv(BUDGET_ENV_VAR, "50")

    # Branch 1: registry-miss path with a fast cache-miss-returning-None.
    fast_miss = _SlowRegistry(delay_ms=1.0, agent=None)
    miss_samples: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        with pytest.raises(AdcpError):
            await _resolve_buyer_agent(fast_miss, _http_sig_auth("https://buyer.example"))
        miss_samples.append((time.perf_counter() - start) * 1000.0)

    # Branch 2: unknown-status path with a slower registry read for a real row.
    unknown_agent = BuyerAgent(
        agent_url="https://buyer.example",
        display_name="Test Buyer",
        status="pending_review",
    )
    slow_hit = _SlowRegistry(delay_ms=10.0, agent=unknown_agent)
    hit_samples: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        with pytest.raises(AdcpError):
            await _resolve_buyer_agent(slow_hit, _http_sig_auth("https://buyer.example"))
        hit_samples.append((time.perf_counter() - start) * 1000.0)

    miss_max = max(miss_samples)
    hit_max = max(hit_samples)
    # Both should be dominated by the 50 ms budget. The branch difference
    # must be much smaller than the registry-delay difference (9 ms) that
    # would leak without the budget.
    branch_delta = abs(hit_max - miss_max)
    assert (
        branch_delta < 15.0
    ), f"branch parity broken: hit={hit_max:.1f} ms vs miss={miss_max:.1f} ms"
    # Both should be at or above the budget.
    assert min(miss_samples + hit_samples) >= 45.0
