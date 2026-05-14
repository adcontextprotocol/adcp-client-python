"""PostgreSQL-backed :class:`~adcp.decisioning.ProposalStore` implementation.

Durable counterpart to :class:`~adcp.decisioning.InMemoryProposalStore`:
proposal lifecycle state survives process restarts and is safe for
multi-worker deployments sharing a single Postgres database.

The caller supplies an :class:`psycopg_pool.AsyncConnectionPool`. We
don't open, own, or close the pool — adopters typically share an
existing pool with their main application database.

Quickstart
----------

::

    from psycopg_pool import AsyncConnectionPool
    from adcp.decisioning import PgProposalStore, serve
    from myapp.recipes import decode_recipe

    async with AsyncConnectionPool(
        "postgresql://...",
        min_size=2,
        max_size=10,
    ) as pool:
        store = PgProposalStore(pool=pool, recipe_decoder=decode_recipe)
        await store.create_schema()  # idempotent; safe on every boot
        ...

Recipe round-trip
-----------------

Recipes are stored as ``{product_id: model_dump(mode='json')}`` in a
JSONB column. On read, the constructor's ``recipe_decoder`` callable
rehydrates each entry back to a typed :class:`Recipe` subclass. The
default decoder is :meth:`Recipe.model_validate` — it works for
adopters using only the base ``Recipe`` (e.g. for capability_overlap)
but raises on subclass-specific fields because the base class declares
``extra='forbid'``.

Adopters with typed Recipe subclasses (``GAMRecipe``, ``KevelRecipe``,
etc.) MUST supply a decoder that branches on the ``recipe_kind``
discriminator and constructs the right subclass:

.. code-block:: python

    def decode_recipe(payload: dict) -> Recipe:
        kind = payload.get("recipe_kind")
        if kind == "gam":
            return GAMRecipe.model_validate(payload)
        if kind == "kevel":
            return KevelRecipe.model_validate(payload)
        raise ValueError(f"unknown recipe_kind={kind!r}")

Cross-tenant safety
-------------------

:meth:`get`, :meth:`try_reserve_consumption`, :meth:`finalize_consumption`,
:meth:`release_consumption`, and :meth:`get_by_media_buy_id` enforce
tenant isolation at the SQL level — ``WHERE account_id = %s`` is part
of every query predicate, not a Python-level filter after fetch.

Concurrency
-----------

:meth:`try_reserve_consumption` uses ``SELECT ... FOR UPDATE`` inside a
single-connection transaction so two parallel ``create_media_buy``
callers cannot both reserve the same proposal. The loser of the race
raises ``PROPOSAL_NOT_COMMITTED`` once the winner's UPDATE commits.

Schema bootstrap
----------------

Three equivalent ways to land the table, pick one:

* **App-boot bootstrap** — call :meth:`PgProposalStore.create_schema`
  on every application start. Idempotent via ``CREATE TABLE IF NOT
  EXISTS``; honours the ``table_name`` you constructed with.
* **Alembic / dbmate** — call
  :meth:`PgProposalStore.migration_sql(table_name=...)` to get an
  ``{"upgrade": "...", "downgrade": "..."}`` dict you can paste into
  a migration revision. The classmethod honours custom table names.
* **Raw psql / Flyway** — apply
  :file:`src/adcp/decisioning/pg/proposal_store.sql` verbatim. Only
  matches the default table name (``adcp_proposal_drafts``).

The three sources are kept in sync by ``tests/test_pg_proposal_store_migration_sync.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from adcp.decisioning.proposal_store import (
    ProposalRecord,
    ProposalState,
)
from adcp.decisioning.recipe import Recipe
from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

try:
    from psycopg_pool import AsyncConnectionPool as _AsyncConnectionPool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

_INSTALL_HINT = (
    "PgProposalStore requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'` "
    "(Poetry: `poetry add 'adcp[pg]'`)."
)

DEFAULT_TABLE_NAME = "adcp_proposal_drafts"

_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _default_recipe_decoder(payload: Mapping[str, Any]) -> Recipe:
    payload_dict = dict(payload)
    if "recipe_kind" in payload_dict:
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                "PgProposalStore.get: stored recipe payload has "
                f"recipe_kind={payload_dict.get('recipe_kind')!r} but no "
                "recipe_decoder was supplied at construction. The default "
                "decoder only handles the base Recipe shape; adopters with "
                "typed Recipe subclasses (GAMRecipe, KevelRecipe, etc.) "
                "MUST supply a `recipe_decoder=` callable that branches "
                "on `recipe_kind` and returns the right subclass. See "
                "PgProposalStore module docstring for the pattern."
            ),
            recovery="terminal",
        )
    return Recipe.model_validate(payload_dict)


def _encode_recipes(recipes: Mapping[str, Recipe]) -> str:
    out: dict[str, dict[str, Any]] = {}
    for product_id, recipe in recipes.items():
        if isinstance(recipe, Recipe):
            out[str(product_id)] = recipe.model_dump(mode="json")
        elif isinstance(recipe, Mapping):
            out[str(product_id)] = dict(recipe)
        else:
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"PgProposalStore.put_draft: recipes[{product_id!r}] must "
                    f"be a Recipe instance or Mapping, got {type(recipe).__name__}."
                ),
                recovery="terminal",
            )
    return json.dumps(out)


def _decode_recipes(
    raw: Any,
    decoder: Callable[[Mapping[str, Any]], Recipe],
) -> dict[str, Recipe]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw)
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                f"PgProposalStore: recipes column returned non-object "
                f"{type(parsed).__name__}; schema drift suspected."
            ),
            recovery="terminal",
        )
    out: dict[str, Recipe] = {}
    for product_id, payload in parsed.items():
        if not isinstance(payload, Mapping):
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"PgProposalStore: recipes[{product_id!r}] is "
                    f"{type(payload).__name__}, expected object."
                ),
                recovery="terminal",
            )
        try:
            out[str(product_id)] = decoder(payload)
        except AdcpError:
            raise
        except Exception as exc:
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"PgProposalStore.get: recipe_decoder failed for "
                    f"recipes[{product_id!r}]: {exc}. If your adapter "
                    "uses typed Recipe subclasses, supply a "
                    "`recipe_decoder=` callable when constructing "
                    "PgProposalStore — see the module docstring for "
                    "the recipe_kind-branching pattern."
                ),
                recovery="terminal",
            ) from exc
    return out


def _decode_payload(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw)
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                f"PgProposalStore: proposal_payload column returned non-object "
                f"{type(parsed).__name__}; schema drift suspected."
            ),
            recovery="terminal",
        )
    return dict(parsed)


def _ensure_utc(dt: Any) -> datetime | None:
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                f"PgProposalStore: expires_at column returned non-datetime "
                f"{type(dt).__name__}; schema drift suspected."
            ),
            recovery="terminal",
        )
    if dt.tzinfo is None:
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                "PgProposalStore received a naive datetime from expires_at. "
                "This usually means the column was created as "
                "TIMESTAMP WITHOUT TIME ZONE instead of TIMESTAMPTZ — "
                "adopter migration drift from the SDK schema. Recreate the "
                "column as TIMESTAMPTZ (see PgProposalStore.MIGRATION for "
                "the canonical DDL)."
            ),
            recovery="terminal",
        )
    return dt


class PgProposalStore:
    """PostgreSQL-backed :class:`~adcp.decisioning.ProposalStore`."""

    is_durable: ClassVar[bool] = True

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        table_name: str = DEFAULT_TABLE_NAME,
        recipe_decoder: Callable[[Mapping[str, Any]], Recipe] | None = None,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        if not _SAFE_IDENTIFIER_RE.fullmatch(table_name):
            raise ValueError(
                f"table_name must match [a-z_][a-z0-9_]{{0,62}} (ASCII only), "
                f"got {table_name!r}"
            )
        self._pool = pool
        self._table = table_name
        self._recipe_decoder = recipe_decoder or _default_recipe_decoder

        t = self._table
        self._sql_put_draft = (  # noqa: S608
            f"INSERT INTO {t} "
            f"(account_id, proposal_id, publisher_id, state, recipes, proposal_payload, "
            f" recipe_schema_version, created_at, updated_at) "
            f"VALUES (%s, %s, %s, 'draft', %s::jsonb, %s::jsonb, %s, now(), now()) "
            f"ON CONFLICT (account_id, proposal_id) DO UPDATE SET "
            f"  publisher_id     = EXCLUDED.publisher_id, "
            f"  recipes          = EXCLUDED.recipes, "
            f"  proposal_payload = EXCLUDED.proposal_payload, "
            f"  recipe_schema_version = EXCLUDED.recipe_schema_version, "
            f"  updated_at       = now() "
            f"WHERE {t}.state = 'draft' "
            f"RETURNING xmax = 0 AS inserted"
        )
        self._sql_get_state = (  # noqa: S608
            f"SELECT state, expires_at, proposal_payload FROM {t} "
            f"WHERE account_id = %s AND proposal_id = %s "
            f"AND publisher_id IS NOT DISTINCT FROM %s"
        )
        self._sql_select_state_for_update = (  # noqa: S608
            f"SELECT state, expires_at, proposal_payload FROM {t} "
            f"WHERE account_id = %s AND proposal_id = %s "
            f"AND publisher_id IS NOT DISTINCT FROM %s FOR UPDATE"
        )
        self._sql_commit = (  # noqa: S608
            f"UPDATE {t} SET "
            f"  state            = 'committed', "
            f"  expires_at       = %s, "
            f"  proposal_payload = %s::jsonb, "
            f"  updated_at       = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'draft' "
            f"AND publisher_id IS NOT DISTINCT FROM %s "
            f"RETURNING proposal_id"
        )
        self._sql_select_for_update = (  # noqa: S608
            f"SELECT state, recipes, proposal_payload, expires_at, "
            f"       media_buy_id, recipe_schema_version, publisher_id "
            f"FROM {t} WHERE account_id = %s AND proposal_id = %s "
            f"AND publisher_id IS NOT DISTINCT FROM %s FOR UPDATE"
        )
        self._sql_reserve = (  # noqa: S608
            f"UPDATE {t} SET state = 'consuming', updated_at = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'committed' "
            f"AND publisher_id IS NOT DISTINCT FROM %s"
        )
        self._sql_finalize = (  # noqa: S608
            f"UPDATE {t} SET "
            f"  state        = 'consumed', "
            f"  media_buy_id = %s, "
            f"  updated_at   = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'consuming' "
            f"AND publisher_id IS NOT DISTINCT FROM %s "
            f"RETURNING proposal_id"
        )
        self._sql_release = (  # noqa: S608
            f"UPDATE {t} SET state = 'committed', updated_at = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'consuming' "
            f"AND publisher_id IS NOT DISTINCT FROM %s "
            f"RETURNING proposal_id"
        )
        self._sql_mark_consumed = (  # noqa: S608
            f"UPDATE {t} SET "
            f"  state        = 'consumed', "
            f"  media_buy_id = %s, "
            f"  updated_at   = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'committed' "
            f"AND publisher_id IS NOT DISTINCT FROM %s "
            f"RETURNING proposal_id"
        )
        self._sql_discard = (  # noqa: S608
            f"DELETE FROM {t} WHERE account_id = %s AND proposal_id = %s "
            f"AND publisher_id IS NOT DISTINCT FROM %s"
        )
        self._sql_get_by_media_buy_id = (  # noqa: S608
            f"SELECT proposal_id, account_id, state, recipes, proposal_payload, "
            f"       expires_at, media_buy_id, recipe_schema_version, publisher_id "
            f"FROM {t} WHERE account_id = %s AND media_buy_id = %s"
        )

    async def create_schema(self) -> None:
        t = self._table
        statements = [
            f"""CREATE TABLE IF NOT EXISTS {t} (
                account_id              TEXT        COLLATE "C" NOT NULL,
                proposal_id             TEXT        COLLATE "C" NOT NULL,
                state                   TEXT        NOT NULL
                    CHECK (state IN ('draft', 'committed', 'consuming', 'consumed')),
                recipes                 JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
                proposal_payload        JSONB       NOT NULL,
                expires_at              TIMESTAMPTZ,
                media_buy_id            TEXT        COLLATE "C",
                recipe_schema_version   INTEGER     NOT NULL DEFAULT 1,
                publisher_id            TEXT        COLLATE "C",
                created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (account_id, proposal_id)
            )""",
            f"""CREATE UNIQUE INDEX IF NOT EXISTS {t}_media_buy_idx
                ON {t} (account_id, media_buy_id)
                WHERE media_buy_id IS NOT NULL""",
            f"""CREATE INDEX IF NOT EXISTS {t}_expires_idx
                ON {t} (expires_at)
                WHERE expires_at IS NOT NULL""",
            f"""CREATE INDEX IF NOT EXISTS {t}_publisher_idx
                ON {t} (publisher_id, account_id)
                WHERE publisher_id IS NOT NULL""",
        ]
        async with self._pool.connection() as conn:
            for stmt in statements:
                await conn.execute(stmt)

    async def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
        publisher_id: str | None = None,
        recipes: Mapping[str, Recipe],
        proposal_payload: Mapping[str, Any],
    ) -> None:
        recipes_json = _encode_recipes(recipes)
        payload_json = json.dumps(dict(proposal_payload))
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_put_draft,
                (account_id, proposal_id, publisher_id, recipes_json, payload_json, 1),
            )
            row = await cur.fetchone()
            if row is not None:
                return
            cur2 = await conn.execute(self._sql_get_state, (account_id, proposal_id, publisher_id))
            existing = await cur2.fetchone()
            if existing is None:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"PgProposalStore.put_draft: proposal {proposal_id!r} "
                        "vanished between conflict and refetch. Concurrent "
                        "discard suspected."
                    ),
                    recovery="terminal",
                )
            state_str = existing[0]
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"Cannot put_draft on proposal {proposal_id!r} in "
                    f"state {state_str!r}; refine iterations are only "
                    "valid on draft proposals. Once committed or "
                    "consumed, a proposal_id is immutable."
                ),
                recovery="terminal",
            )

    async def get(
        self,
        proposal_id: str,
        *,
        expected_account_id: str | None = None,
        expected_publisher_id: str | None = None,
    ) -> ProposalRecord | None:
        if expected_account_id is None:
            if expected_publisher_id is None:
                sql = (  # noqa: S608
                    f"SELECT proposal_id, account_id, state, recipes, "
                    f"proposal_payload, expires_at, media_buy_id, "
                    f"recipe_schema_version, publisher_id FROM {self._table} "
                    f"WHERE proposal_id = %s"
                )
                params: tuple[Any, ...] = (proposal_id,)
            else:
                sql = (  # noqa: S608
                    f"SELECT proposal_id, account_id, state, recipes, "
                    f"proposal_payload, expires_at, media_buy_id, "
                    f"recipe_schema_version, publisher_id FROM {self._table} "
                    f"WHERE proposal_id = %s "
                    f"AND publisher_id IS NOT DISTINCT FROM %s"
                )
                params = (proposal_id, expected_publisher_id)
        else:
            if expected_publisher_id is None:
                sql = (  # noqa: S608
                    f"SELECT proposal_id, account_id, state, recipes, "
                    f"proposal_payload, expires_at, media_buy_id, "
                    f"recipe_schema_version, publisher_id FROM {self._table} "
                    f"WHERE account_id = %s AND proposal_id = %s"
                )
                params = (expected_account_id, proposal_id)
            else:
                sql = (  # noqa: S608
                    f"SELECT proposal_id, account_id, state, recipes, "
                    f"proposal_payload, expires_at, media_buy_id, "
                    f"recipe_schema_version, publisher_id FROM {self._table} "
                    f"WHERE account_id = %s AND proposal_id = %s "
                    f"AND publisher_id IS NOT DISTINCT FROM %s"
                )
                params = (expected_account_id, proposal_id, expected_publisher_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            if row is None:
                return None
            return self._row_to_record(row)

    async def commit(
        self,
        proposal_id: str,
        *,
        expires_at: datetime,
        proposal_payload: Mapping[str, Any],
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        payload_dict = dict(proposal_payload)
        payload_json = json.dumps(payload_dict)
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    self._sql_select_state_for_update,
                    (expected_account_id, proposal_id, expected_publisher_id),
                )
                existing = await cur.fetchone()
                if existing is None:
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message=(
                            f"Cannot commit proposal {proposal_id!r}: not "
                            "in store for the expected tenant. The "
                            "framework's finalize dispatch must put_draft "
                            "before commit."
                        ),
                        recovery="terminal",
                    )
                current_state, current_expires_at, current_payload = existing
                if current_state == "committed":
                    same_deadline = _ensure_utc(current_expires_at) == expires_at
                    cur_payload_dict = (
                        current_payload
                        if isinstance(current_payload, dict)
                        else json.loads(current_payload) if current_payload is not None else {}
                    )
                    same_payload = cur_payload_dict == payload_dict
                    if same_deadline and same_payload:
                        return
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message=(
                            f"Proposal {proposal_id!r} already committed "
                            "with a different expires_at or payload — "
                            "re-commit with different values is a developer "
                            "bug."
                        ),
                        recovery="terminal",
                    )
                if current_state != "draft":
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message=(
                            f"Cannot commit proposal {proposal_id!r} from "
                            f"state {current_state!r}; commit requires "
                            "DRAFT."
                        ),
                        recovery="terminal",
                    )
                update_cur = await conn.execute(
                    self._sql_commit,
                    (
                        expires_at,
                        payload_json,
                        expected_account_id,
                        proposal_id,
                        expected_publisher_id,
                    ),
                )
                if await update_cur.fetchone() is None:
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message=(
                            f"PgProposalStore.commit: UPDATE returned zero "
                            f"rows for proposal {proposal_id!r} despite "
                            "passing the FOR UPDATE state check. Schema "
                            "drift suspected."
                        ),
                        recovery="terminal",
                    )

    async def try_reserve_consumption(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> ProposalRecord:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    self._sql_select_for_update,
                    (expected_account_id, proposal_id, expected_publisher_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise AdcpError(
                        "PROPOSAL_NOT_FOUND",
                        message=(f"Proposal {proposal_id!r} not found."),
                        recovery="terminal",
                        field="proposal_id",
                    )
                (
                    state_str,
                    recipes_raw,
                    payload_raw,
                    expires_at_raw,
                    media_buy_id,
                    recipe_schema_version,
                    publisher_id_raw,
                ) = row
                if state_str != "committed":
                    raise AdcpError(
                        "PROPOSAL_NOT_COMMITTED",
                        message=(
                            f"Proposal {proposal_id!r} is in state "
                            f"{state_str!r}; create_media_buy requires a "
                            "committed proposal that hasn't been accepted "
                            "or reserved by another request."
                        ),
                        recovery="correctable",
                        field="proposal_id",
                    )
                await conn.execute(
                    self._sql_reserve,
                    (expected_account_id, proposal_id, expected_publisher_id),
                )
                return ProposalRecord(
                    proposal_id=proposal_id,
                    account_id=expected_account_id,
                    publisher_id=publisher_id_raw,
                    state=ProposalState.CONSUMING,
                    recipes=_decode_recipes(recipes_raw, self._recipe_decoder),
                    proposal_payload=_decode_payload(payload_raw),
                    expires_at=_ensure_utc(expires_at_raw),
                    media_buy_id=media_buy_id,
                    recipe_schema_version=int(recipe_schema_version or 1),
                )

    async def finalize_consumption(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_finalize,
                (media_buy_id, expected_account_id, proposal_id, expected_publisher_id),
            )
            if await cur.fetchone() is not None:
                return
            cur2 = await conn.execute(
                self._sql_get_state, (expected_account_id, proposal_id, expected_publisher_id)
            )
            existing = await cur2.fetchone()
            if existing is None:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"finalize_consumption: proposal {proposal_id!r} "
                        "not found for the expected tenant."
                    ),
                    recovery="terminal",
                )
            state_str = existing[0]
            if state_str == "consumed":
                cur3 = await conn.execute(
                    f"SELECT media_buy_id FROM {self._table} "  # noqa: S608
                    f"WHERE account_id = %s AND proposal_id = %s "
                    f"AND publisher_id IS NOT DISTINCT FROM %s",
                    (expected_account_id, proposal_id, expected_publisher_id),
                )
                row = await cur3.fetchone()
                existing_media_buy_id = row[0] if row else None
                if existing_media_buy_id == media_buy_id:
                    return
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Proposal {proposal_id!r} already consumed by "
                        f"media_buy_id={existing_media_buy_id!r}; cannot "
                        f"re-consume as {media_buy_id!r}."
                    ),
                    recovery="terminal",
                )
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"finalize_consumption requires CONSUMING; "
                    f"proposal {proposal_id!r} is in {state_str!r}. "
                    "Framework must call try_reserve_consumption first."
                ),
                recovery="terminal",
            )

    async def release_consumption(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_release,
                (expected_account_id, proposal_id, expected_publisher_id),
            )
            if await cur.fetchone() is not None:
                return
            cur2 = await conn.execute(
                self._sql_get_state, (expected_account_id, proposal_id, expected_publisher_id)
            )
            existing = await cur2.fetchone()
            if existing is None:
                return
            state_str = existing[0]
            if state_str == "committed":
                return
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"release_consumption requires CONSUMING; "
                    f"proposal {proposal_id!r} is in {state_str!r}."
                ),
                recovery="terminal",
            )

    async def mark_consumed(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    f"SELECT state, media_buy_id FROM {self._table} "  # noqa: S608
                    f"WHERE account_id = %s AND proposal_id = %s "
                    f"AND publisher_id IS NOT DISTINCT FROM %s FOR UPDATE",
                    (expected_account_id, proposal_id, expected_publisher_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message=(
                            f"Cannot mark_consumed proposal {proposal_id!r}: "
                            "not in store for the expected tenant."
                        ),
                        recovery="terminal",
                    )
                state_str, existing_media_buy_id = row
                if state_str == "consumed":
                    if existing_media_buy_id == media_buy_id:
                        return
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message=(
                            f"Proposal {proposal_id!r} already consumed by "
                            f"media_buy_id={existing_media_buy_id!r}; cannot "
                            f"re-consume as {media_buy_id!r}."
                        ),
                        recovery="terminal",
                    )
                if state_str != "committed":
                    raise AdcpError(
                        "INTERNAL_ERROR",
                        message=(
                            f"Cannot mark_consumed proposal {proposal_id!r} "
                            f"from state {state_str!r}; mark_consumed "
                            "requires COMMITTED."
                        ),
                        recovery="terminal",
                    )
                await conn.execute(
                    self._sql_mark_consumed,
                    (media_buy_id, expected_account_id, proposal_id, expected_publisher_id),
                )

    async def discard(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                self._sql_discard, (expected_account_id, proposal_id, expected_publisher_id)
            )

    async def get_by_media_buy_id(
        self,
        media_buy_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> ProposalRecord | None:
        if expected_publisher_id is None:
            sql = self._sql_get_by_media_buy_id
            params: tuple[Any, ...] = (expected_account_id, media_buy_id)
        else:
            sql = (  # noqa: S608
                f"SELECT proposal_id, account_id, state, recipes, proposal_payload, "
                f"       expires_at, media_buy_id, recipe_schema_version, publisher_id "
                f"FROM {self._table} WHERE account_id = %s AND media_buy_id = %s "
                f"AND publisher_id IS NOT DISTINCT FROM %s"
            )
            params = (expected_account_id, media_buy_id, expected_publisher_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            if row is None:
                return None
            return self._row_to_record(row)

    def _row_to_record(self, row: tuple[Any, ...]) -> ProposalRecord:
        (
            proposal_id,
            account_id,
            state_str,
            recipes_raw,
            payload_raw,
            expires_at_raw,
            media_buy_id,
            recipe_schema_version,
            publisher_id,
        ) = row
        return ProposalRecord(
            proposal_id=proposal_id,
            account_id=account_id,
            publisher_id=publisher_id,
            state=ProposalState(state_str),
            recipes=_decode_recipes(recipes_raw, self._recipe_decoder),
            proposal_payload=_decode_payload(payload_raw),
            expires_at=_ensure_utc(expires_at_raw),
            media_buy_id=media_buy_id,
            recipe_schema_version=int(recipe_schema_version or 1),
        )


def _migration_sql(table_name: str = DEFAULT_TABLE_NAME) -> dict[str, str]:
    if not _SAFE_IDENTIFIER_RE.fullmatch(table_name):
        raise ValueError(
            f"table_name must match [a-z_][a-z0-9_]{{0,62}} (ASCII only), " f"got {table_name!r}"
        )
    t = table_name
    return {
        "upgrade": (
            f"CREATE TABLE IF NOT EXISTS {t} (\n"
            f'    account_id              TEXT        COLLATE "C" NOT NULL,\n'
            f'    proposal_id             TEXT        COLLATE "C" NOT NULL,\n'
            f"    state                   TEXT        NOT NULL\n"
            f"        CHECK (state IN ('draft', 'committed', 'consuming', 'consumed')),\n"
            f"    recipes                 JSONB       NOT NULL DEFAULT '{{}}'::jsonb,\n"
            f"    proposal_payload        JSONB       NOT NULL,\n"
            f"    expires_at              TIMESTAMPTZ,\n"
            f'    media_buy_id            TEXT        COLLATE "C",\n'
            f"    recipe_schema_version   INTEGER     NOT NULL DEFAULT 1,\n"
            f'    publisher_id            TEXT        COLLATE "C",\n'
            f"    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
            f"    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
            f"    PRIMARY KEY (account_id, proposal_id)\n"
            f");\n"
            f"CREATE UNIQUE INDEX IF NOT EXISTS {t}_media_buy_idx\n"
            f"    ON {t} (account_id, media_buy_id)\n"
            f"    WHERE media_buy_id IS NOT NULL;\n"
            f"CREATE INDEX IF NOT EXISTS {t}_expires_idx\n"
            f"    ON {t} (expires_at)\n"
            f"    WHERE expires_at IS NOT NULL;\n"
            f"CREATE INDEX IF NOT EXISTS {t}_publisher_idx\n"
            f"    ON {t} (publisher_id, account_id)\n"
            f"    WHERE publisher_id IS NOT NULL;\n"
        ),
        "downgrade": f"DROP TABLE IF EXISTS {t};\n",
    }


def _migration_sql_add_publisher_id(table_name: str = DEFAULT_TABLE_NAME) -> dict[str, str]:
    """Build Alembic-compatible upgrade/downgrade SQL to add ``publisher_id``
    to an existing table created before this column existed.
    """
    if not _SAFE_IDENTIFIER_RE.fullmatch(table_name):
        raise ValueError(
            f"table_name must match [a-z_][a-z0-9_]{{0,62}} (ASCII only), "
            f"got {table_name!r}"
        )
    t = table_name
    return {
        "upgrade": (
            f'ALTER TABLE {t} ADD COLUMN IF NOT EXISTS publisher_id TEXT COLLATE "C";\n'
            f"CREATE INDEX IF NOT EXISTS {t}_publisher_idx\n"
            f"    ON {t} (publisher_id, account_id)\n"
            f"    WHERE publisher_id IS NOT NULL;\n"
        ),
        "downgrade": (
            f"DROP INDEX IF EXISTS {t}_publisher_idx;\n"
            f"ALTER TABLE {t} DROP COLUMN IF EXISTS publisher_id;\n"
        ),
    }


PgProposalStore.migration_sql = classmethod(  # type: ignore[attr-defined]
    lambda cls, table_name=DEFAULT_TABLE_NAME: _migration_sql(table_name)
)
PgProposalStore.migration_sql_add_publisher_id = classmethod(  # type: ignore[attr-defined]
    lambda cls, table_name=DEFAULT_TABLE_NAME: _migration_sql_add_publisher_id(table_name)
)


__all__ = [
    "DEFAULT_TABLE_NAME",
    "PG_AVAILABLE",
    "PgProposalStore",
]
