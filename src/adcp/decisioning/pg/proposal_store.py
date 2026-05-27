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

The schema's primary key is ``(account_id, proposal_id)``. That is
collision-safe **only** when ``account_id`` is globally unique across
your deployment. Multi-tenant seller adopters are expected to compose
tenant scope INTO ``Account.id`` at the
:class:`~adcp.decisioning.AccountStore` layer (see the AccountStore
docstring's "Multi-tenant deployments" section, or use
:func:`~adcp.decisioning.create_tenant_store`) — this store treats
``account_id`` as opaque and does not parse it.

Adopters who want a foreign key to their tenants table own the schema
on top of what this store ships. The recommended path is to add a
plain ``tenant_id`` column to ``adcp_proposal_drafts`` in your own
migration and populate it at write time alongside the framework's
``put_draft`` call (your adopter ProposalStore wrapper has the typed
:attr:`~adcp.decisioning.RequestContext.account` in scope):

.. code-block:: sql

    ALTER TABLE adcp_proposal_drafts ADD COLUMN tenant_id TEXT;
    ALTER TABLE adcp_proposal_drafts
        ADD CONSTRAINT adcp_proposal_drafts_tenant_fk
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        ON DELETE CASCADE;
    CREATE INDEX adcp_proposal_drafts_tenant_idx
        ON adcp_proposal_drafts (tenant_id);

A Postgres ``GENERATED ALWAYS AS (...) STORED`` column derived by
parsing ``account_id`` is tempting but bakes the encoding delimiter
into adopter schema permanently — a later change to your AccountStore
encoding (adding a region prefix, swapping the separator) silently
corrupts the generated value. Mint ``tenant_id`` from application
code where the encoding is owned in one place.

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

# Same ASCII-only identifier guard used by PgBackend / PgTaskRegistry —
# the table name is static-formatted into SQL, so this is the only
# protection against SQL injection or Unicode homoglyph substitution.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _default_recipe_decoder(payload: Mapping[str, Any]) -> Recipe:
    """Default decoder: base :class:`Recipe` round-trip.

    Adopters using typed Recipe subclasses MUST supply their own
    decoder; the base class declares ``extra='forbid'`` and rejects
    subclass-specific fields.

    Fail-loud when the payload has a ``recipe_kind`` field — that's a
    deterministic signal the adopter has typed subclasses but forgot
    to wire a decoder. The raw Pydantic ``extra='forbid'`` error
    wouldn't mention ``recipe_decoder=``; this one does.
    """
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


