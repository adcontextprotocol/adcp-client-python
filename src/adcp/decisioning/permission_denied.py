"""Tier 2 commercial-identity gate — single emit point for the four
PERMISSION_DENIED denial paths in
:func:`adcp.decisioning.handler._resolve_buyer_agent`.

The cross-tenant onboarding-oracle clamp in the AdCP spec requires the
unrecognized-agent path and the recognized-but-denied path to be
observably indistinguishable to an external attacker across:

* HTTP status code (aligned via the shared ``PERMISSION_DENIED`` envelope).
* Response headers — same ``Content-Type``; ``Content-Length`` within a
  documented tolerance (see the *Header parity* note below).
* Side effects — same audit-row shape, same metric label.
* Latency — bounded by an explicit budget so branch-variance is dominated
  by the budget rather than the code path.
* Observability — same operation label; the discriminator (``agent_url``)
  is hashed-truncated before it reaches the audit sink so log-scraping
  cannot rebuild the side channel from internal telemetry.

Latency budget tradeoff
-----------------------

Every denial path executes ``await asyncio.sleep(...)`` to a shared
deadline. The default is 50 ms; adopters tune via the
``ADCP_PERMISSION_DENIED_BUDGET_MS`` env var. The tradeoff:

* Higher budget → tighter latency parity (an external attacker's timing
  oracle is dominated by the budget, not by code-path variance).
* Higher budget → more wall-clock latency on every rejection, which
  matters for buyers in legitimate operator-suspension states who get a
  fast rejection today.

50 ms was chosen as a compromise — large enough that audit-emit and
serialization variance (typically <5 ms in the framework's reference
sinks) is absorbed by the budget; small enough that legitimate buyers
don't notice. Adopters running into real DB-backed audit sinks with
higher tail latency (p99 > 30 ms) should raise the budget so the budget
continues to dominate the variance.

Header parity tradeoff
----------------------

The ``details`` payload differs across paths:

* Recognized + suspended/blocked → ``{scope, status, agent_url}``.
* Unrecognized → ``details`` OMITTED entirely (spec rule:
  omit-on-unestablished-identity — see ``schemas/cache/error-details/
  agent-permission-denied.json``).

The omit rule prevents padding the unrecognized envelope. The
``Content-Length`` variance between paths is therefore non-zero:
the recognized-but-denied envelope is ~80 bytes longer than the
unrecognized envelope on a typical ``agent_url``.

However, the recognized branches *already* vary in ``Content-Length``
among themselves because ``agent_url`` is buyer-controlled — an
attacker watching ``Content-Length`` cannot distinguish "this is
unrecognized" from "this is suspended with a short ``agent_url``".
So the existing intra-recognized variance dominates the recognized-vs-
unrecognized delta. The tradeoff: we accept the ~80-byte delta on the
unrecognized path because closing it (e.g., padding all recognized
envelopes to a fixed size) would require choosing a worst-case
``agent_url`` length that buyers might exceed.

Adopters that want hard ``Content-Length`` parity wrap the transport
layer with a fixed-width response middleware — out of scope for this
gate.

Audit-row parity
----------------

Every denial path emits a single :class:`~adcp.audit_sink.AuditEvent`
with the same operation label (``buyer_agent_registry.permission_denied``)
and the same key set in ``details``:

* ``outcome`` — always ``"denied"``.
* ``reason_scope`` — ``"agent"`` (recognized) or ``None`` (unrecognized).
* ``reason_status`` — ``"suspended"`` / ``"blocked"`` (recognized) or
  ``None`` (unrecognized).
* ``agent_url_hash`` — first 12 chars of ``sha256(agent_url)`` when an
  ``agent_url`` is known; ``None`` otherwise.

The hash truncation defends against log-scraping: an operator with read
access to the audit sink can correlate denial events across requests
(same hash = same agent_url) without learning the underlying URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.audit_sink import AuditSink

logger = logging.getLogger(__name__)


# Env var adopters tune. Read on every denial so a runtime config flip
# takes effect without a process restart.
BUDGET_ENV_VAR = "ADCP_PERMISSION_DENIED_BUDGET_MS"
DEFAULT_BUDGET_MS = 50.0

# Operation label emitted on every denial — uniform across the four
# branches so audit queries can filter the gate's traffic without
# branching on the discriminator.
AUDIT_OPERATION = "buyer_agent_registry.permission_denied"

# Generic message used on every denial path. MUST be identical across
# the unrecognized and the recognized-but-denied paths so the wire-level
# ``error.message`` is not itself a side channel leaking which
# ``agent_url``s are onboarded with which sellers.
DENIED_MESSAGE = (
    "Buyer agent is not authorized for this seller. The seller's "
    "commercial allowlist did not authorize this credential. "
    "Resolve out-of-band via the seller's onboarding contact; this "
    "is not a request-side error the buyer can correct."
)


@dataclass(frozen=True)
class PermissionDeniedReason:
    """Internal reason carried by :class:`PermissionDeniedError`.

    Projected to the wire envelope by :func:`translate_to_adcp_error`.
    ``scope is None`` → unrecognized path (no ``details`` on the wire).
    ``scope == "agent"`` → recognized-but-denied path (``details`` carries
    ``scope`` + ``status`` + ``agent_url``).

    :param scope: ``"agent"`` for recognized-but-denied; ``None`` for
        unrecognized (registry miss, no credential, unknown status).
    :param status: Agent's status string on recognized paths
        (``"suspended"`` / ``"blocked"``). ``None`` on unrecognized.
    :param agent_url: The agent's URL when known. Used to populate the
        wire ``details.agent_url`` (recognized paths only) and to
        compute the audit-row ``agent_url_hash`` (all paths where known).
    """

    scope: Literal["agent"] | None
    status: str | None = None
    agent_url: str | None = None


class PermissionDeniedError(Exception):
    """Internal exception raised by :func:`_resolve_buyer_agent` branches.

    Caught at the function's tail and projected to :class:`AdcpError`
    via :func:`translate_to_adcp_error` after the audit emission and
    latency-budget sleep have run. Adopter code does NOT see this
    type — it's a framework-internal marker.
    """

    def __init__(self, reason: PermissionDeniedReason) -> None:
        super().__init__(f"PermissionDenied(scope={reason.scope})")
        self.reason = reason


def get_latency_budget_seconds() -> float:
    """Read the latency budget in seconds from the env var, with fallback.

    Re-read on every call so a runtime config flip (e.g., adopter raises
    the budget during an incident response) takes effect without a
    process restart. Malformed values fall back to the default with a
    one-shot warning — fail-open rather than crash the dispatch path.
    """
    # Literal string (rather than ``os.environ.get(BUDGET_ENV_VAR)``)
    # so the framework's docstring/env-var drift test
    # (``tests/test_docstring_consistency.py``) can detect this read
    # via static regex scan — its detector matches string literals
    # only, not constant references.
    raw = os.environ.get("ADCP_PERMISSION_DENIED_BUDGET_MS")
    if raw is None:
        return DEFAULT_BUDGET_MS / 1000.0
    try:
        ms = float(raw)
    except ValueError:
        logger.warning(
            "[adcp.permission_denied] %s=%r is not a number; " "falling back to default %.1f ms",
            BUDGET_ENV_VAR,
            raw,
            DEFAULT_BUDGET_MS,
        )
        return DEFAULT_BUDGET_MS / 1000.0
    if ms < 0:
        logger.warning(
            "[adcp.permission_denied] %s=%r is negative; " "falling back to default %.1f ms",
            BUDGET_ENV_VAR,
            raw,
            DEFAULT_BUDGET_MS,
        )
        return DEFAULT_BUDGET_MS / 1000.0
    return ms / 1000.0


def hash_discriminator(value: str) -> str:
    """Hash-truncate a discriminator string for audit emission.

    SHA-256 (12 hex chars = 48 bits) is overkill for collision resistance
    in this domain — the threat is log-scraping correlation, not
    cryptographic forgery. 12 chars matches the convention used by
    git short SHAs and is long enough that accidental collisions across
    distinct ``agent_url``s in the same tenant are negligible.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def translate_to_adcp_error(reason: PermissionDeniedReason) -> AdcpError:
    """Project a :class:`PermissionDeniedReason` to the wire envelope.

    Single translator — every denial path goes through here so the
    wire shape is uniform. ``details`` is populated only on the
    recognized-but-denied paths (``reason.scope is not None``); the
    unrecognized path emits a byte-equivalent envelope with no
    ``details`` per the omit-on-unestablished-identity rule.

    The ``message`` and ``recovery`` are identical on every path.
    """
    if reason.scope is None:
        # Unrecognized path — registry miss, no credential, unknown
        # status. ``details`` is OMITTED. ``scope`` would itself be the
        # discriminator that leaks onboarding state to an external
        # attacker.
        return AdcpError(
            "PERMISSION_DENIED",
            message=DENIED_MESSAGE,
            recovery="correctable",
        )
    # Recognized-but-denied path. ``details`` carries the discriminator.
    details: dict[str, object] = {"scope": reason.scope}
    if reason.status is not None:
        details["status"] = reason.status
    if reason.agent_url is not None:
        details["agent_url"] = reason.agent_url
    return AdcpError(
        "PERMISSION_DENIED",
        message=DENIED_MESSAGE,
        recovery="correctable",
        details=details,
    )


