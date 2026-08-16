"""PostgreSQL-backed :class:`~adcp.signing.ReplayStore` implementation.

Gives multi-instance AdCP verifiers a shared nonce-seen store so a
replay accepted on worker A can't land again on worker B within the
signature's validity window.

The caller supplies a :class:`psycopg_pool.ConnectionPool`. We don't
open, own, or close the pool — integrators typically already have one
for their main database and sharing is cleaner than a second pool.

End-to-end example
------------------

::

    from psycopg_pool import ConnectionPool
    from adcp.signing import (
        PgReplayStore,
        StaticJwksResolver,
        VerifierCapability,
        VerifyOptions,
        verify_request_signature,
    )

    pool = ConnectionPool("postgresql://...", min_size=4, max_size=20)
    replay = PgReplayStore(pool=pool)
    replay.create_schema()  # bootstrap once per deployment; idempotent

    options = VerifyOptions(
        now=...,
        capability=VerifierCapability(required_for=frozenset({"create_media_buy"})),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [...]}),
        replay_store=replay,  # <-- plug in here
    )
    signer = verify_request_signature(
        method="POST", url=..., headers=..., body=..., options=options,
    )

REQUIRED: sweep job
-------------------

:meth:`seen` self-filters via ``expires_at > now()``, so lookups never
return stale entries. Rows accumulate, though — you MUST run a
periodic sweep or the table grows unbounded. Two options:

1. **pg_cron** (or any out-of-process scheduler)::

     DELETE FROM adcp_replay WHERE expires_at <= now();

2. **In-process loop** — call :meth:`sweep_expired` on a timer::

     async def sweep_forever(store: PgReplayStore, interval: float = 60.0) -> None:
         while True:
             store.sweep_expired()
             await asyncio.sleep(interval)

Pick one. An instance without a sweep is a memory leak waiting to
page your oncall.

Failure mode
------------

Transport or connection errors propagate from psycopg unchanged
(``OperationalError``, ``PoolTimeout``, etc.). The current verifier
does not catch them — so a pool hiccup raises out of
:func:`~adcp.signing.verify_request_signature`, and the enclosing
framework returns a 5xx. That's fail-closed from the client's
perspective (no 2xx on a broken store), but it's the framework's
default, not a SignatureVerificationError the caller can cleanly
handle. If your handler wraps verifier calls in a
``except Exception: return 503``, you're good; if it only catches
``SignatureVerificationError``, a broken store bubbles up as an
uncaught exception.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from adcp.signing.replay import ReplayClaimResult

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False


DEFAULT_TABLE_NAME = "adcp_replay"

# Byte-level ASCII identifier match. ``str.islower()`` / ``str.isalpha()``
# return True for non-ASCII Unicode letters (``é``, fullwidth Latin
# ``ｔ``, ``µ``, ``ß`` etc.) — which would then format verbatim into SQL
# as a DIFFERENT table from the one the operator thinks they configured.
# Under multi-tenant config where ``table_name`` can be attacker-
# influenced, that's a real replay-bypass vector. The regex here is
# ASCII-range-by-construction.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_INSTALL_HINT = (
    "PgReplayStore requires psycopg3 and psycopg-pool. Install the 'pg' "
    "extra: `pip install 'adcp[pg]'` (Poetry: `poetry add 'adcp[pg]'`)."
)


class PgReplayStore:
    """PostgreSQL-backed replay store implementing :class:`ReplayStore`.

    Parameters
    ----------
    pool:
        A :class:`psycopg_pool.ConnectionPool` owned by the caller. Each
        operation acquires a short-lived connection and returns it promptly.
        ``claim`` runs its capacity check and insert in one short transaction.
    per_keyid_cap:
        Maximum number of live (non-expired) nonces per ``keyid``.
        Mirrors :class:`InMemoryReplayStore`; spec-recommended 1M.
        When :meth:`at_capacity` reports True, the verifier rejects
        with ``request_signature_rate_abuse`` rather than silently
        evicting older entries (which would create a replay window
        under attack).
    table_name:
        Override the default ``adcp_replay`` table if two tenants share
        a database and need separate replay stores. Must be a
        byte-equal-clean identifier — we don't quote it into the SQL
        dynamically for obvious injection reasons; the constructor
        validates shape.

    Concurrency
    -----------

    Safe to share across threads and processes. Postgres provides the
    cross-instance locking we need via PK conflict resolution on
    ``INSERT ... ON CONFLICT``.
    """

    def __init__(
        self,
        *,
        pool: ConnectionPool,
        per_keyid_cap: int = 1_000_000,
        table_name: str = DEFAULT_TABLE_NAME,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        if not _is_safe_identifier(table_name):
            raise ValueError(
                f"table_name must match [a-z_][a-z0-9_]* (ASCII only), got {table_name!r}"
            )
        self._pool = pool
        self._per_keyid_cap = per_keyid_cap
        self._table = table_name

        # Pre-format queries with the validated table name so the hot
        # path doesn't f-string per call.
        self._sql_seen = (
            f"SELECT 1 FROM {self._table} "  # noqa: S608 — table name is whitelisted
            f"WHERE keyid = %s AND nonce = %s AND expires_at > now()"
        )
        # ``WHERE EXCLUDED.expires_at > {table}.expires_at`` avoids write
        # amplification on the common case (a row is already present
        # with a later-or-equal expiry). Without the predicate, every
        # remember() would re-write the MVCC tuple even when the new
        # TTL is shorter or equal.
        self._sql_remember = (
            f"INSERT INTO {self._table} (keyid, nonce, expires_at) "  # noqa: S608
            f"VALUES (%s, %s, now() + make_interval(secs => %s)) "
            f"ON CONFLICT (keyid, nonce) DO UPDATE "
            f"SET expires_at = EXCLUDED.expires_at "
            f"WHERE EXCLUDED.expires_at > {self._table}.expires_at"
        )
        self._sql_at_capacity = (
            f"SELECT COUNT(*) >= %s FROM {self._table} "  # noqa: S608
            f"WHERE keyid = %s AND expires_at > now()"
        )
        self._sql_sweep = f"DELETE FROM {self._table} WHERE expires_at <= now()"  # noqa: S608
        self._sql_claim_lock = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 9173))"
        self._sql_claim = (
            f"INSERT INTO {self._table} (keyid, nonce, expires_at) "  # noqa: S608
            f"VALUES (%s, %s, now() + make_interval(secs => %s)) "
            f"ON CONFLICT (keyid, nonce) DO UPDATE "
            f"SET expires_at = EXCLUDED.expires_at "
            f"WHERE {self._table}.expires_at <= now() RETURNING 1"
        )

    # -- schema bootstrap --------------------------------------------

    def create_schema(self) -> None:
        """Create the replay table + indexes for this store's ``table_name``.

        Honors the ``table_name`` kwarg the store was constructed with —
        integrators using per-tenant tables get the right DDL without
        extra plumbing. Idempotent via ``CREATE ... IF NOT EXISTS``;
        safe to call on every app boot.

        The equivalent raw DDL ships at
        :file:`src/adcp/signing/pg/replay_store.sql` for integrators
        using a migration tool (Alembic, Flyway, psql) — that file
        uses the canonical ``adcp_replay`` name.
        """
        table = self._table  # already validated at __init__
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {table} ("  # noqa: S608 — validated
            f'    keyid      TEXT        COLLATE "C" NOT NULL,'
            f'    nonce      TEXT        COLLATE "C" NOT NULL,'
            f"    expires_at TIMESTAMPTZ NOT NULL,"
            f"    PRIMARY KEY (keyid, nonce)"
            f");"
            f"CREATE INDEX IF NOT EXISTS {table}_expires_idx "  # noqa: S608
            f"    ON {table} (expires_at);"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl)

    # -- ReplayStore Protocol -----------------------------------------

    def seen(self, keyid: str, nonce: str) -> bool:
        """Return True iff ``(keyid, nonce)`` has a live entry."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_seen, (keyid, nonce))
            return cur.fetchone() is not None

    def remember(self, keyid: str, nonce: str, ttl_seconds: float) -> bool:
        """Record ``(keyid, nonce)`` with a TTL.

        ``ON CONFLICT ... DO UPDATE`` refreshes the expiry on a
        legitimate retry of the same nonce in-window — matches
        :class:`InMemoryReplayStore` behavior.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_remember, (keyid, nonce, ttl_seconds))
        return True

    def at_capacity(self, keyid: str) -> bool:
        """Return True iff the live row count for ``keyid`` meets the cap.

        Implementation note: ``COUNT(*) >= cap`` uses the PK for the
        keyid filter and the expires index for the time predicate.
        For the spec-recommended 1M cap, the expensive case is exactly
        when a signer is misbehaving, so paying for accuracy is the
        right trade.

        For deployments that need faster short-circuiting on a hot
        keyid, an alternative shape is::

            SELECT 1 FROM {table}
             WHERE keyid = %s AND expires_at > now()
             OFFSET %s LIMIT 1

        which stops scanning once ``cap+1`` rows are seen. Swap in if
        profiling identifies ``at_capacity`` as hot.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_at_capacity, (self._per_keyid_cap, keyid))
            row = cur.fetchone()
            return bool(row[0]) if row is not None else False

    def claim(self, keyid: str, nonce: str, ttl_seconds: float) -> ReplayClaimResult:
        """Atomically enforce the per-key cap and reserve a fresh nonce.

        A transaction-scoped advisory lock serializes claims for one key id
        across all verifier processes. The primary key then provides the
        exact nonce winner selection.
        """
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(self._sql_claim_lock, (keyid,))
            cur.execute(self._sql_seen, (keyid, nonce))
            if cur.fetchone() is not None:
                return "replayed"
            cur.execute(self._sql_at_capacity, (self._per_keyid_cap, keyid))
            row = cur.fetchone()
            if row is not None and bool(row[0]):
                return "capacity"
            cur.execute(self._sql_claim, (keyid, nonce, ttl_seconds))
            return "claimed" if cur.fetchone() is not None else "replayed"

    # -- admin / cron ------------------------------------------------

    def sweep_expired(self) -> int:
        """Delete all rows whose ``expires_at`` is in the past.

        Returns the number of rows removed. Safe to call concurrently
        with :meth:`seen` / :meth:`remember`.

        Call from a cron or admin endpoint. :meth:`seen` self-filters
        so expired rows never cause false positives, but they do
        accumulate and grow the table.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_sweep)
            return cur.rowcount or 0

    def live_count(self, keyid: str) -> int:
        """Return the number of live (non-expired) rows for ``keyid``.

        Mostly useful for tests, monitoring, and admin tooling. Not on
        the :class:`ReplayStore` Protocol — hit-path code should call
        :meth:`at_capacity` which short-circuits at the cap without
        materializing the count.
        """
        sql = (
            f"SELECT COUNT(*) FROM {self._table} "  # noqa: S608
            f"WHERE keyid = %s AND expires_at > now()"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (keyid,))
            row = cur.fetchone()
            return int(row[0]) if row is not None else 0


def _is_safe_identifier(name: str) -> bool:
    """Allow only byte-ASCII lowercase identifiers for the table-name kwarg.

    The table name is static-formatted into SQL at construction; this
    validator is the sole guard against injection OR silent table-name
    substitution via Unicode homoglyphs. Must stay ASCII-byte-exact
    (see :data:`_SAFE_IDENTIFIER_RE`).

    Postgres's NAMEDATALEN default caps identifiers at 63 bytes.
    """
    return _SAFE_IDENTIFIER_RE.fullmatch(name) is not None


__all__ = ["PG_AVAILABLE", "DEFAULT_TABLE_NAME", "PgReplayStore"]
