"""Tests for :class:`adcp.signing.pg.PgReplayStore`.

Requires a real PostgreSQL instance. To run locally::

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=pg postgres:16
    export ADCP_PG_TEST_URL=postgresql://postgres:pg@localhost:5432/postgres
    pytest tests/conformance/signing/test_pg_replay_store.py -v

The entire module skips when ``ADCP_PG_TEST_URL`` is unset, so the
default test matrix stays green without a database dependency.

Each test runs in an isolated schema (``test_adcp_replay_<random>``)
so parallel test runs and rerun-after-crash scenarios don't collide.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections.abc import Iterator

import pytest

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping PgReplayStore tests",
        allow_module_level=True,
    )

from adcp.signing import AtomicReplayStore  # noqa: E402
from adcp.signing.pg import PgReplayStore  # noqa: E402


@pytest.fixture()
def isolated_pool() -> Iterator[psycopg_pool.ConnectionPool]:
    """Connection pool against a per-test schema + table.

    Creates a unique table name so tests running in parallel (or a
    crashed-then-retry run) don't step on each other. Drops the table
    on teardown.
    """
    table = f"test_adcp_replay_{secrets.token_hex(6)}"
    with psycopg_pool.ConnectionPool(TEST_URL, min_size=2, max_size=8) as pool:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE {table} (
                    keyid      TEXT        COLLATE "C" NOT NULL,
                    nonce      TEXT        COLLATE "C" NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (keyid, nonce)
                )
                """
            )
            cur.execute(f"CREATE INDEX {table}_expires_idx ON {table} (expires_at)")
        try:
            yield pool, table  # type: ignore[misc]
        finally:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")


def _store(fixture, **overrides) -> PgReplayStore:
    pool, table = fixture
    return PgReplayStore(pool=pool, table_name=table, **overrides)


def test_satisfies_atomic_replay_store_protocol(isolated_pool) -> None:
    assert isinstance(_store(isolated_pool), AtomicReplayStore)


# -- Protocol happy path ----------------------------------------------


def test_seen_returns_false_for_unknown_nonce(isolated_pool) -> None:
    store = _store(isolated_pool)
    assert store.seen("k", "n") is False


def test_remember_then_seen_returns_true(isolated_pool) -> None:
    store = _store(isolated_pool)
    store.remember("k", "n", ttl_seconds=60)
    assert store.seen("k", "n") is True


def test_remember_different_nonce_isolated(isolated_pool) -> None:
    store = _store(isolated_pool)
    store.remember("k", "n1", ttl_seconds=60)
    assert store.seen("k", "n2") is False


def test_remember_different_keyid_isolated(isolated_pool) -> None:
    store = _store(isolated_pool)
    store.remember("k1", "n", ttl_seconds=60)
    assert store.seen("k2", "n") is False


# -- TTL semantics ----------------------------------------------------


def test_seen_returns_false_after_ttl_expiry(isolated_pool) -> None:
    store = _store(isolated_pool)
    store.remember("k", "n", ttl_seconds=1)
    time.sleep(1.2)
    assert store.seen("k", "n") is False


def test_remember_refreshes_ttl_on_repeat(isolated_pool) -> None:
    """ON CONFLICT DO UPDATE keeps the most recent TTL — mirrors InMemoryReplayStore."""
    store = _store(isolated_pool)
    store.remember("k", "n", ttl_seconds=1)
    # Refresh well before expiry with a longer TTL.
    store.remember("k", "n", ttl_seconds=60)
    time.sleep(1.2)
    # The second remember's 60s TTL wins — still seen.
    assert store.seen("k", "n") is True


# -- at_capacity ------------------------------------------------------


def test_at_capacity_false_when_empty(isolated_pool) -> None:
    store = _store(isolated_pool, per_keyid_cap=3)
    assert store.at_capacity("k") is False


def test_at_capacity_true_at_cap(isolated_pool) -> None:
    store = _store(isolated_pool, per_keyid_cap=3)
    for i in range(3):
        store.remember("k", f"n{i}", ttl_seconds=60)
    assert store.at_capacity("k") is True


def test_at_capacity_respects_ttl_expiry(isolated_pool) -> None:
    store = _store(isolated_pool, per_keyid_cap=3)
    for i in range(3):
        store.remember("k", f"n{i}", ttl_seconds=1)
    assert store.at_capacity("k") is True
    time.sleep(1.2)
    # All three rows expired → count drops back to zero.
    assert store.at_capacity("k") is False


def test_at_capacity_is_per_keyid(isolated_pool) -> None:
    store = _store(isolated_pool, per_keyid_cap=2)
    store.remember("k1", "a", ttl_seconds=60)
    store.remember("k1", "b", ttl_seconds=60)
    assert store.at_capacity("k1") is True
    assert store.at_capacity("k2") is False


# -- sweep_expired ---------------------------------------------------