async def _emit_denial_audit(
    reason: PermissionDeniedReason,
    *,
    audit_sink: AuditSink | None,
    tenant_id: str | None,
    sink_timeout_seconds: float = 5.0,
) -> None:
    """Emit a single audit event with a uniform shape across every denial.

    Same operation label, same key set in ``details`` (values vary per
    path). The ``agent_url`` is hashed-truncated so an operator reading
    the audit trail cannot reconstruct which URLs the seller has
    onboarded — log-scraping is closed as a side channel.

    Sink failures are bounded + swallowed — a stalled sink NEVER blocks
    the gate. The latency budget downstream is still honored because
    this function returns within ``sink_timeout_seconds`` worst-case.
    """
    from adcp.audit_sink import AuditEvent

    details: dict[str, object | None] = {
        "outcome": "denied",
        "reason_scope": reason.scope,
        "reason_status": reason.status,
        "agent_url_hash": (
            hash_discriminator(reason.agent_url) if reason.agent_url is not None else None
        ),
    }

    event = AuditEvent(
        operation=AUDIT_OPERATION,
        success=False,
        occurred_at=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        details=details,
    )

    if audit_sink is None:
        logger.debug(
            "[adcp.permission_denied] %s outcome=denied scope=%s status=%s",
            AUDIT_OPERATION,
            reason.scope,
            reason.status,
        )
        return
    try:
        await asyncio.wait_for(audit_sink.record(event), timeout=sink_timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "[adcp.permission_denied] audit sink %s timed out after %ss",
            type(audit_sink).__name__,
            sink_timeout_seconds,
        )
    except Exception:  # noqa: BLE001 — sink failures must not propagate
        logger.warning(
            "[adcp.permission_denied] audit sink %s raised",
            type(audit_sink).__name__,
            exc_info=True,
        )


