"""PostgreSQL buyer intent storage with short transactions and no network locks.

The application owns the pool and its lifecycle. ``create_schema`` installs only
the isolated buyer tables, without modifying seller schemas or existing APIs.
Importing this module does not require psycopg; actual use requires adcp[pg].
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
from importlib.resources import files
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from adcp.reporting.submissions.models import (
    ReportingReceiptSubmission,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    ReportingSubmissionScope,
    confirm_submission,
    decode_submission,
    encode_submission,
    freeze_response,
    validate_submission,
)
from adcp.types import SyncReportingReceiptsResponse

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_P = ParamSpec("_P")
_R = TypeVar("_R")
_MAX_CAS_ATTEMPTS = 16
_Row = tuple[Any, ...]


@dataclass(frozen=True, repr=False)
class _Snapshot:
    """Complete raw state used by a read/validate/lock/compare transaction.

    Actual text, digests, pending flags, pointer and scope identity participate
    in equality. A matching digest label alone never permits a write.
    """

    scope: _Row | None
    current: _Row | None
    selected: _Row | None
    has_intents: bool


def _closed_storage_errors(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    @wraps(fn)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await fn(*args, **kwargs)
        except ReportingSubmissionError as error:
            code = error.code
        except Exception:
            code = ReportingSubmissionCode.STORAGE_UNAVAILABLE
        # Outside both handlers: even __context__ cannot contain driver details.
        raise ReportingSubmissionError(code)

    return wrapped


class PgReportingSubmissionIntentStore:
    """Production persistence for optional buyer receipt submission intents.

    Pass a trusted application-owned psycopg AsyncConnectionPool, preferably
    with a dedicated schema/search_path and bounded connection/statement waits.
    There is no credential or connection string in this object's representation.
    Run create_schema explicitly during rollout, then validate custom backends
    with the same memory/PostgreSQL state-machine vectors. Readiness requires all
    shipped, validated CHECK definitions as well as the keys and reservation
    index. Snapshot validation happens outside transactions; a mutation compares
    the complete locked state, retrying at most 16 times. Contention exhaustion
    raises STORAGE_UNAVAILABLE and leaves the durable intent available to resume.
    """

    def __init__(self, *, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @_closed_storage_errors
    async def create_schema(self) -> None:
        available = False
        try:
            import psycopg  # noqa: F401

            available = True
        except ImportError:
            pass
        if not available:
            raise ReportingSubmissionError(ReportingSubmissionCode.PG_REQUIRED)
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (712071172,))
            sql = (
                files("adcp.reporting.ledger")
                .joinpath("reporting_buyer_submissions.sql")
                .read_text()
            )
            await connection.execute(sql)
            await self._check_schema_on(connection)

    async def _check_schema_on(self, connection: Any) -> None:
        # Verify storage bounds as well as concurrency invariants. IF NOT EXISTS
        # must not silently bless a pre-created table with weaker constraints.
        rows = await (
            await connection.execute(
                "SELECT c.conrelid='reporting_buyer_submission_scopes'::regclass,"
                " c.conname,c.contype,c.convalidated,c.condeferrable,c.connoinherit,"
                " pg_get_constraintdef(c.oid) FROM pg_constraint c"
                " WHERE c.conrelid IN ('reporting_buyer_submission_scopes'::regclass,"
                " 'reporting_buyer_submission_intents'::regclass)"
            )
        ).fetchall()
        constraints = {(row[0], row[1]): tuple(row[2:]) for row in rows}
        required = {
            (True, "reporting_buyer_submission_scopes_pkey"): (
                "p",
                True,
                False,
                True,
                "PRIMARY KEY (scope_sha256)",
            ),
            (False, "reporting_buyer_submission_intents_pkey"): (
                "p",
                True,
                False,
                True,
                "PRIMARY KEY (scope_sha256, submission_id)",
            ),
            (False, "reporting_buyer_submission_scope_fk"): (
                "f",
                True,
                False,
                True,
                "FOREIGN KEY (scope_sha256) REFERENCES "
                "reporting_buyer_submission_scopes(scope_sha256)",
            ),
        }
        checks = (
            (True, "scope_digest", "scope_sha256 ~ '^[a-f0-9]{64}$'::text"),
            (True, "scope_bound", "octet_length(canonical_identity) <= 32768"),
            (False, "submission_id", "submission_id ~ '^reporting-submission:[a-f0-9]{64}$'::text"),
            (False, "plan_digest", "plan_sha256 ~ '^[a-f0-9]{64}$'::text"),
            (False, "confirmed_digest", "confirmed_sha256 ~ '^[a-f0-9]{64}$'::text"),
            (False, "plan_bound", "octet_length(canonical_plan) <= 16777216"),
            (False, "confirmed_bound", "octet_length(confirmed_results) <= 16777216"),
        )
        for scopes_table, suffix, expression in checks:
            required[(scopes_table, f"reporting_buyer_{suffix}")] = (
                "c",
                True,
                False,
                False,
                f"CHECK (({expression}))",
            )
        if any(constraints.get(name) != expected for name, expected in required.items()):
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        index = await (
            await connection.execute(
                "SELECT i.indisunique,i.indisvalid,i.indisready,i.indnkeyatts,"
                " pg_get_indexdef(i.indexrelid,1,true),pg_get_expr(i.indpred,i.indrelid),"
                " i.indrelid='reporting_buyer_submission_intents'::regclass"
                " FROM pg_index i WHERE i.indexrelid='reporting_buyer_one_pending_scope'::regclass"
            )
        ).fetchone()
        if index is None or tuple(index) != (True, True, True, 1, "scope_sha256", "pending", True):
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)

    async def _snapshot_on(
        self, connection: Any, key: str, selected_id: str | None, *, lock: bool = False
    ) -> _Snapshot:
        scope_query = (
            "SELECT canonical_identity,current_submission_id"
            " FROM reporting_buyer_submission_scopes WHERE scope_sha256=%s FOR UPDATE"
            if lock
            else "SELECT canonical_identity,current_submission_id"
            " FROM reporting_buyer_submission_scopes WHERE scope_sha256=%s"
        )
        row = await (await connection.execute(scope_query, (key,))).fetchone()
        if row is None:
            return _Snapshot(None, None, None, False)
        intent_query = (
            "SELECT submission_id,canonical_plan,plan_sha256,confirmed_results,"
            " confirmed_sha256,pending FROM reporting_buyer_submission_intents"
            " WHERE scope_sha256=%s AND (submission_id=%s OR submission_id=%s) FOR UPDATE"
            if lock
            else "SELECT submission_id,canonical_plan,plan_sha256,confirmed_results,"
            " confirmed_sha256,pending FROM reporting_buyer_submission_intents"
            " WHERE scope_sha256=%s AND (submission_id=%s OR submission_id=%s)"
        )
        rows = await (await connection.execute(intent_query, (key, row[1], selected_id))).fetchall()
        by_id = {value[0]: tuple(value) for value in rows}
        has_intents = bool(rows)
        if row[1] is None:
            has_intents = (
                await (
                    await connection.execute(
                        "SELECT 1 FROM reporting_buyer_submission_intents"
                        " WHERE scope_sha256=%s LIMIT 1",
                        (key,),
                    )
                ).fetchone()
            ) is not None
        return _Snapshot(
            tuple(row),
            by_id.get(row[1]),
            by_id.get(row[1] if selected_id is None else selected_id),
            has_intents,
        )

    async def _snapshot(self, key: str, selected_id: str | None) -> _Snapshot:
        # Release the read-only MVCC snapshot and connection before CPU work.
        # No scope row lock or caller-owned validation occurs in this phase.
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return await self._snapshot_on(connection, key, selected_id)

    def _validate_snapshot(
        self, scope: ReportingSubmissionScope, snapshot: _Snapshot
    ) -> tuple[ReportingReceiptSubmission | None, ReportingReceiptSubmission | None]:
        if snapshot.scope is None:
            return None, None
        if snapshot.scope[0] != scope.canonical_identity.decode() or (
            snapshot.scope[1] is None and snapshot.has_intents
        ):
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        if snapshot.scope[1] is not None and snapshot.current is None:
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        current = decode_submission(scope, snapshot.current) if snapshot.current else None
        selected = (
            current
            if snapshot.selected is snapshot.current
            else decode_submission(scope, snapshot.selected) if snapshot.selected else None
        )
        if selected is not None and selected.pending and selected != current:
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        return current, selected

    @_closed_storage_errors
    async def reserve(self, proposed: ReportingReceiptSubmission) -> ReportingReceiptSubmission:
        validate_submission(proposed)
        if proposed.confirmed_chunks:
            raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_PLAN)
        scope = proposed.scope
        key, identity = scope.storage_key, scope.canonical_identity.decode()
        plan, plan_digest, confirmed, confirmed_digest = encode_submission(proposed)
        for _ in range(_MAX_CAS_ATTEMPTS):
            snapshot = await self._snapshot(key, proposed.submission_id)
            current, previous = await asyncio.to_thread(self._validate_snapshot, scope, snapshot)
            if current is not None and current.pending:
                return current
            if previous is not None:
                if previous._plan != proposed._plan:
                    raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
                return previous
            async with self._pool.connection() as connection, connection.transaction():
                expected = snapshot
                if snapshot.scope is None:
                    await connection.execute(
                        "INSERT INTO reporting_buyer_submission_scopes"
                        " (scope_sha256,canonical_identity) VALUES (%s,%s)"
                        " ON CONFLICT (scope_sha256) DO NOTHING",
                        (key, identity),
                    )
                    expected = _Snapshot((identity, None), None, None, False)
                actual = await self._snapshot_on(connection, key, proposed.submission_id, lock=True)
                if actual != expected:
                    continue
                await connection.execute(
                    "INSERT INTO reporting_buyer_submission_intents"
                    " (scope_sha256,submission_id,canonical_plan,plan_sha256,"
                    " confirmed_results,confirmed_sha256,pending) VALUES (%s,%s,%s,%s,%s,%s,true)",
                    (
                        key,
                        proposed.submission_id,
                        plan,
                        plan_digest,
                        confirmed,
                        confirmed_digest,
                    ),
                )
                await connection.execute(
                    "UPDATE reporting_buyer_submission_scopes SET current_submission_id=%s"
                    " WHERE scope_sha256=%s",
                    (proposed.submission_id, key),
                )
            return proposed
        raise ReportingSubmissionError(ReportingSubmissionCode.STORAGE_UNAVAILABLE)

    @_closed_storage_errors
    async def get(
        self, scope: ReportingSubmissionScope, submission_id: str | None = None
    ) -> ReportingReceiptSubmission | None:
        snapshot = await self._snapshot(scope.storage_key, submission_id)
        _, selected = await asyncio.to_thread(self._validate_snapshot, scope, snapshot)
        return selected

    @_closed_storage_errors
    async def confirm(
        self,
        scope: ReportingSubmissionScope,
        submission_id: str,
        chunk: int,
        response: SyncReportingReceiptsResponse,
    ) -> ReportingReceiptSubmission:
        # Caller-owned Pydantic objects are detached before the first await.
        # Retries compare the same immutable response, even if its owner edits it.
        frozen = freeze_response(response)
        key = scope.storage_key
        for _ in range(_MAX_CAS_ATTEMPTS):
            snapshot = await self._snapshot(key, submission_id)
            _, state = await asyncio.to_thread(self._validate_snapshot, scope, snapshot)
            if state is None:
                raise ReportingSubmissionError(ReportingSubmissionCode.NOT_FOUND)
            updated = await asyncio.to_thread(confirm_submission, state, chunk, frozen)
            _, _, confirmed, digest = encode_submission(updated)
            pending = updated.pending
            async with self._pool.connection() as connection, connection.transaction():
                actual = await self._snapshot_on(connection, key, submission_id, lock=True)
                if actual != snapshot:
                    continue
                if updated is not state:
                    await connection.execute(
                        "UPDATE reporting_buyer_submission_intents"
                        " SET confirmed_results=%s,confirmed_sha256=%s,pending=%s"
                        " WHERE scope_sha256=%s AND submission_id=%s",
                        (confirmed, digest, pending, key, submission_id),
                    )
            return updated
        # Contention never frees the lane or guesses whether another commit won.
        raise ReportingSubmissionError(ReportingSubmissionCode.STORAGE_UNAVAILABLE)
