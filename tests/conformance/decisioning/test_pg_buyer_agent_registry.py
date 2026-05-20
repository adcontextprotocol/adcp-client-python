"""Conformance tests for :class:`adcp.decisioning.pg.PgBuyerAgentRegistry`.

Requires a real PostgreSQL instance. To run locally::

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=pg postgres:16
    export ADCP_PG_TEST_URL=postgresql://postgres:pg@localhost:5432/postgres
    pytest tests/conformance/decisioning/test_pg_buyer_agent_registry.py -v

The entire module skips when ``ADCP_PG_TEST_URL`` is unset, so the
default test matrix stays green without a database dependency. CI
runs this in the same Postgres-16 job as the PgReplayStore tests.

Each test runs in an isolated table (``test_adcp_buyer_agents_<random>``)
so parallel runs and rerun-after-crash don't collide.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Iterator

import pytest

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping PgBuyerAgentRegistry tests",
        allow_module_level=True,
    )

from adcp.audit_sink import AuditEvent  # noqa: E402
from adcp.decisioning import (  # noqa: E402
    ApiKeyCredential,
    BuyerAgent,
    BuyerAgentDefaultTerms,
    OAuthCredential,
)
from adcp.decisioning.pg import PgBuyerAgentRegistry  # noqa: E402
from adcp.decisioning.types import AdcpError  # noqa: E402


class CapturingAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def isolated_pool() -> Iterator[tuple[psycopg_pool.ConnectionPool, str]]:
    """Fresh pool + isolated table per test. Drops on teardown."""
    table = f"test_adcp_buyer_agents_{secrets.token_hex(6)}"
    with psycopg_pool.ConnectionPool(TEST_URL, min_size=2, max_size=8) as pool:
        registry = PgBuyerAgentRegistry(pool=pool, table_name=table)
        registry.create_schema()
        try:
            yield pool, table
        finally:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")


def _registry(fixture: tuple[psycopg_pool.ConnectionPool, str]) -> PgBuyerAgentRegistry:
    pool, table = fixture
    return PgBuyerAgentRegistry(pool=pool, table_name=table)


# ----- create_schema bootstrap -------------------------------------------


def test_create_schema_is_idempotent(isolated_pool) -> None:
    """``create_schema`` is safe to call multiple times — uses
    ``CREATE TABLE IF NOT EXISTS`` so a second call after the
    fixture's bootstrap is a no-op."""
    registry = _registry(isolated_pool)
    registry.create_schema()
    registry.create_schema()  # must not raise


# ----- resolve_by_agent_url ----------------------------------------------