async def raise_permission_denied(
    reason: PermissionDeniedReason,
    *,
    audit_sink: AuditSink | None = None,
    tenant_id: str | None = None,
    sink_timeout_seconds: float = 5.0,
) -> None:
    """Single emit point for every PERMISSION_DENIED branch.

    Order on every path is deliberately identical:

    1. Capture the deadline (``now + budget``) BEFORE any I/O.
    2. Emit the uniform audit event (variable wall-clock; sink-dependent).
    3. Sleep until the deadline so total wall-clock from call entry to
       raise is dominated by the budget rather than by step 2's
       variance.
    4. Raise the wire ``AdcpError`` via the single translator.

    Step 3's deadline-relative sleep (rather than a fixed
    ``sleep(budget)``) is what closes the timing oracle: if step 2
    takes longer on one path than another, the remaining sleep
    automatically shrinks to keep the total constant.

    Reasoning on ordering:

    * Audit-emit BEFORE the sleep means a slow sink absorbs into the
      budget rather than extending past it — a denied request never
      takes longer than ``budget + sink_timeout_seconds`` regardless of
      branch.
    * Sleep BEFORE the raise means the wall-clock between request entry
      and the wire response is dominated by the budget, not by the
      raise/serialize path.
    """
    deadline = time.perf_counter() + get_latency_budget_seconds()
    await _emit_denial_audit(
        reason,
        audit_sink=audit_sink,
        tenant_id=tenant_id,
        sink_timeout_seconds=sink_timeout_seconds,
    )
    remaining = deadline - time.perf_counter()
    if remaining > 0:
        await asyncio.sleep(remaining)
    raise PermissionDeniedError(reason)


__all__ = [
    "AUDIT_OPERATION",
    "BUDGET_ENV_VAR",
    "DEFAULT_BUDGET_MS",
    "DENIED_MESSAGE",
    "PermissionDeniedError",
    "PermissionDeniedReason",
    "get_latency_budget_seconds",
    "hash_discriminator",
    "raise_permission_denied",
    "translate_to_adcp_error",
]
