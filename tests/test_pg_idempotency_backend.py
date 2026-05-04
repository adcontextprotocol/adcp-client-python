"""Unit tests for :class:`adcp.server.idempotency.PgBackend` — mocked pool.

psycopg3 is an optional dependency (``adcp[pg]``); these tests mock the
pool and connection entirely so they pass without a real Postgres
instance. Conformance tests against a real database live at
``tests/conformance/decisioning/test_pg_idempotency_backend.py``.

Behaviour under test:

* Construction validation — invalid identifier rejected; missing pg deps
  raise ImportError.
* ``create_schema`` issues exactly one ``execute()`` per DDL statement
  (psycopg does not split on semicolons).
* ``get`` reads the row, parses JSONB, normalizes timestamp to epoch
  seconds, returns ``None`` on miss (filtered server-side by
  ``expires_at > now()``).
* ``put`` upserts with ``ON CONFLICT DO UPDATE``; serializes response
  via json.dumps; converts epoch to tz-aware datetime.
* ``delete_expired`` returns the rowcount.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp.server.idempotency import IdempotencyBackend
from adcp.server.idempotency.backends import (
    DEFAULT_IDEMPOTENCY_TABLE,
    CachedResponse,
    PgBackend,
)


@pytest.fixture(autouse=True)
def _patch_pg_available():
    """psycopg3 is an optional dep; patch the availability gate so the
    mock-pool tests run on CI nodes that don't install ``adcp[pg]``."""
    with patch("adcp.server.idempotency.backends._PG_AVAILABLE", True):
        yield


def _cursor(fetchone_value: Any = None, rowcount: int = 0) -> AsyncMock:
    cur = AsyncMock()
    cur.fetchone = AsyncMock(return_value=fetchone_value)
    cur.rowcount = rowcount
    return cur


def _make_conn(*cursors: AsyncMock) -> AsyncMock:
    """Async context-manager mock yielding sequential cursors per execute()."""
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.execute = AsyncMock(side_effect=list(cursors))
    return conn


def _make_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()

    @asynccontextmanager
    async def _connection():
        yield conn

    pool.connection = _connection
    return pool


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_table_name(self) -> None:
        pool = MagicMock()
        backend = PgBackend(pool=pool)
        assert backend._table == DEFAULT_IDEMPOTENCY_TABLE

    def test_custom_table_name_accepted(self) -> None:
        pool = MagicMock()
        backend = PgBackend(pool=pool, table_name="my_idem_cache")
        assert backend._table == "my_idem_cache"

    def test_invalid_identifier_rejected(self) -> None:
        pool = MagicMock()
        with pytest.raises(ValueError, match="Table name must match"):
            PgBackend(pool=pool, table_name="bad-name")

    def test_uppercase_identifier_rejected(self) -> None:
        with pytest.raises(ValueError, match="Table name must match"):
            PgBackend(pool=MagicMock(), table_name="MyTable")

    def test_satisfies_idempotency_backend_protocol(self) -> None:
        backend = PgBackend(pool=MagicMock())
        assert isinstance(backend, IdempotencyBackend)


# ---------------------------------------------------------------------------
# create_schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_schema_executes_create_table_and_index() -> None:
    """Two DDL statements: CREATE TABLE + CREATE INDEX. Each must be a
    separate ``execute()`` call (psycopg does not split on ``;``)."""
    conn = _make_conn(_cursor(), _cursor())
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool, table_name="adcp_idempotency")

    await backend.create_schema()

    assert conn.execute.call_count == 2
    table_stmt = conn.execute.call_args_list[0].args[0]
    index_stmt = conn.execute.call_args_list[1].args[0]
    assert "CREATE TABLE IF NOT EXISTS adcp_idempotency" in table_stmt
    assert 'COLLATE "C"' in table_stmt
    assert "PRIMARY KEY (scope_key, key)" in table_stmt
    assert "CREATE INDEX IF NOT EXISTS adcp_idempotency_expires_idx" in index_stmt


@pytest.mark.asyncio
async def test_create_schema_uses_custom_table_name() -> None:
    conn = _make_conn(_cursor(), _cursor())
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool, table_name="alt_idem")

    await backend.create_schema()
    assert "CREATE TABLE IF NOT EXISTS alt_idem" in conn.execute.call_args_list[0].args[0]
    assert (
        "CREATE INDEX IF NOT EXISTS alt_idem_expires_idx" in conn.execute.call_args_list[1].args[0]
    )


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_none_on_miss() -> None:
    conn = _make_conn(_cursor(fetchone_value=None))
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    assert await backend.get("scope-x", "key-y") is None
    sql = conn.execute.call_args.args[0]
    assert "expires_at > now()" in sql