def _encode_recipes(recipes: Mapping[str, Recipe | Mapping[str, Any]]) -> str:
    """Serialize the recipes mapping to a JSONB-compatible JSON string."""
    out: dict[str, dict[str, Any]] = {}
    for product_id, recipe in recipes.items():
        if isinstance(recipe, Recipe):
            out[str(product_id)] = recipe.model_dump(mode="json")
        elif isinstance(recipe, Mapping):
            # Allow pre-serialized dicts; adopter tests or migrations
            # may hand the store dict-shaped recipes directly.
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
    """Rehydrate a JSONB recipes blob back into a ``{product_id: Recipe}``
    mapping using the adopter-supplied decoder.

    psycopg returns JSONB columns as already-parsed Python objects
    (``dict``); fall back to ``json.loads`` for the string path so
    tests that round-trip via raw psycopg cursors still work.
    """
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
            # Pydantic ValidationError / arbitrary decoder failure —
            # surface with a pointer to ``recipe_decoder=`` so adopters
            # don't have to spelunk into framework source to figure out
            # what to fix.
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
    """Rehydrate the proposal_payload JSONB blob to a plain dict."""
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
    """Normalize a psycopg-returned ``TIMESTAMPTZ`` to a tz-aware datetime.

    A naive datetime here means the column was created as
    ``TIMESTAMP WITHOUT TIME ZONE`` instead of ``TIMESTAMPTZ`` — adopter
    Alembic migration drift from the SDK schema. Fail fast rather than
    silently coerce; silent UTC defaults will produce wrong expiry
    windows when the server's local time is not UTC.
    """
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
    """PostgreSQL-backed :class:`~adcp.decisioning.ProposalStore`.

    Durable counterpart to :class:`~adcp.decisioning.InMemoryProposalStore`.
    Set ``is_durable = True`` so production-mode gates accept it without
    requiring the dev-mode bypass.

    :param pool: ``psycopg_pool.AsyncConnectionPool`` owned by the
        caller. Each operation acquires a short-lived connection;
        :meth:`try_reserve_consumption` holds one for the duration of
        its CAS transaction.
    :param table_name: Override the default table name. Useful for
        adopters with one Postgres serving multiple AdCP instances or
        whose ``proposal_drafts`` table is already taken. Defaults to
        ``adcp_proposal_drafts``.
    :param recipe_decoder: Callable ``(payload: dict) -> Recipe`` used
        to rehydrate stored recipe payloads back to typed
        :class:`Recipe` instances. Adopters with subclasses
        (``GAMRecipe``, ``KevelRecipe``, etc.) MUST supply a decoder
        that branches on ``recipe_kind``. Defaults to
        :meth:`Recipe.model_validate` which only works for the base
        ``Recipe`` shape.

    :raises ImportError: when psycopg/psycopg-pool are not installed.
    :raises ValueError: when ``table_name`` is not a safe ASCII
        identifier (``[a-z_][a-z0-9_]{0,62}``).
    """

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
        # put_draft: insert a fresh DRAFT row, or rewrite an existing DRAFT
        # row in place. ON CONFLICT DO UPDATE is gated on state='draft' so
        # a buyer probing put_draft against a COMMITTED/CONSUMED record
        # falls through to a zero-row UPDATE — we then SELECT to surface
        # the INTERNAL_ERROR with the actual current state.
        self._sql_put_draft = (  # noqa: S608 — table name whitelisted
            f"INSERT INTO {t} "
            f"(account_id, proposal_id, state, recipes, proposal_payload, "
            f" recipe_schema_version, created_at, updated_at) "
            f"VALUES (%s, %s, 'draft', %s::jsonb, %s::jsonb, %s, now(), now()) "
            f"ON CONFLICT (account_id, proposal_id) DO UPDATE SET "
            f"  recipes          = EXCLUDED.recipes, "
            f"  proposal_payload = EXCLUDED.proposal_payload, "
            f"  recipe_schema_version = EXCLUDED.recipe_schema_version, "
            f"  updated_at       = now() "
            f"WHERE {t}.state = 'draft' "
            f"RETURNING xmax = 0 AS inserted"
        )
        self._sql_get_state = (  # noqa: S608
            f"SELECT state, expires_at, proposal_payload FROM {t} "
            f"WHERE account_id = %s AND proposal_id = %s"
        )
        # Tenant-scoped SELECT FOR UPDATE used by commit() to lock the
        # row before the state-machine check + UPDATE. Mirrors the
        # try_reserve_consumption pattern.
        self._sql_select_state_for_update = (  # noqa: S608
            f"SELECT state, expires_at, proposal_payload FROM {t} "
            f"WHERE account_id = %s AND proposal_id = %s FOR UPDATE"
        )
        self._sql_commit = (  # noqa: S608
            f"UPDATE {t} SET "
            f"  state            = 'committed', "
            f"  expires_at       = %s, "
            f"  proposal_payload = %s::jsonb, "
            f"  updated_at       = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'draft' "
            f"RETURNING proposal_id"
        )
        # try_reserve_consumption uses SELECT ... FOR UPDATE inside a tx
        # so two parallel callers serialize on the row lock. The CAS
        # check (state='committed') happens after the lock is held; the
        # loser sees CONSUMING/CONSUMED and raises PROPOSAL_NOT_COMMITTED.
        self._sql_select_for_update = (  # noqa: S608
            f"SELECT state, recipes, proposal_payload, expires_at, "
            f"       media_buy_id, recipe_schema_version "
            f"FROM {t} WHERE account_id = %s AND proposal_id = %s FOR UPDATE"
        )
        self._sql_reserve = (  # noqa: S608
            f"UPDATE {t} SET state = 'consuming', updated_at = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'committed'"
        )
        self._sql_finalize = (  # noqa: S608
            f"UPDATE {t} SET "
            f"  state        = 'consumed', "
            f"  media_buy_id = %s, "
            f"  updated_at   = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'consuming' "
            f"RETURNING proposal_id"
        )
        self._sql_release = (  # noqa: S608
            f"UPDATE {t} SET state = 'committed', updated_at = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'consuming' "
            f"RETURNING proposal_id"
        )
        self._sql_mark_consumed = (  # noqa: S608
            f"UPDATE {t} SET "
            f"  state        = 'consumed', "
            f"  media_buy_id = %s, "
            f"  updated_at   = now() "
            f"WHERE account_id = %s AND proposal_id = %s AND state = 'committed' "
            f"RETURNING proposal_id"
        )
        self._sql_discard = (  # noqa: S608
            f"DELETE FROM {t} WHERE account_id = %s AND proposal_id = %s"
        )
        self._sql_get_by_media_buy_id = (  # noqa: S608
            f"SELECT proposal_id, account_id, state, recipes, proposal_payload, "
            f"       expires_at, media_buy_id, recipe_schema_version "
            f"FROM {t} WHERE account_id = %s AND media_buy_id = %s"
        )

    # -- schema bootstrap -----------------------------------------------

    async def create_schema(self) -> None:
        """Create the proposal store table + supporting indexes.

        Honors the ``table_name`` kwarg the store was constructed with.
        Idempotent via ``CREATE TABLE IF NOT EXISTS`` — safe to call on
        every application boot. The equivalent raw DDL ships at
        :file:`adcp/decisioning/pg/proposal_store.sql` in the installed
        package for adopters using a migration tool.
        """
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
        ]
        async with self._pool.connection() as conn:
            for stmt in statements:
                await conn.execute(stmt)

    # -- ProposalStore Protocol -----------------------------------------

    async def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
        recipes: Mapping[str, Recipe],
        proposal_payload: Mapping[str, Any],
    ) -> None:
        recipes_json = _encode_recipes(recipes)
        payload_json = json.dumps(dict(proposal_payload))
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_put_draft,
                (account_id, proposal_id, recipes_json, payload_json, 1),
            )
            row = await cur.fetchone()
            if row is not None:
                # Either inserted fresh (xmax=0) or rewrote a DRAFT.
                return
            # ON CONFLICT DO UPDATE matched zero rows — the record is in
            # a non-DRAFT state. Re-fetch to surface the current state in
            # the error message.
            cur2 = await conn.execute(self._sql_get_state, (account_id, proposal_id))
            existing = await cur2.fetchone()
            if existing is None:
                # Race: row vanished between the failed INSERT and the
                # follow-up SELECT (concurrent discard from another
                # worker). Surface as INTERNAL_ERROR so the framework's
                # outer dispatcher can decide whether to retry; we don't
                # transparently retry here because put_draft is meant to
                # be one-shot per dispatch.
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
    ) -> ProposalRecord | None:
        # The Protocol allows expected_account_id=None — historically a
        # convenience for diagnostic / admin callers. We still serve
        # that case but route it through a separate query without the
        # account predicate so the tenancy-aware fast path is purely
        # parameterised; a future code reader can't accidentally pass
        # None into a tenancy-required call.
        if expected_account_id is None:
            sql = (  # noqa: S608 — table name pre-validated at construction
                f"SELECT proposal_id, account_id, state, recipes, "
                f"proposal_payload, expires_at, media_buy_id, "
                f"recipe_schema_version FROM {self._table} "
                f"WHERE proposal_id = %s"
            )
            params: tuple[Any, ...] = (proposal_id,)
        else:
            sql = (  # noqa: S608
                f"SELECT proposal_id, account_id, state, recipes, "
                f"proposal_payload, expires_at, media_buy_id, "
                f"recipe_schema_version FROM {self._table} "
                f"WHERE account_id = %s AND proposal_id = %s"
            )
            params = (expected_account_id, proposal_id)
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
    ) -> None:
        payload_dict = dict(proposal_payload)
        payload_json = json.dumps(payload_dict)
        # Atomic: SELECT FOR UPDATE → state-machine check → UPDATE,
        # all inside one transaction. The SELECT predicate is keyed on
        # (account_id, proposal_id) so a cross-tenant probe collapses
        # to "not in store" without touching another tenant's row.
        # The row lock prevents a concurrent put_draft / commit from
        # racing the validate-then-update sequence.
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    self._sql_select_state_for_update,
                    (expected_account_id, proposal_id),
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
                    # Idempotent only when the second commit matches the
                    # first.
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
                    (expires_at, payload_json, expected_account_id, proposal_id),
                )
                if await update_cur.fetchone() is None:
                    # Should not happen under the row lock, but fail loud
                    # if it does — silent zero-row UPDATE would let the
                    # caller believe the transition landed.
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
    ) -> ProposalRecord:
        # Single-connection transaction so SELECT FOR UPDATE + UPDATE
        # serialize across parallel callers.
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    self._sql_select_for_update,
                    (expected_account_id, proposal_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise AdcpError(
                        "PROPOSAL_NOT_FOUND",
                        message=(f"Proposal {proposal_id!r} not found."),
                        recovery="correctable",
                        field="proposal_id",
                    )
                (
                    state_str,
                    recipes_raw,
                    payload_raw,
                    expires_at_raw,
                    media_buy_id,
                    recipe_schema_version,
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
                    (expected_account_id, proposal_id),
                )
                # Build the in-memory record reflecting the transition.
                return ProposalRecord(
                    proposal_id=proposal_id,
                    account_id=expected_account_id,
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
    ) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_finalize,
                (media_buy_id, expected_account_id, proposal_id),
            )
            if await cur.fetchone() is not None:
                return
            # Zero rows updated. Determine why.
            cur2 = await conn.execute(self._sql_get_state, (expected_account_id, proposal_id))
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
                # Idempotent on re-call with the same media_buy_id.
                cur3 = await conn.execute(
                    f"SELECT media_buy_id FROM {self._table} "  # noqa: S608
                    f"WHERE account_id = %s AND proposal_id = %s",
                    (expected_account_id, proposal_id),
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
    ) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_release,
                (expected_account_id, proposal_id),
            )
            if await cur.fetchone() is not None:
                return
            # Zero rows updated. Idempotent: no-op on unknown id /
            # cross-tenant probe, no-op on already-COMMITTED.
            cur2 = await conn.execute(self._sql_get_state, (expected_account_id, proposal_id))
            existing = await cur2.fetchone()
            if existing is None:
                # Unknown id or cross-tenant — idempotent no-op so the
                # adapter-failure rollback path can be unconditional.
                return
            state_str = existing[0]
            if state_str == "committed":
                # Already rolled back.
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
    ) -> None:
        # Tenant-scoped SELECT FOR UPDATE → state-machine check →
        # UPDATE. Cross-tenant probes collapse to "not in store".
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    f"SELECT state, media_buy_id FROM {self._table} "  # noqa: S608
                    f"WHERE account_id = %s AND proposal_id = %s FOR UPDATE",
                    (expected_account_id, proposal_id),
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
                    (media_buy_id, expected_account_id, proposal_id),
                )

    async def discard(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_discard, (expected_account_id, proposal_id))

    async def get_by_media_buy_id(
        self,
        media_buy_id: str,
        *,
        expected_account_id: str,
    ) -> ProposalRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_get_by_media_buy_id,
                (expected_account_id, media_buy_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return self._row_to_record(row)

    # -- helpers --------------------------------------------------------

    def _row_to_record(self, row: tuple[Any, ...]) -> ProposalRecord:
        """Project a SELECT row tuple to a typed :class:`ProposalRecord`."""
        (
            proposal_id,
            account_id,
            state_str,
            recipes_raw,
            payload_raw,
            expires_at_raw,
            media_buy_id,
            recipe_schema_version,
        ) = row
        return ProposalRecord(
            proposal_id=proposal_id,
            account_id=account_id,
            state=ProposalState(state_str),
            recipes=_decode_recipes(recipes_raw, self._recipe_decoder),
            proposal_payload=_decode_payload(payload_raw),
            expires_at=_ensure_utc(expires_at_raw),
            media_buy_id=media_buy_id,
            recipe_schema_version=int(recipe_schema_version or 1),
        )


def _migration_sql(table_name: str = DEFAULT_TABLE_NAME) -> dict[str, str]:
    """Build Alembic-compatible upgrade/downgrade SQL for a given table
    name. Public via :meth:`PgProposalStore.migration_sql`.

    Validates the identifier the same way the constructor does so an
    adopter can't accidentally land an unsafe DDL string in their
    migration.
    """
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
        ),
        "downgrade": f"DROP TABLE IF EXISTS {t};\n",
    }


# Bind the helper as a classmethod on PgProposalStore so adopters can
# call ``PgProposalStore.migration_sql(table_name="my_proposals")``
# from Alembic without instantiating the store.
PgProposalStore.migration_sql = classmethod(  # type: ignore[attr-defined]
    lambda cls, table_name=DEFAULT_TABLE_NAME: _migration_sql(table_name)
)


__all__ = [
    "DEFAULT_TABLE_NAME",
    "PG_AVAILABLE",
    "PgProposalStore",
]