def test_sweep_expired_removes_stale_rows(isolated_pool) -> None:
    store = _store(isolated_pool)
    store.remember("k", "old", ttl_seconds=1)
    store.remember("k", "fresh", ttl_seconds=60)
    time.sleep(1.2)

    removed = store.sweep_expired()
    assert removed == 1
    assert store.live_count("k") == 1
    assert store.seen("k", "fresh") is True


def test_sweep_expired_returns_zero_when_clean(isolated_pool) -> None:
    store = _store(isolated_pool)
    store.remember("k", "n", ttl_seconds=60)
    assert store.sweep_expired() == 0


# -- concurrency -----------------------------------------------------


def test_concurrent_claim_same_nonce_has_one_winner(isolated_pool) -> None:
    """Only one verifier process may claim a nonce."""
    store = _store(isolated_pool)
    errors: list[Exception] = []
    results: list[str] = []
    barrier = threading.Barrier(10)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(store.claim("k", "shared", ttl_seconds=60))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert results.count("claimed") == 1
    assert results.count("replayed") == 9
    assert store.seen("k", "shared") is True
    assert store.live_count("k") == 1


def test_concurrent_at_capacity_safe(isolated_pool) -> None:
    """at_capacity from many threads shouldn't throw; value should stabilize."""
    store = _store(isolated_pool, per_keyid_cap=5)
    for i in range(5):
        store.remember("k", f"n{i}", ttl_seconds=60)

    results: list[bool] = []

    def worker() -> None:
        results.append(store.at_capacity("k"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results)


# -- validation -----------------------------------------------------


def test_bad_table_name_rejected(isolated_pool) -> None:
    pool, _ = isolated_pool
    with pytest.raises(ValueError, match="table_name"):
        PgReplayStore(pool=pool, table_name="has-dash")
    with pytest.raises(ValueError, match="table_name"):
        PgReplayStore(pool=pool, table_name="1leading_digit")
    with pytest.raises(ValueError, match="table_name"):
        PgReplayStore(pool=pool, table_name="")


def test_non_ascii_table_name_rejected(isolated_pool) -> None:
    """Unicode letters that ``str.islower()`` / ``str.isalpha()`` accept
    MUST be rejected — otherwise a homoglyph like fullwidth ``ｔａｂｌｅ``
    formats into SQL as a DIFFERENT table than operators configured.
    """
    pool, _ = isolated_pool
    # Fullwidth Latin letters lowercase to themselves, not ASCII.
    with pytest.raises(ValueError, match="ASCII"):
        PgReplayStore(pool=pool, table_name="ｔａｂｌｅ")
    # Latin small letter with accents — passes .islower() but not ASCII.
    with pytest.raises(ValueError, match="ASCII"):
        PgReplayStore(pool=pool, table_name="café_replay")
    # Greek small letter mu — looks like "u" in some fonts.
    with pytest.raises(ValueError, match="ASCII"):
        PgReplayStore(pool=pool, table_name="µreplay")


def test_remember_twice_with_shorter_ttl_keeps_longer_expiry(isolated_pool) -> None:
    """Conditional ``DO UPDATE WHERE EXCLUDED.expires_at > current`` means
    a shorter-TTL repeat must NOT shrink the cached expiry.
    """
    store = _store(isolated_pool)
    store.remember("k", "n", ttl_seconds=60)
    # Shorter TTL should be a no-op on the row.
    store.remember("k", "n", ttl_seconds=1)
    time.sleep(1.2)
    # If the update had fired, the row would have expired by now.
    assert store.seen("k", "n") is True


def test_create_schema_honors_table_name(isolated_pool) -> None:
    """create_schema must create the table the store was built for —
    not a hardcoded name. Integrators using per-tenant tables would
    break silently if this regresses.
    """
    pool, _existing_table = isolated_pool
    custom_table = f"custom_{secrets.token_hex(4)}_replay"
    store = PgReplayStore(pool=pool, table_name=custom_table)
    try:
        store.create_schema()
        store.create_schema()  # idempotent — second call must not error
        # Actually use the store — proves the DDL + runtime queries
        # target the same table.
        store.remember("k", "n", ttl_seconds=60)
        assert store.seen("k", "n") is True
    finally:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {custom_table}")


def test_collation_prevents_case_collapse(isolated_pool) -> None:
    """With COLLATE "C", keyid "Key-A" and "key-a" are distinct slots.

    Would be a real problem on locales where default collation case-folds:
    a buyer with kid "Key-A" and an attacker with kid "key-a" would share
    a replay cache, opening cross-tenant nonce interference.
    """
    store = _store(isolated_pool)
    store.remember("Key-A", "n", ttl_seconds=60)
    # Same nonce, case-variant kid. With "C" collation these are distinct.
    assert store.seen("key-a", "n") is False
    # And at_capacity for the other case shouldn't see the first one either.
    assert store.live_count("key-a") == 0
    assert store.live_count("Key-A") == 1
