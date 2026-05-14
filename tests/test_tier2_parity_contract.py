"""Tier 2 commercial-identity gate — latency / headers / side-effects
parity contract.

Follow-up to PR #375 (wire-code rename) per issue #392. The
:file:`test_tier2_spec_conformance.py` file pins the wire-shape
conformance for the four denial paths in
:func:`adcp.decisioning.handler._resolve_buyer_agent`; this file pins
the *parity* guarantees the cross-tenant onboarding-oracle clamp
requires:

* **Latency parity** — p99 difference between the unrecognized and
  recognized-but-suspended paths is bounded below the latency budget
  (50 ms default).
* **Status-code parity** — every denial path raises
  ``AdcpError("PERMISSION_DENIED", recovery="correctable")``.
* **Header parity** — wire envelopes serialize with the same
  ``Content-Type`` and ``Content-Length`` within a documented
  tolerance (the ``details`` payload's size variance is the
  legitimate, spec-allowed delta).
* **Audit / metric parity** — every denial emits one
  :class:`~adcp.audit_sink.AuditEvent` with the same operation label
  and the same key set in ``details``. The discriminator
  (``agent_url``) is hashed-truncated before reaching the sink so
  log-scraping cannot reconstruct the side channel.

See :mod:`adcp.decisioning.permission_denied` for the latency-budget
and header-parity tradeoff documentation.
"""