@pytest.mark.asyncio
async def test_get_parses_dict_response() -> None:
    """psycopg returns dict for JSONB columns by default — pass through."""
    expires = datetime(2030, 1, 1, tzinfo=timezone.utc)
    conn = _make_conn(_cursor(fetchone_value=("hash-1", {"k": "v"}, expires)))
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    cached = await backend.get("scope-a", "key-1")
    assert cached is not None
    assert cached.payload_hash == "hash-1"
    assert cached.response == {"k": "v"}
    assert cached.expires_at_epoch == expires.timestamp()


@pytest.mark.asyncio
async def test_get_parses_json_string_response() -> None:
    """If the driver/casts return JSON as a string, parse it."""
    expires = datetime(2030, 1, 1, tzinfo=timezone.utc)
    conn = _make_conn(_cursor(fetchone_value=("hash-1", '{"k":"v"}', expires)))
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    cached = await backend.get("scope-a", "key-1")
    assert cached is not None
    assert cached.response == {"k": "v"}


@pytest.mark.asyncio
async def test_get_raises_on_naive_timestamp() -> None:
    """Naive datetime indicates schema drift (TIMESTAMP vs TIMESTAMPTZ).
    Fail-fast over silent UTC coercion — silent coercion would produce
    wrong replay windows when the server's local time is not UTC."""
    naive = datetime(2030, 1, 1)
    conn = _make_conn(_cursor(fetchone_value=("hash", {}, naive)))
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    with pytest.raises(ValueError, match="naive datetime"):
        await backend.get("scope", "key")


# ---------------------------------------------------------------------------
# put
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_upserts_with_on_conflict() -> None:
    conn = _make_conn(_cursor())
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    entry = CachedResponse(
        payload_hash="hash-1",
        response={"k": "v"},
        expires_at_epoch=time.time() + 3600,
    )
    await backend.put("scope-a", "key-1", entry)

    sql, params = conn.execute.call_args.args
    assert "INSERT INTO adcp_idempotency" in sql
    assert "ON CONFLICT (scope_key, key) DO UPDATE" in sql
    # First-writer-wins: the UPDATE only applies to actually-expired
    # rows. Concurrent put against a fresh slot is a no-op.
    assert "WHERE adcp_idempotency.expires_at <= now()" in sql
    assert "::jsonb" in sql
    scope_key, key, payload_hash, response_json, expires_at = params
    assert scope_key == "scope-a"
    assert key == "key-1"
    assert payload_hash == "hash-1"
    assert response_json == '{"k": "v"}'
    assert isinstance(expires_at, datetime)
    assert expires_at.tzinfo is not None  # tz-aware


# ---------------------------------------------------------------------------
# delete_expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_expired_uses_supplied_cutoff() -> None:
    conn = _make_conn(_cursor(rowcount=7))
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    cutoff_epoch = 1_000_000_000.0
    deleted = await backend.delete_expired(cutoff_epoch)
    assert deleted == 7

    sql, params = conn.execute.call_args.args
    assert "DELETE FROM adcp_idempotency" in sql
    assert "expires_at <= %s" in sql
    (cutoff_dt,) = params
    assert isinstance(cutoff_dt, datetime)
    assert cutoff_dt.timestamp() == cutoff_epoch


@pytest.mark.asyncio
async def test_delete_expired_defaults_to_wall_clock() -> None:
    conn = _make_conn(_cursor(rowcount=0))
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    before = time.time()
    deleted = await backend.delete_expired()
    after = time.time()

    assert deleted == 0
    (cutoff_dt,) = conn.execute.call_args.args[1]
    # Cutoff should be roughly "now" — within the test execution window.
    assert before <= cutoff_dt.timestamp() <= after


@pytest.mark.asyncio
async def test_delete_expired_returns_zero_on_no_rowcount() -> None:
    """``rowcount`` may be ``-1`` or ``None`` on some psycopg paths;
    backend coerces to 0."""
    conn = _make_conn(_cursor(rowcount=None))  # type: ignore[arg-type]
    pool = _make_pool(conn)
    backend = PgBackend(pool=pool)

    assert await backend.delete_expired() == 0
