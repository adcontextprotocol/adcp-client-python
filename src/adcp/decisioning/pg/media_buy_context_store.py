"""PostgreSQL-backed :class:`~adcp.decisioning.MediaBuyContextStore`.

Durable counterpart to :class:`~adcp.decisioning.InMemoryMediaBuyContextStore`:
the ``context`` a ``create_media_buy`` request carried — buy-level and per
package — survives restarts and is shared by every worker, so
``get_media_buys`` echoes it wherever the read lands.

The caller supplies an :class:`psycopg_pool.AsyncConnectionPool`. We don't
open, own, or close the pool.

::

    from adcp.decisioning import create_media_buy_store
    from adcp.decisioning.pg import PgMediaBuyContextStore

    context_store = PgMediaBuyContextStore(pool=pool)
    await context_store.create_schema()  # idempotent; safe on every boot
    platform.media_buy_store = create_media_buy_store(
        capabilities=platform.capabilities, context_store=context_store
    )

Storage
-------

One row per echoed object: ``scope = 'media_buy'`` (with ``package_id = ''``)
for the buy-level ``context``, ``scope = 'package'`` for each package's. The
``context`` column is ``JSON``, not ``JSONB``: ``JSON`` keeps the text the SDK
wrote, so key order, duplicate-free spelling and number lexemes (``7.0``
stays ``7.0``) round-trip as sent, where ``JSONB`` would re-order keys and
normalize numbers. Rows are written once (``ON CONFLICT DO NOTHING``): the
create request's ``context`` is echoed unchanged for the life of the buy.

This is a new table. Nothing existing changes, so there is no data
migration; create it with :meth:`create_schema`, with the
:meth:`migration_sql` statements in your migration tool, or by applying
:file:`media_buy_context_store.sql` (default table name only).

Every query filters on ``account_id``; multi-tenant sellers encode tenant
scope into ``Account.id`` as for the other decisioning stores.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from adcp.decisioning.media_buy_store import MediaBuyContexts

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

try:
    from psycopg_pool import AsyncConnectionPool as _AsyncConnectionPool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

_INSTALL_HINT = (
    "PgMediaBuyContextStore requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'`."
)

DEFAULT_TABLE_NAME = "adcp_media_buy_contexts"

# The table name is static-formatted into SQL; this ASCII-only guard is the
# only protection against injection or homoglyph substitution.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_MEDIA_BUY_SCOPE = "media_buy"
_PACKAGE_SCOPE = "package"


def _checked_table(table_name: str) -> str:
    if not _SAFE_IDENTIFIER_RE.fullmatch(table_name):
        raise ValueError(
            f"table_name must match [a-z_][a-z0-9_]{{0,62}} (ASCII only), got {table_name!r}"
        )
    return table_name


def _encode(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, allow_nan=False)


def _ddl(table: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        f'    account_id    TEXT        COLLATE "C" NOT NULL,\n'
        f'    media_buy_id  TEXT        COLLATE "C" NOT NULL,\n'
        f"    scope         TEXT        NOT NULL CHECK (scope IN ('media_buy', 'package')),\n"
        f"    package_id    TEXT        COLLATE \"C\" NOT NULL DEFAULT '',\n"
        f"    context       JSON        NOT NULL,\n"
        f"    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
        f"    PRIMARY KEY (account_id, media_buy_id, scope, package_id),\n"
        f"    CHECK ((scope = 'media_buy') = (package_id = ''))\n"
        f")"
    )


class PgMediaBuyContextStore:
    """Durable :class:`~adcp.decisioning.MediaBuyContextStore` on PostgreSQL."""

    def __init__(self, *, pool: AsyncConnectionPool, table_name: str = DEFAULT_TABLE_NAME) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        self._pool = pool
        self._table = _checked_table(table_name)

    @classmethod
    def migration_sql(cls, *, table_name: str = DEFAULT_TABLE_NAME) -> dict[str, str]:
        """``{"upgrade": ..., "downgrade": ...}`` statements for a migration tool."""
        table = _checked_table(table_name)
        return {"upgrade": _ddl(table) + ";\n", "downgrade": f"DROP TABLE IF EXISTS {table};\n"}

    async def create_schema(self) -> None:
        """Create the table if it does not exist. Idempotent."""
        async with self._pool.connection() as conn:
            await conn.execute(_ddl(self._table))

    async def save_contexts(
        self, account_id: str, media_buy_id: str, contexts: MediaBuyContexts
    ) -> None:
        rows: list[tuple[str, str, str, str, str]] = []
        if contexts.context is not None:
            rows.append((account_id, media_buy_id, _MEDIA_BUY_SCOPE, "", _encode(contexts.context)))
        for package_id, context in contexts.package_contexts.items():
            rows.append((account_id, media_buy_id, _PACKAGE_SCOPE, package_id, _encode(context)))
        if not rows:
            return
        sql = (  # noqa: S608 — table name validated at construction
            f"INSERT INTO {self._table} "  # nosec B608
            f"(account_id, media_buy_id, scope, package_id, context) "
            f"VALUES (%s, %s, %s, %s, %s::json) "
            f"ON CONFLICT (account_id, media_buy_id, scope, package_id) DO NOTHING"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(sql, rows)

    async def load_contexts(
        self, account_id: str, media_buy_ids: Sequence[str]
    ) -> dict[str, MediaBuyContexts]:
        if not media_buy_ids:
            return {}
        sql = (  # noqa: S608 — table name validated at construction
            f"SELECT media_buy_id, scope, package_id, context::text FROM {self._table} "  # nosec B608
            f"WHERE account_id = %s AND media_buy_id = ANY(%s)"
        )
        buy_contexts: dict[str, dict[str, Any]] = {}
        package_contexts: dict[str, dict[str, dict[str, Any]]] = {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, (account_id, list(media_buy_ids)))
            for media_buy_id, scope, package_id, text in await cur.fetchall():
                context = json.loads(text)
                if scope == _MEDIA_BUY_SCOPE:
                    buy_contexts[media_buy_id] = context
                else:
                    package_contexts.setdefault(media_buy_id, {})[package_id] = context
        return {
            media_buy_id: MediaBuyContexts(
                context=buy_contexts.get(media_buy_id),
                package_contexts=package_contexts.get(media_buy_id, {}),
            )
            for media_buy_id in buy_contexts.keys() | package_contexts.keys()
        }