def test_resolve_by_agent_url_returns_none_for_unknown(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    result = asyncio.run(registry.resolve_by_agent_url("https://unknown/"))
    assert result is None


def test_resolve_by_agent_url_returns_typed_buyer_agent(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    expected = BuyerAgent(
        agent_url="https://acme/",
        display_name="Acme",
        status="active",
        billing_capabilities=frozenset({"operator", "agent"}),
        default_account_terms=BuyerAgentDefaultTerms(
            rate_card="acme-2026",
            payment_terms="NET30",
        ),
        allowed_brands=frozenset({"example.com", "acme.example"}),
        ext={"internal_id": "tenant-42"},
    )
    registry.upsert(expected)

    result = asyncio.run(registry.resolve_by_agent_url("https://acme/"))
    assert result is not None
    assert result.agent_url == "https://acme/"
    assert result.display_name == "Acme"
    assert result.status == "active"
    assert result.billing_capabilities == frozenset({"operator", "agent"})
    assert result.default_account_terms is not None
    assert result.default_account_terms.rate_card == "acme-2026"
    assert result.default_account_terms.payment_terms == "NET30"
    assert result.allowed_brands == frozenset({"example.com", "acme.example"})
    assert result.ext == {"internal_id": "tenant-42"}


# ----- resolve_by_credential ---------------------------------------------


def test_resolve_by_api_key_credential(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    agent = BuyerAgent(
        agent_url="https://bearer-buyer/",
        display_name="Bearer Buyer",
        status="active",
    )
    registry.upsert(agent, api_key_id="bearer-key-1")

    result = asyncio.run(
        registry.resolve_by_credential(
            ApiKeyCredential(kind="api_key", key_id="bearer-key-1"),
        )
    )
    assert result is not None
    assert result.agent_url == "https://bearer-buyer/"


def test_resolve_by_oauth_credential_uses_client_id(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    agent = BuyerAgent(
        agent_url="https://oauth-buyer/",
        display_name="OAuth Buyer",
        status="active",
    )
    registry.upsert(agent, api_key_id="oauth-client-1")

    result = asyncio.run(
        registry.resolve_by_credential(
            OAuthCredential(kind="oauth", client_id="oauth-client-1"),
        )
    )
    assert result is not None
    assert result.agent_url == "https://oauth-buyer/"


def test_resolve_by_credential_returns_none_for_unknown_key(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    result = asyncio.run(
        registry.resolve_by_credential(
            ApiKeyCredential(kind="api_key", key_id="never-seeded"),
        )
    )
    assert result is None


# ----- upsert (admin path) -----------------------------------------------


def test_upsert_overwrites_existing_row(isolated_pool) -> None:
    """Re-upserting under the same agent_url updates the display_name,
    status, billing_capabilities, etc. — the framework uses this
    path to project admin-UI edits into the registry."""
    registry = _registry(isolated_pool)
    registry.upsert(
        BuyerAgent(
            agent_url="https://acme/",
            display_name="Acme (old)",
            status="active",
        )
    )
    registry.upsert(
        BuyerAgent(
            agent_url="https://acme/",
            display_name="Acme (renamed)",
            status="active",
            billing_capabilities=frozenset({"operator", "agent"}),
        )
    )

    result = asyncio.run(registry.resolve_by_agent_url("https://acme/"))
    assert result is not None
    assert result.display_name == "Acme (renamed)"
    assert result.billing_capabilities == frozenset({"operator", "agent"})


def test_upsert_rejects_invalid_status(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    with pytest.raises(ValueError, match="status"):
        registry.upsert(
            BuyerAgent(
                agent_url="https://x/",
                display_name="x",
                status="bogus",  # type: ignore[arg-type]
            )
        )


# ----- set_status (admin lifecycle) --------------------------------------


def test_set_status_suspends_agent(isolated_pool) -> None:
    """Admin path: flipping status to ``suspended`` cuts the agent
    off on the next request — no cache invalidation, no restart.
    The framework reads status fresh on every dispatch."""
    registry = _registry(isolated_pool)
    registry.upsert(
        BuyerAgent(
            agent_url="https://acme/",
            display_name="Acme",
            status="active",
        )
    )
    registry.set_status("https://acme/", "suspended")

    result = asyncio.run(registry.resolve_by_agent_url("https://acme/"))
    assert result is not None
    assert result.status == "suspended"


def test_set_status_rejects_invalid_status(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    with pytest.raises(ValueError, match="status"):
        registry.set_status("https://acme/", "bogus")  # type: ignore[arg-type]


# ----- delete -------------------------------------------------------------


def test_delete_removes_agent(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    registry.upsert(BuyerAgent(agent_url="https://x/", display_name="x", status="active"))
    registry.delete("https://x/")

    result = asyncio.run(registry.resolve_by_agent_url("https://x/"))
    assert result is None


# ----- security: table-name validation -----------------------------------


def test_constructor_rejects_unsafe_table_name(isolated_pool) -> None:
    """Attacker-influenced table_name with a SQL fragment must not
    format into the dynamic SQL. The constructor validates against
    the same regex the PgReplayStore uses."""
    pool, _ = isolated_pool
    with pytest.raises(ValueError, match="table_name"):
        PgBuyerAgentRegistry(pool=pool, table_name="dangerous; DROP TABLE foo--")


def test_constructor_rejects_unicode_homoglyph_table_name(isolated_pool) -> None:
    """Unicode homoglyphs (e.g. fullwidth Latin) format verbatim and
    would silently address a different table than the operator
    intended. Reject."""
    pool, _ = isolated_pool
    with pytest.raises(ValueError, match="table_name"):
        # Fullwidth Latin "table" — looks like ASCII to a reader,
        # different bytes to Postgres.
        PgBuyerAgentRegistry(pool=pool, table_name="ｔａｂｌｅ")


# ----- defaults / edge cases ---------------------------------------------


def test_default_billing_capabilities_is_operator_only(isolated_pool) -> None:
    """Pre-trust beta default: agents seeded with no explicit
    capabilities project to passthrough-only."""
    registry = _registry(isolated_pool)
    registry.upsert(
        BuyerAgent(
            agent_url="https://x/",
            display_name="x",
            status="active",
            # billing_capabilities defaults to frozenset({"operator"}).
        )
    )
    result = asyncio.run(registry.resolve_by_agent_url("https://x/"))
    assert result is not None
    assert result.billing_capabilities == frozenset({"operator"})


def test_round_trip_with_no_optional_fields(isolated_pool) -> None:
    """Minimal seed (no terms, no allowed_brands, default ext) round
    trips without losing field presence."""
    registry = _registry(isolated_pool)
    registry.upsert(
        BuyerAgent(
            agent_url="https://minimal/",
            display_name="Minimal",
            status="active",
        )
    )
    result = asyncio.run(registry.resolve_by_agent_url("https://minimal/"))
    assert result is not None
    assert result.default_account_terms is None
    assert result.allowed_brands is None
    assert result.ext == {}


# ----- mutation observers + with_caching factory -------------------------


def test_add_mutation_observer_fires_on_upsert(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    calls: list[tuple[str, str]] = []
    registry.add_mutation_observer(lambda op, url: calls.append((op, url)))

    registry.upsert(
        BuyerAgent(
            agent_url="https://obs/",
            display_name="Observed",
            status="active",
        )
    )
    assert calls == [("upsert", "https://obs/")]


def test_add_mutation_observer_fires_on_set_status_and_delete(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    registry.upsert(
        BuyerAgent(
            agent_url="https://obs/",
            display_name="Observed",
            status="active",
        )
    )
    calls: list[tuple[str, str]] = []
    registry.add_mutation_observer(lambda op, url: calls.append((op, url)))

    registry.set_status("https://obs/", "suspended")
    registry.delete("https://obs/")

    assert calls == [("set_status", "https://obs/"), ("delete", "https://obs/")]


def test_observer_exception_does_not_block_mutation(isolated_pool) -> None:
    """An observer raising must not propagate to the mutation caller."""
    registry = _registry(isolated_pool)

    def boom(_op: str, _url: str) -> None:
        raise RuntimeError("observer raised")

    registry.add_mutation_observer(boom)
    # Mutation succeeds despite the observer raising.
    registry.upsert(
        BuyerAgent(
            agent_url="https://resilient/",
            display_name="Resilient",
            status="active",
        )
    )
    # And the row landed in the DB.
    result = asyncio.run(registry.resolve_by_agent_url("https://resilient/"))
    assert result is not None
    assert result.display_name == "Resilient"


def test_with_caching_returns_wired_cache(isolated_pool) -> None:
    """`pg.with_caching()` returns a cache that auto-invalidates on
    mutations through the same `pg` instance."""
    registry = _registry(isolated_pool)
    cache = registry.with_caching(ttl_seconds=60.0)

    registry.upsert(
        BuyerAgent(
            agent_url="https://wired/",
            display_name="Wired",
            status="active",
        )
    )
    # Warm cache via resolve.
    first = asyncio.run(cache.resolve_by_agent_url("https://wired/"))
    assert first is not None
    assert first.status == "active"

    # Mutate through pg — cache MUST auto-invalidate.
    registry.set_status("https://wired/", "suspended")

    second = asyncio.run(cache.resolve_by_agent_url("https://wired/"))
    assert second is not None
    assert second.status == "suspended", (
        "Cache served stale 'active' after pg.set_status — with_caching "
        "observer did not fire or did not invalidate"
    )


def test_with_full_stack_wires_cache_invalidation_and_audit(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    sink = CapturingAuditSink()
    stack = registry.with_full_stack(
        ttl_seconds=60.0,
        rps_per_tenant=1000.0,
        audit_sink=sink,
    )

    registry.upsert(
        BuyerAgent(
            agent_url="https://full-stack/",
            display_name="Full Stack",
            status="active",
        )
    )

    first = asyncio.run(stack.resolve_by_agent_url("https://full-stack/"))
    second = asyncio.run(stack.resolve_by_agent_url("https://full-stack/"))
    assert first is not None
    assert second is not None
    assert first.status == "active"
    assert second.status == "active"

    registry.set_status("https://full-stack/", "suspended")
    after_mutation = asyncio.run(stack.resolve_by_agent_url("https://full-stack/"))
    assert after_mutation is not None
    assert after_mutation.status == "suspended"

    outcomes = [event.details["outcome"] for event in sink.events]
    assert outcomes.count("resolved") == 2
    assert outcomes.count("cached_hit") == 1


def test_with_full_stack_rate_limit_fires_and_audits(isolated_pool) -> None:
    registry = _registry(isolated_pool)
    sink = CapturingAuditSink()
    clock = FakeClock()
    stack = registry.with_full_stack(
        ttl_seconds=0.1,
        rps_per_tenant=1.0,
        burst=1.0,
        audit_sink=sink,
        time_source=clock,
    )

    assert asyncio.run(stack.resolve_by_agent_url("https://rate-limited/")) is None
    clock.advance(0.2)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(stack.resolve_by_agent_url("https://rate-limited/"))

    assert exc_info.value.code == "PERMISSION_DENIED"
    outcomes = [event.details["outcome"] for event in sink.events]
    assert outcomes.count("miss") == 1
    assert outcomes.count("rate_limited") == 1
