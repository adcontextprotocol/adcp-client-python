"""Timing-oracle defense for the two ``PERMISSION_DENIED`` branches of
:func:`adcp.decisioning.handler._resolve_buyer_agent`.

Background
----------

After #748 migrated suspended / blocked agents to dedicated wire codes
(``AGENT_SUSPENDED`` / ``AGENT_BLOCKED``), the only paths that still emit
``PERMISSION_DENIED`` from the commercial-identity gate are:

* **registry-miss / no-credential / unauthenticated** — ``agent is None``.
* **unknown-status default-reject** — ``agent.status`` is neither
  ``active`` nor a recognized rejection status.

Both must be observably indistinguishable in latency: registry I/O on the
miss branch may be fast (cache hit returning ``None``) while the
default-reject branch always pays the registry read for a real row, which
is a natural timing oracle ("if the response is fast, the agent_url is
not onboarded"). The budget below floors both branches at the same
deadline so the I/O variance is absorbed into the wait.

Why not on the dedicated-code branches
--------------------------------------

``AGENT_SUSPENDED`` / ``AGENT_BLOCKED`` are intentionally distinct wire
codes — the code itself is the discriminator. An attacker who reads the
code already knows the agent is recognized; the latency carries no
additional bit. Per #772 the budget is scoped to ``PERMISSION_DENIED``
only.

Failure mode
------------

Operators tune the budget via ``ADCP_PERMISSION_DENIED_BUDGET_MS``. Any
non-numeric, non-finite, or non-positive value falls back to the default
and logs at WARNING. The budget is **never silently disabled** — an
operator who fat-fingers the env var must see the deviation in their
logs, because the alternative is a timing-oracle regression they cannot
detect by reading the response.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time

BUDGET_ENV_VAR = "ADCP_PERMISSION_DENIED_BUDGET_MS"
DEFAULT_BUDGET_MS = 50.0

_logger = logging.getLogger(__name__)


def parse_budget_ms(raw: str | None = None) -> float:
    """Resolve the budget in milliseconds.

    ``raw=None`` reads from the environment; an explicit string lets
    tests exercise the parser without monkeypatching ``os.environ``.

    Falls back to :data:`DEFAULT_BUDGET_MS` (with a WARNING log) on:

    * unset env var (no warning — this is the expected default path);
    * non-numeric (``"abc"``, ``""``);
    * non-finite (``"nan"``, ``"inf"``, ``"-inf"``);
    * non-positive (``"0"``, ``"-5"``).
    """
    if raw is None:
        raw = os.environ.get(BUDGET_ENV_VAR)
    if raw is None:
        return DEFAULT_BUDGET_MS
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        _logger.warning(
            "%s=%r is not numeric; falling back to default %s ms. The "
            "PERMISSION_DENIED timing-oracle defense is still active at "
            "the default budget.",
            BUDGET_ENV_VAR,
            raw,
            DEFAULT_BUDGET_MS,
        )
        return DEFAULT_BUDGET_MS
    if not math.isfinite(ms) or ms <= 0:
        _logger.warning(
            "%s=%r is not a positive finite number; falling back to "
            "default %s ms. The PERMISSION_DENIED timing-oracle defense "
            "is never silently disabled — set a positive finite value to "
            "override the default.",
            BUDGET_ENV_VAR,
            raw,
            DEFAULT_BUDGET_MS,
        )
        return DEFAULT_BUDGET_MS
    return ms


class PermissionDeniedBudget:
    """Per-request budget instance. Construct at function entry, call
    :meth:`enforce` immediately before raising ``PERMISSION_DENIED``.

    The deadline is measured from the construction site, not from the
    branch site, so I/O latency variance between branches is absorbed
    into the budget rather than added on top of it.
    """

    __slots__ = ("_budget_seconds", "_start")

    def __init__(self) -> None:
        self._budget_seconds = parse_budget_ms() / 1000.0
        self._start = time.perf_counter()

    @property
    def budget_seconds(self) -> float:
        return self._budget_seconds

    async def enforce(self) -> None:
        elapsed = time.perf_counter() - self._start
        remaining = self._budget_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