from __future__ import annotations

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.audit_sink import AuditEvent, AuditSink
from adcp.decisioning import (
    AdcpError,
    AuthInfo,
    BuyerAgent,
    DecisioningCapabilities,
    DecisioningPlatform,
    HttpSigCredential,
    InMemoryTaskRegistry,
    SingletonAccounts,
    signing_only_registry,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.permission_denied import (
    AUDIT_OPERATION,
    BUDGET_ENV_VAR,
    PermissionDeniedReason,
    hash_discriminator,
    translate_to_adcp_error,
)
from adcp.server.base import ToolContext

# ---------------------------------------------------------------------------
# Test fixtures + helpers
# ---------------------------------------------------------------------------


class _CollectingSink:
    """In-memory :class:`AuditSink` for parity assertions.

    Records every event verbatim so tests can assert per-path key set
    + value shape without an external dependency. NOT a Mock — calling
    :meth:`AuditSink.record` on a ``MagicMock`` swallows the real
    Pydantic validation, which is what catches schema regressions.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def collecting_sink() -> _CollectingSink:
    return _CollectingSink()


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tier2-parity-")
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture
def short_budget_ms(monkeypatch):
    """5 ms budget for fast-running tests that don't measure latency.

    The default 50 ms budget would make the 200-iteration latency
    test take ~10s. Tests that DO measure latency override this
    fixture explicitly to keep the budget at the production value
    (so they exercise the same code path adopters run).
    """
    monkeypatch.setenv(BUDGET_ENV_VAR, "5")
    yield 5.0


def _signed_auth_info(agent_url: str) -> AuthInfo:
    return AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig",
            keyid="kid-1",
            agent_url=agent_url,
            verified_at=1700000000.0,
        ),
    )


class _RejectingPlatform(DecisioningPlatform):
    """Test platform whose every method asserts it's not invoked.

    The Tier 2 gate runs BEFORE the platform method, so any platform
    method invocation here means the gate let the request through.
    """

    capabilities = DecisioningCapabilities()
    accounts = SingletonAccounts(account_id="acct-1")

    async def get_products(self, req, ctx):  # pragma: no cover - asserts not called
        raise AssertionError("Tier 2 gate should have rejected before this")


def _make_handler(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
    registry,
    *,
    audit_sink: AuditSink | None = None,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=registry,
        permission_denied_audit_sink=audit_sink,
    )


def _make_registry(agent: BuyerAgent | None):
    """Build a signing-only registry that returns ``agent`` for any URL."""

    async def lookup(_url: str) -> BuyerAgent | None:
        return agent

    return signing_only_registry(lookup)


async def _trigger_denial(
    *,
    executor: ThreadPoolExecutor,
    agent: BuyerAgent | None,
    audit_sink: AuditSink | None = None,
    auth: AuthInfo | None = None,
) -> AdcpError:
    """Drive one request through the gate and return the AdcpError raised."""
    from adcp.types import GetProductsRequest

    handler = _make_handler(
        _RejectingPlatform(),
        executor,
        _make_registry(agent),
        audit_sink=audit_sink,
    )
    ctx_metadata: dict[str, Any] = {}
    if auth is not None:
        ctx_metadata["adcp.auth_info"] = auth
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata=ctx_metadata),
        )
    return exc.value


# ---------------------------------------------------------------------------
# Translator parity — wire shape is uniform modulo `details`
# ---------------------------------------------------------------------------


def test_translator_unrecognized_omits_details() -> None:
    """``scope is None`` → ``details`` is OMITTED entirely (not ``{}``).

    Per the omit-on-unestablished-identity rule in the spec
    (``schemas/cache/error-details/agent-permission-denied.json``).
    """
    reason = PermissionDeniedReason(scope=None, status=None, agent_url=None)
    err = translate_to_adcp_error(reason)
    assert err.code == "PERMISSION_DENIED"
    assert err.recovery == "correctable"
    # ``details`` is the empty dict on the AdcpError instance (the
    # constructor normalizes None → {}). ``to_wire()`` then omits the
    # key entirely. That's the byte-equivalence guarantee.
    assert err.details == {}
    assert "details" not in err.to_wire()


def test_translator_unrecognized_drops_agent_url_even_when_known() -> None:
    """Defense in depth: even when the framework KNOWS an ``agent_url``
    (unknown-status branch), the unrecognized translator path drops it
    from the wire envelope. Echoing the URL would let an attacker
    confirm probe inputs even though ``scope`` is omitted.
    """
    reason = PermissionDeniedReason(scope=None, status=None, agent_url="https://known/")
    err = translate_to_adcp_error(reason)
    assert err.details == {}
    assert "details" not in err.to_wire()


def test_translator_recognized_includes_full_details() -> None:
    reason = PermissionDeniedReason(scope="agent", status="suspended", agent_url="https://s/")
    err = translate_to_adcp_error(reason)
    wire = err.to_wire()
    assert wire["code"] == "PERMISSION_DENIED"
    assert wire["recovery"] == "correctable"
    assert wire["details"] == {
        "scope": "agent",
        "status": "suspended",
        "agent_url": "https://s/",
    }


# ---------------------------------------------------------------------------
# Status code parity across all 4 denial paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_code_parity_across_four_paths(executor, short_budget_ms) -> None:
    """All four denial branches raise the same ``code`` + ``recovery``.

    Status-code parity is the most basic parity guarantee — an attacker
    who sees a distinct code on one path has the side channel. Verified
    here even though the spec-conformance file pins the code on each
    path individually; this asserts they're pairwise identical.
    """
    # Registry miss (unrecognized).
    err_miss = await _trigger_denial(
        executor=executor, agent=None, auth=_signed_auth_info("https://x/")
    )
    # No credential (unrecognized).
    err_no_cred = await _trigger_denial(executor=executor, agent=None, auth=None)
    # Suspended (recognized).
    err_susp = await _trigger_denial(
        executor=executor,
        agent=BuyerAgent(agent_url="https://s/", display_name="S", status="suspended"),
        auth=_signed_auth_info("https://s/"),
    )
    # Blocked (recognized).
    err_blocked = await _trigger_denial(
        executor=executor,
        agent=BuyerAgent(agent_url="https://b/", display_name="B", status="blocked"),
        auth=_signed_auth_info("https://b/"),
    )

    errs = [err_miss, err_no_cred, err_susp, err_blocked]
    codes = {e.code for e in errs}
    recoveries = {e.recovery for e in errs}
    assert codes == {"PERMISSION_DENIED"}
    assert recoveries == {"correctable"}
    # Same `message` across paths — the message itself MUST NOT vary.
    messages = {str(e.args[0]) for e in errs}
    assert len(messages) == 1, f"message variance leaks branch identity: {messages}"


# ---------------------------------------------------------------------------
# Audit / metric parity — same op label, same key set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_emit_same_operation_label_across_paths(
    executor, collecting_sink, short_budget_ms
) -> None:
    """Every denial branch emits exactly one event with the same
    ``operation`` label. Operation-label variance would let log-
    scraping reconstruct the branch.
    """
    paths = [
        # (agent, auth) for each of the 4 denial branches.
        (None, _signed_auth_info("https://x/")),  # registry miss
        (None, None),  # no credential
        (
            BuyerAgent(agent_url="https://s/", display_name="S", status="suspended"),
            _signed_auth_info("https://s/"),
        ),
        (
            BuyerAgent(agent_url="https://b/", display_name="B", status="blocked"),
            _signed_auth_info("https://b/"),
        ),
    ]
    for agent, auth in paths:
        await _trigger_denial(executor=executor, agent=agent, audit_sink=collecting_sink, auth=auth)

    assert len(collecting_sink.events) == 4
    labels = {e.operation for e in collecting_sink.events}
    assert labels == {AUDIT_OPERATION}
    # Every event is a `success=False` row.
    assert all(not e.success for e in collecting_sink.events)


@pytest.mark.asyncio
async def test_audit_emit_uniform_details_key_set(
    executor, collecting_sink, short_budget_ms
) -> None:
    """The audit row's ``details`` key set is uniform across paths.

    Key-set variance would let log-scraping detect the branch via
    ``"reason_status" in details`` even with hashed discriminators.
    Values may vary; the key set may not.
    """
    paths = [
        (None, _signed_auth_info("https://x/")),
        (None, None),
        (
            BuyerAgent(agent_url="https://s/", display_name="S", status="suspended"),
            _signed_auth_info("https://s/"),
        ),
        (
            BuyerAgent(agent_url="https://b/", display_name="B", status="blocked"),
            _signed_auth_info("https://b/"),
        ),
    ]
    for agent, auth in paths:
        await _trigger_denial(executor=executor, agent=agent, audit_sink=collecting_sink, auth=auth)

    key_sets = {frozenset(e.details.keys()) for e in collecting_sink.events}
    assert len(key_sets) == 1, f"key-set variance across paths: {key_sets}"
    # Pin the expected shape — a future schema change shouldn't slip
    # in unnoticed.
    expected = frozenset({"outcome", "reason_scope", "reason_status", "agent_url_hash"})
    assert next(iter(key_sets)) == expected


@pytest.mark.asyncio
async def test_audit_emit_agent_url_is_hashed_not_plaintext(
    executor, collecting_sink, short_budget_ms
) -> None:
    """``agent_url_hash`` is the SHA-256 prefix, never the URL itself.

    Log-scraping defense — an operator with audit-read access can
    correlate denials by hash without learning the URL.
    """
    agent_url = "https://buyer.example.com/agent"
    suspended = BuyerAgent(agent_url=agent_url, display_name="S", status="suspended")

    await _trigger_denial(
        executor=executor,
        agent=suspended,
        audit_sink=collecting_sink,
        auth=_signed_auth_info(agent_url),
    )

    [event] = collecting_sink.events
    hashed = event.details["agent_url_hash"]
    assert hashed == hash_discriminator(agent_url)
    assert agent_url not in str(event.model_dump_json())


@pytest.mark.asyncio
async def test_audit_emit_unrecognized_carries_none_hash_not_missing_key(
    executor, collecting_sink, short_budget_ms
) -> None:
    """On the unrecognized path the hash is ``None``, but the KEY is
    still present. Key presence (not value) is the parity invariant —
    a missing key would be a presence-discriminator.
    """
    await _trigger_denial(
        executor=executor,
        agent=None,
        audit_sink=collecting_sink,
        auth=_signed_auth_info("https://x/"),
    )
    [event] = collecting_sink.events
    assert "agent_url_hash" in event.details
    assert event.details["agent_url_hash"] is None
    assert event.details["reason_scope"] is None
    assert event.details["reason_status"] is None


# ---------------------------------------------------------------------------
# Header parity — Content-Type identical; Content-Length within tolerance
# ---------------------------------------------------------------------------


def _serialize_wire(err: AdcpError) -> bytes:
    """Serialize an AdcpError envelope the way the transport layer would.

    JSON with no whitespace — matches the canonical wire form used by
    both MCP and A2A transports. The Content-Length of this bytestream
    is what an external attacker observes.
    """
    return json.dumps(err.to_wire(), separators=(",", ":")).encode("utf-8")


@pytest.mark.asyncio
async def test_header_content_length_within_tolerance(executor, short_budget_ms) -> None:
    """Content-Length variance between unrecognized and recognized is
    bounded by the size of the ``details`` payload.

    Justification for the tolerance: the recognized-but-denied
    envelope adds ``{"scope":"agent","status":"suspended","agent_url":
    "<url>"}`` to ``details``. With a typical ``agent_url`` (~30
    bytes) and the two fixed fields (~40 bytes), the recognized
    envelope is ~80 bytes longer than the unrecognized envelope. We
    pin tolerance at 200 bytes to absorb realistic ``agent_url``
    variance up to ~150 bytes, while still detecting an accidental
    huge-payload regression (e.g., echoing the full request).
    """
    err_unrecognized = await _trigger_denial(
        executor=executor, agent=None, auth=_signed_auth_info("https://x/")
    )
    err_suspended = await _trigger_denial(
        executor=executor,
        agent=BuyerAgent(agent_url="https://s/", display_name="S", status="suspended"),
        auth=_signed_auth_info("https://s/"),
    )
    err_blocked = await _trigger_denial(
        executor=executor,
        agent=BuyerAgent(agent_url="https://b/", display_name="B", status="blocked"),
        auth=_signed_auth_info("https://b/"),
    )

    lengths = [
        len(_serialize_wire(err_unrecognized)),
        len(_serialize_wire(err_suspended)),
        len(_serialize_wire(err_blocked)),
    ]
    spread = max(lengths) - min(lengths)
    # Tolerance documented in the function docstring above.
    assert spread < 200, (
        f"Content-Length spread {spread} exceeds 200-byte tolerance: "
        f"lengths={lengths}. A spread larger than the documented "
        f"tolerance suggests a regression — e.g., the unrecognized "
        f"path started leaking a discriminator into `details`, or a "
        f"recognized path is echoing buyer-supplied free text."
    )


@pytest.mark.asyncio
async def test_unrecognized_envelopes_are_byte_equivalent(executor, short_budget_ms) -> None:
    """Two unrecognized paths (registry miss, no credential) MUST
    serialize to byte-identical wire envelopes.

    The intra-unrecognized parity is the strongest form of the
    omit-on-unestablished-identity rule: an attacker who probes both
    code paths sees indistinguishable responses.
    """
    err_miss = await _trigger_denial(
        executor=executor, agent=None, auth=_signed_auth_info("https://x/")
    )
    err_no_cred = await _trigger_denial(executor=executor, agent=None, auth=None)
    assert _serialize_wire(err_miss) == _serialize_wire(err_no_cred)


# ---------------------------------------------------------------------------
# Latency parity — p99(unrecognized) ~ p99(recognized) under the budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latency_parity_p99_difference_under_budget(monkeypatch) -> None:
    """p99 difference between unrecognized and recognized-suspended
    paths < 5 ms over 200 iterations.

    Measures :func:`raise_permission_denied` directly rather than
    routing through the full handler stack. The parity contract is
    a property of ``raise_permission_denied`` — its deadline-relative
    sleep is what dominates branch variance. Wrapping handler setup
    into the measurement adds 5-15 ms of per-iteration jitter from
    ``PlatformHandler`` construction (``inspect.signature``,
    middleware composition) that varies with GC scheduling, not
    with the denial branch. Including it would measure noise.

    The handler-level integration is already covered by:

    * :func:`test_status_code_parity_across_four_paths` — same code
      and recovery across the four handler-routed branches.
    * :func:`test_audit_emit_same_operation_label_across_paths` —
      same audit row from the same handler path.

    So this test isolates the latency-budget guarantee:
    ``raise_permission_denied`` with the same budget produces
    indistinguishable wall-clock regardless of ``reason.scope`` /
    ``reason.status`` / ``reason.agent_url``.

    Budget choice: 50 ms — matches the production default. With
    ``raise_permission_denied`` isolated from handler overhead, this
    budget dominates branch variance even on CI hardware.

    Iteration count: 200 per arm. The 198th-percentile sample is the
    p99 quantile for n=200, which is robust to a single outlier.
    Total wall-clock: 200 × 2 × 50 ms = 20 s.
    """
    from adcp.decisioning.permission_denied import (
        PermissionDeniedError,
        raise_permission_denied,
    )

    monkeypatch.setenv(BUDGET_ENV_VAR, "50")

    async def measure(reason: PermissionDeniedReason) -> list[float]:
        latencies: list[float] = []
        for _ in range(200):
            start = time.perf_counter()
            try:
                await raise_permission_denied(reason)
            except PermissionDeniedError:
                pass
            latencies.append(time.perf_counter() - start)
        return latencies

    unrecognized_reason = PermissionDeniedReason(scope=None, status=None, agent_url=None)
    suspended_reason = PermissionDeniedReason(
        scope="agent", status="suspended", agent_url="https://s/"
    )
    unrecognized_lat = await measure(unrecognized_reason)
    suspended_lat = await measure(suspended_reason)

    p99_unrecognized = statistics.quantiles(unrecognized_lat, n=100)[98]
    p99_suspended = statistics.quantiles(suspended_lat, n=100)[98]
    diff_ms = abs(p99_unrecognized - p99_suspended) * 1000.0

    # 5 ms tolerance against a 50 ms budget — the budget dominates
    # by 10×. A regression that re-introduces branch variance (e.g.,
    # a future change that emits audit only on recognized paths,
    # or skips the budget sleep on the unrecognized path) would
    # show up as a 5+ ms diff.
    assert diff_ms < 5.0, (
        f"p99 latency parity violated: |unrecognized - suspended| = "
        f"{diff_ms:.2f} ms (budget=50 ms, tolerance=5 ms). "
        f"unrecognized_p99={p99_unrecognized * 1000:.2f} ms, "
        f"suspended_p99={p99_suspended * 1000:.2f} ms"
    )


# ---------------------------------------------------------------------------
# Configuration knob — budget is env-tunable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_env_var_takes_effect_at_runtime(executor, monkeypatch) -> None:
    """``ADCP_PERMISSION_DENIED_BUDGET_MS`` is read on every denial.

    Adopters tuning the budget during an incident response (e.g.,
    raising it because audit sink p99 went up) should NOT need to
    restart the process. The translator function is the unit under
    test rather than ``_resolve_buyer_agent`` so we avoid measuring
    framework setup overhead in the assertion.
    """
    from adcp.decisioning.permission_denied import get_latency_budget_seconds

    monkeypatch.setenv(BUDGET_ENV_VAR, "10")
    assert get_latency_budget_seconds() == pytest.approx(0.010, rel=0.001)
    monkeypatch.setenv(BUDGET_ENV_VAR, "100")
    assert get_latency_budget_seconds() == pytest.approx(0.100, rel=0.001)


def test_budget_env_var_falls_back_on_malformed_value(monkeypatch) -> None:
    """A malformed env var falls back to the default — the parity
    contract is preserved even when an adopter typos the config.
    """
    from adcp.decisioning.permission_denied import (
        DEFAULT_BUDGET_MS,
        get_latency_budget_seconds,
    )

    monkeypatch.setenv(BUDGET_ENV_VAR, "not-a-number")
    assert get_latency_budget_seconds() == pytest.approx(DEFAULT_BUDGET_MS / 1000.0)

    monkeypatch.setenv(BUDGET_ENV_VAR, "-10")
    assert get_latency_budget_seconds() == pytest.approx(DEFAULT_BUDGET_MS / 1000.0)


def test_budget_env_var_unset_uses_default(monkeypatch) -> None:
    from adcp.decisioning.permission_denied import (
        DEFAULT_BUDGET_MS,
        get_latency_budget_seconds,
    )

    monkeypatch.delenv(BUDGET_ENV_VAR, raising=False)
    assert get_latency_budget_seconds() == pytest.approx(DEFAULT_BUDGET_MS / 1000.0)


# ---------------------------------------------------------------------------
# Audit sink failure isolation — sink raises don't break the gate
# ---------------------------------------------------------------------------


class _RaisingSink:
    """An :class:`AuditSink` that raises — simulates a broken audit
    pipeline (DB connection error, schema mismatch).
    """

    def __init__(self) -> None:
        self.record_calls = 0

    async def record(self, event: AuditEvent) -> None:
        self.record_calls += 1
        raise RuntimeError("audit sink intentionally broken for test")


@pytest.mark.asyncio
async def test_raising_sink_does_not_propagate(executor, short_budget_ms) -> None:
    """A broken audit sink must not break the gate.

    Sink failures are swallowed inside ``_emit_denial_audit``. The
    gate continues to its budget sleep and raises ``PERMISSION_DENIED``
    as if the sink had succeeded.  This is the failure-isolation
    contract: an attacker who can break the audit pipeline MUST NOT
    be able to bypass the commercial-identity gate or distort its
    timing.
    """
    sink = _RaisingSink()
    err = await _trigger_denial(
        executor=executor,
        agent=None,
        audit_sink=sink,
        auth=_signed_auth_info("https://x/"),
    )
    assert err.code == "PERMISSION_DENIED"
    assert sink.record_calls == 1, "sink was supposed to be called once per denial"
