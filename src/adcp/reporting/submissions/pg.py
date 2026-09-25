"""PostgreSQL buyer intent storage with short transactions and no network locks.

The application owns the pool and its lifecycle. ``create_schema`` installs only
the isolated buyer tables, without modifying seller schemas or existing APIs.
Importing this module does not require psycopg; actual use requires adcp[pg].
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
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
    validate_submission,
)
from adcp.types import SyncReportingReceiptsResponse

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_P = ParamSpec("_P")
_R = TypeVar("_R")


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
    with the same memory/PostgreSQL state-machine vectors.
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
        # These are the concurrency invariants, not an IF NOT EXISTS assumption.
        rows = await (
            await connection.execute(
                "SELECT c.conname,c.contype,c.convalidated,c.condeferrable,"
                " pg_get_constraintdef(c.oid) FROM pg_constraint c"
                " WHERE c.conrelid IN ('reporting_buyer_submission_scopes'::regclass,"
                " 'reporting_buyer_submission_intents'::regclass)"
            )
        ).fetchall()
        constraints = {row[0]: tuple(row[1:]) for row in rows}
        required = {
            "reporting_buyer_submission_scopes_pkey": (
                "p",
                True,
                False,
                "PRIMARY KEY (scope_sha256)",
            ),
            "reporting_buyer_submission_intents_pkey": (
                "p",
                True,
                False,
                "PRIMARY KEY (scope_sha256, submission_id)",
            ),
            "reporting_buyer_submission_scope_fk": (
                "f",
                True,
                False,
                "FOREIGN KEY (scope_sha256) REFERENCES "
                "reporting_buyer_submission_scopes(scope_sha256)",
            ),
        }
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

    async def _scope_on(
        self, connection: Any, scope: ReportingSubmissionScope, *, create: bool = False
    ) -> tuple[bool, str | None]:
        if create:
            await connection.execute(
                "INSERT INTO reporting_buyer_submission_scopes"
                " (scope_sha256,canonical_identity) VALUES (%s,%s)"
                " ON CONFLICT (scope_sha256) DO NOTHING",
                (scope.storage_key, scope.canonical_identity.decode()),
            )
        row = await (
            await connection.execute(
                "SELECT canonical_identity,current_submission_id"
                " FROM reporting_buyer_submission_scopes WHERE scope_sha256=%s FOR UPDATE",
                (scope.storage_key,),
            )
        ).fetchone()
        if row is None:
            return False, None
        if row[0] != scope.canonical_identity.decode():
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        if row[1] is None:
            existing = await (
                await connection.execute(
                    "SELECT 1 FROM reporting_buyer_submission_intents"
                    " WHERE scope_sha256=%s LIMIT 1",
                    (scope.storage_key,),
                )
            ).fetchone()
            if existing is not None:
                raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        return True, row[1]

    async def _read_on(
        self, connection: Any, scope: ReportingSubmissionScope, submission_id: str
    ) -> ReportingReceiptSubmission | None:
        row = await (
            await connection.execute(
                "SELECT submission_id,canonical_plan,plan_sha256,confirmed_results,"
                " confirmed_sha256,pending FROM reporting_buyer_submission_intents"
                " WHERE scope_sha256=%s AND submission_id=%s",
                (scope.storage_key, submission_id),
            )
        ).fetchone()
        return decode_submission(scope, row) if row is not None else None

    @_closed_storage_errors
    async def reserve(self, proposed: ReportingReceiptSubmission) -> ReportingReceiptSubmission:
        validate_submission(proposed)
        if proposed.confirmed_chunks:
            raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_PLAN)
        scope = proposed.scope
        async with self._pool.connection() as connection, connection.transaction():
            _, current_id = await self._scope_on(connection, scope, create=True)
            if current_id is not None:
                current = await self._read_on(connection, scope, current_id)
                if current is None:
                    raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
                if current.pending:
                    return current
            previous = await self._read_on(connection, scope, proposed.submission_id)
            if previous is not None:
                if previous._plan != proposed._plan:
                    raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
                return previous
            plan, plan_digest, confirmed, confirmed_digest = encode_submission(proposed)
            await connection.execute(
                "INSERT INTO reporting_buyer_submission_intents"
                " (scope_sha256,submission_id,canonical_plan,plan_sha256,"
                " confirmed_results,confirmed_sha256,pending) VALUES (%s,%s,%s,%s,%s,%s,true)",
                (
                    scope.storage_key,
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
                (proposed.submission_id, scope.storage_key),
            )
        return proposed

    @_closed_storage_errors
    async def get(
        self, scope: ReportingSubmissionScope, submission_id: str | None = None
    ) -> ReportingReceiptSubmission | None:
        async with self._pool.connection() as connection, connection.transaction():
            exists, current_id = await self._scope_on(connection, scope)
            if not exists or current_id is None:
                return None
            current = await self._read_on(connection, scope, current_id)
            if current is None:
                raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
            if submission_id is None or submission_id == current_id:
                return current
            return await self._read_on(connection, scope, submission_id)

    @_closed_storage_errors
    async def confirm(
        self,
        scope: ReportingSubmissionScope,
        submission_id: str,
        chunk: int,
        response: SyncReportingReceiptsResponse,
    ) -> ReportingReceiptSubmission:
        async with self._pool.connection() as connection, connection.transaction():
            exists, current_id = await self._scope_on(connection, scope)
            state = await self._read_on(connection, scope, submission_id) if exists else None
            if state is None:
                raise ReportingSubmissionError(ReportingSubmissionCode.NOT_FOUND)
            if state.pending and current_id != submission_id:
                raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
            updated = confirm_submission(state, chunk, response)
            if updated is not state:
                _, _, confirmed, digest = encode_submission(updated)
                await connection.execute(
                    "UPDATE reporting_buyer_submission_intents"
                    " SET confirmed_results=%s,confirmed_sha256=%s,pending=%s"
                    " WHERE scope_sha256=%s AND submission_id=%s",
                    (confirmed, digest, updated.pending, scope.storage_key, submission_id),
                )
        return updated
