"""SQLite durable store for :mod:`adcp.compat.purchase_continuation`.

The store opens one connection per operation and uses ``BEGIN IMMEDIATE`` for
claim/state transitions.  That gives a single atomic winner across threads and
processes sharing the same local database file.  Network filesystems with weak
SQLite locking semantics are not supported; distributed deployments should
implement :class:`CompatibilityContinuationStore` on their transactional
database.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from adcp.compat.purchase_continuation import (
    CompatibilityContinuationError,
    CompatibilityContinuationErrorCode,
    CompatibilityOperationState,
    CompatibilityPurchaseOperation,
    LegacyPurchaseContinuation,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adcp_compat_continuations (
    token_hash TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    account_identity TEXT NOT NULL,
    source_adcp_version TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    observed_request_json TEXT NOT NULL,
    observed_response_json TEXT NOT NULL,
    observed_payload_hash TEXT NOT NULL,
    product_ids_json TEXT NOT NULL,
    projected_products_json TEXT NOT NULL DEFAULT '[]',
    losses_json TEXT NOT NULL,
    mutation_idempotency_guaranteed INTEGER NOT NULL DEFAULT 0,
    target_binding TEXT NOT NULL,
    listed_purchase_context_json TEXT,
    claimed_operation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adcp_compat_continuations_principal_idx
    ON adcp_compat_continuations (principal_id, token_hash);

CREATE TABLE IF NOT EXISTS adcp_compat_operations (
    operation_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('claimed', 'in_flight', 'pending', 'succeeded', 'failed', 'ambiguous')
    ),
    revision INTEGER NOT NULL DEFAULT 1,
    execution_input_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (principal_id, idempotency_key),
    UNIQUE (token_hash),
    FOREIGN KEY (token_hash)
        REFERENCES adcp_compat_continuations(token_hash)
);
"""


class SqliteCompatibilityContinuationStore:
    """Durable local continuation ledger backed by a SQLite file."""

    is_durable: ClassVar[bool] = True

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        raw = str(path)
        if raw == ":memory:" or raw.startswith("file::memory:"):
            raise ValueError("SqliteCompatibilityContinuationStore requires a file-backed database")
        # Keep the lexical path so lstat() can reject symlink components;
        # Path.resolve() would dereference them before the safety walk.
        self.path = Path(os.path.abspath(os.path.expanduser(raw)))
        self._ensure_private_parent_directory()
        self.timeout = timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure_private_database_file()
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)
            self._ensure_timestamp_columns(conn)

    def _ensure_timestamp_columns(self, conn: sqlite3.Connection) -> None:
        """Migrate ledgers created by pre-release coordinator builds."""

        # Serialize the inspect/alter/backfill sequence across processes. Without
        # the write lock, two starters can both observe a missing column and the
        # second ALTER then fails with ``duplicate column name``.
        conn.execute("BEGIN IMMEDIATE")
        now = _format_datetime(self._clock())
        for table in ("adcp_compat_continuations", "adcp_compat_operations"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column in ("created_at", "updated_at"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} IS NULL",
                    (now,),
                )
        continuation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(adcp_compat_continuations)")
        }
        if "projected_products_json" not in continuation_columns:
            conn.execute(
                "ALTER TABLE adcp_compat_continuations " "ADD COLUMN projected_products_json TEXT"
            )
        if "mutation_idempotency_guaranteed" not in continuation_columns:
            conn.execute(
                "ALTER TABLE adcp_compat_continuations "
                "ADD COLUMN mutation_idempotency_guaranteed INTEGER NOT NULL DEFAULT 0"
            )

        operation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(adcp_compat_operations)")
        }
        if "revision" not in operation_columns:
            conn.execute(
                "ALTER TABLE adcp_compat_operations "
                "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
        if "execution_input_json" not in operation_columns:
            conn.execute(
                "ALTER TABLE adcp_compat_operations "
                "ADD COLUMN execution_input_json TEXT NOT NULL DEFAULT '{}'"
            )

        operations_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'adcp_compat_operations'"
        ).fetchone()
        operations_sql = operations_sql_row["sql"] if operations_sql_row is not None else ""
        if "'pending'" not in operations_sql or "'failed'" not in operations_sql:
            self._rebuild_operations_table(conn)

    @staticmethod
    def _rebuild_operations_table(conn: sqlite3.Connection) -> None:
        """Expand the operation-state constraint without losing ledger rows."""

        conn.execute("ALTER TABLE adcp_compat_operations RENAME TO adcp_compat_operations_old")
        conn.execute(
            """
            CREATE TABLE adcp_compat_operations (
                operation_id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'claimed', 'in_flight', 'pending', 'succeeded', 'failed', 'ambiguous'
                    )
                ),
                revision INTEGER NOT NULL DEFAULT 1,
                execution_input_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (principal_id, idempotency_key),
                UNIQUE (token_hash),
                FOREIGN KEY (token_hash)
                    REFERENCES adcp_compat_continuations(token_hash)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO adcp_compat_operations (
                operation_id, principal_id, idempotency_key, token_hash,
                payload_hash, state, revision, execution_input_json,
                result_json, created_at, updated_at
            )
            SELECT
                operation_id, principal_id, idempotency_key, token_hash,
                payload_hash, state, revision, execution_input_json,
                result_json, created_at, updated_at
            FROM adcp_compat_operations_old
            """
        )
        conn.execute("DROP TABLE adcp_compat_operations_old")

    def _ensure_private_parent_directory(self) -> None:
        """Create and validate the directory used for SQLite pathname opens."""

        parent = self.path.parent
        chain = list(reversed(parent.parents)) + [parent]
        for directory in chain:
            try:
                directory_status = directory.lstat()
            except FileNotFoundError:
                directory.mkdir(mode=0o700)
                directory_status = directory.lstat()
            except OSError as exc:
                raise PermissionError(f"SQLite directory {directory} is not accessible") from exc
            if not stat.S_ISDIR(directory_status.st_mode):
                raise PermissionError(
                    f"SQLite directory {directory} must be a real directory, not a symlink"
                )

        try:
            direct_status = parent.lstat()
        except OSError as exc:
            raise PermissionError(f"SQLite parent directory {parent} is not accessible") from exc
        if not stat.S_ISDIR(direct_status.st_mode):
            raise PermissionError(f"SQLite parent directory {parent} is not a directory")
        if direct_status.st_uid != os.geteuid():
            raise PermissionError(
                f"SQLite parent directory {parent} must be owned by the current user"
            )
        direct_mode = stat.S_IMODE(direct_status.st_mode)
        if direct_mode & 0o022:
            raise PermissionError(
                f"SQLite parent directory {parent} has mode {direct_mode:#o}; "
                "remove group/world write access before opening"
            )

        for ancestor in parent.parents:
            try:
                ancestor_status = ancestor.lstat()
            except OSError as exc:
                raise PermissionError(
                    f"SQLite ancestor directory {ancestor} is not accessible"
                ) from exc
            ancestor_mode = stat.S_IMODE(ancestor_status.st_mode)
            unsafe_writable = bool(ancestor_mode & 0o022) and not bool(
                ancestor_status.st_mode & stat.S_ISVTX
            )
            if unsafe_writable:
                raise PermissionError(
                    f"SQLite ancestor directory {ancestor} has unsafe writable mode "
                    f"{ancestor_mode:#o}"
                )

    def _ensure_private_database_file(self) -> None:
        """Create the ledger as 0600 and reject an existing loose mode."""

        self._ensure_private_file(self.path, "continuation database")

    def _ensure_private_sidecar_files(self) -> None:
        """Pre-create SQLite WAL files privately before SQLite can open them.

        SQLite normally inherits the database mode for ``-wal`` and ``-shm``
        files.  Creating and validating them explicitly makes that guarantee
        independent of the process umask and SQLite build behavior.
        """

        for suffix in ("-wal", "-shm"):
            self._ensure_private_file(Path(f"{self.path}{suffix}"), "SQLite sidecar")

    @staticmethod
    def _ensure_private_file(path: Path, description: str) -> None:
        """Atomically create *path* as 0600 or validate the existing file."""

        create_flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        existing_flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            create_flags |= os.O_NOFOLLOW
            existing_flags |= os.O_NOFOLLOW
        while True:
            try:
                descriptor = os.open(path, create_flags, 0o600)
                break
            except FileExistsError:
                try:
                    descriptor = os.open(path, existing_flags)
                    break
                except FileNotFoundError:
                    # SQLite removes sidecars when the last WAL connection
                    # closes.  If that happens between the existence check and
                    # this open, retry the atomic create path.
                    continue
                except OSError as exc:
                    raise PermissionError(
                        f"{description} {path} is not a safe regular file"
                    ) from exc
            except OSError as exc:
                raise PermissionError(f"{description} {path} is not a safe regular file") from exc
        try:
            file_status = os.fstat(descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                raise PermissionError(f"{description} {path} is not a regular file")
            if file_status.st_uid != os.geteuid():
                raise PermissionError(f"{description} {path} must be owned by the current user")
            mode = stat.S_IMODE(file_status.st_mode)
            if mode & 0o077:
                raise PermissionError(
                    f"{description} {path} has mode {mode:#o}; "
                    "restrict it to 0o600 before opening"
                )
        finally:
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        # Re-check on every connection so sidecars removed after the previous
        # last close are recreated with a private mode before the next write.
        self._ensure_private_parent_directory()
        self._ensure_private_database_file()
        self._ensure_private_sidecar_files()
        conn = sqlite3.connect(self.path, timeout=self.timeout)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            return conn
        except BaseException:
            conn.close()
            raise

    async def put_continuation(self, continuation: LegacyPurchaseContinuation) -> None:
        await asyncio.to_thread(self._put_continuation, copy.deepcopy(continuation))

    def _put_continuation(self, value: LegacyPurchaseContinuation) -> None:
        now = _format_datetime(self._clock())
        if value.projected_products is None:
            raise ValueError("new continuations require buyer-visible product bindings")
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO adcp_compat_continuations (
                        token_hash, principal_id, account_identity,
                        source_adcp_version, expires_at, observed_request_json,
                        observed_response_json, observed_payload_hash,
                        product_ids_json, projected_products_json, losses_json,
                        mutation_idempotency_guaranteed, target_binding,
                        listed_purchase_context_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value.token_hash,
                        value.principal_id,
                        value.account_identity,
                        value.source_adcp_version,
                        _format_datetime(value.expires_at),
                        _dumps(value.observed_request),
                        _dumps(value.observed_response),
                        value.observed_payload_hash,
                        _dumps(list(value.product_ids)),
                        _dumps(list(value.projected_products)),
                        _dumps(sorted(value.losses)),
                        int(value.mutation_idempotency_guaranteed),
                        value.target_binding,
                        (
                            _dumps(value.listed_purchase_context)
                            if value.listed_purchase_context is not None
                            else None
                        ),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise _error(
                CompatibilityContinuationErrorCode.STORE_CONFLICT,
                "continuation token hash is already registered",
                "Issue a fresh cryptographically random continuation token.",
            ) from exc

    async def get_continuation(
        self, token_hash: str, *, principal_id: str
    ) -> LegacyPurchaseContinuation | None:
        return await asyncio.to_thread(self._get_continuation, token_hash, principal_id)

    def _get_continuation(
        self, token_hash: str, principal_id: str
    ) -> LegacyPurchaseContinuation | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                """
                SELECT * FROM adcp_compat_continuations
                WHERE token_hash = ? AND principal_id = ?
                """,
                (token_hash, principal_id),
            ).fetchone()
        return _decode_continuation(row) if row is not None else None

    async def claim(
        self,
        token_hash: str,
        *,
        principal_id: str,
        idempotency_key: str,
        payload_hash: str,
        execution_input: Mapping[str, Any],
        now: datetime,
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._claim,
            token_hash,
            principal_id,
            idempotency_key,
            payload_hash,
            copy.deepcopy(dict(execution_input)),
            now,
        )

    def _claim(
        self,
        token_hash: str,
        principal_id: str,
        idempotency_key: str,
        payload_hash: str,
        execution_input: dict[str, Any],
        now: datetime,
    ) -> CompatibilityPurchaseOperation:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            claim_time = max(_as_utc(now), _as_utc(self._clock()))
            existing = conn.execute(
                """
                SELECT * FROM adcp_compat_operations
                WHERE principal_id = ? AND idempotency_key = ?
                """,
                (principal_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                operation = _decode_operation(existing)
                if operation.token_hash != token_hash or operation.payload_hash != payload_hash:
                    raise _error(
                        CompatibilityContinuationErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was already used with a different logical payload",
                        "Use the original payload or start a new projected purchase.",
                    )
                if not operation.execution_input:
                    # Pre-hardening ledgers retained the logical payload hash
                    # but not the sanitized execution snapshot. An exact retry
                    # can adopt it atomically; the revision increment fences
                    # every pre-migration operation object.
                    updated_at = _format_datetime(self._clock())
                    adopted = conn.execute(
                        "UPDATE adcp_compat_operations "
                        "SET execution_input_json = ?, revision = revision + 1, updated_at = ? "
                        "WHERE operation_id = ? AND revision = ? "
                        "AND execution_input_json = '{}'",
                        (
                            _dumps(execution_input),
                            updated_at,
                            operation.operation_id,
                            operation.revision,
                        ),
                    )
                    if adopted.rowcount != 1:
                        raise _state_error("legacy execution input changed concurrently")
                    operation = CompatibilityPurchaseOperation(
                        operation_id=operation.operation_id,
                        principal_id=operation.principal_id,
                        idempotency_key=operation.idempotency_key,
                        token_hash=operation.token_hash,
                        payload_hash=operation.payload_hash,
                        state=operation.state,
                        revision=operation.revision + 1,
                        execution_input=copy.deepcopy(execution_input),
                        result=copy.deepcopy(operation.result),
                    )
                elif operation.execution_input != execution_input:
                    raise _state_error("stored execution input changed for idempotent claim")
                conn.commit()
                return operation

            continuation = conn.execute(
                """
                SELECT * FROM adcp_compat_continuations
                WHERE token_hash = ? AND principal_id = ?
                """,
                (token_hash, principal_id),
            ).fetchone()
            if continuation is None:
                raise _not_found()
            expires_at = _parse_datetime(continuation["expires_at"])
            if claim_time >= expires_at:
                raise _error(
                    CompatibilityContinuationErrorCode.EXPIRED,
                    "continuation expired before it could be claimed",
                    "Repeat product discovery and obtain a new continuation.",
                )
            if continuation["claimed_operation_id"] is not None:
                raise _error(
                    CompatibilityContinuationErrorCode.ALREADY_CLAIMED,
                    "continuation was already claimed by another operation",
                    "Replay the original idempotency key, or restart product discovery.",
                )

            operation_id = secrets.token_urlsafe(24)
            updated = conn.execute(
                """
                UPDATE adcp_compat_continuations
                SET claimed_operation_id = ?, updated_at = ?
                WHERE token_hash = ? AND principal_id = ?
                  AND claimed_operation_id IS NULL
                """,
                (operation_id, _format_datetime(claim_time), token_hash, principal_id),
            )
            if updated.rowcount != 1:
                raise _error(
                    CompatibilityContinuationErrorCode.ALREADY_CLAIMED,
                    "continuation was concurrently claimed by another operation",
                    "Replay the original idempotency key, or restart product discovery.",
                )
            conn.execute(
                """
                INSERT INTO adcp_compat_operations (
                    operation_id, principal_id, idempotency_key, token_hash,
                    payload_hash, state, revision, execution_input_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    principal_id,
                    idempotency_key,
                    token_hash,
                    payload_hash,
                    CompatibilityOperationState.CLAIMED.value,
                    1,
                    _dumps(execution_input),
                    _format_datetime(claim_time),
                    _format_datetime(claim_time),
                ),
            )
            conn.commit()
            return CompatibilityPurchaseOperation(
                operation_id=operation_id,
                principal_id=principal_id,
                idempotency_key=idempotency_key,
                token_hash=token_hash,
                payload_hash=payload_hash,
                state=CompatibilityOperationState.CLAIMED,
                revision=1,
                execution_input=copy.deepcopy(execution_input),
            )

    async def get_operation(
        self, operation_id: str, *, principal_id: str
    ) -> CompatibilityPurchaseOperation | None:
        return await asyncio.to_thread(self._get_operation, operation_id, principal_id)

    def _get_operation(
        self, operation_id: str, principal_id: str
    ) -> CompatibilityPurchaseOperation | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM adcp_compat_operations "
                "WHERE operation_id = ? AND principal_id = ?",
                (operation_id, principal_id),
            ).fetchone()
        return _decode_operation(row) if row is not None else None

    async def mark_in_flight(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            copy.deepcopy(operation),
            {CompatibilityOperationState.CLAIMED},
            CompatibilityOperationState.IN_FLIGHT,
            None,
        )

    async def mark_ambiguous(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            copy.deepcopy(operation),
            {CompatibilityOperationState.CLAIMED, CompatibilityOperationState.IN_FLIGHT},
            CompatibilityOperationState.AMBIGUOUS,
            None,
        )

    async def complete(
        self,
        operation: CompatibilityPurchaseOperation,
        result: Mapping[str, Any],
        *,
        state: CompatibilityOperationState = CompatibilityOperationState.SUCCEEDED,
    ) -> CompatibilityPurchaseOperation:
        if state not in {
            CompatibilityOperationState.PENDING,
            CompatibilityOperationState.SUCCEEDED,
            CompatibilityOperationState.FAILED,
        }:
            raise ValueError("complete state must be pending, succeeded, or failed")
        return await asyncio.to_thread(
            self._transition,
            copy.deepcopy(operation),
            {
                CompatibilityOperationState.IN_FLIGHT,
                CompatibilityOperationState.AMBIGUOUS,
                CompatibilityOperationState.PENDING,
            },
            state,
            copy.deepcopy(dict(result)),
        )

    async def fence_in_flight(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            copy.deepcopy(operation),
            {CompatibilityOperationState.IN_FLIGHT},
            CompatibilityOperationState.AMBIGUOUS,
            None,
        )

    async def resume_after_not_applied(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            copy.deepcopy(operation),
            {CompatibilityOperationState.AMBIGUOUS},
            CompatibilityOperationState.CLAIMED,
            None,
        )

    def _transition(
        self,
        operation: CompatibilityPurchaseOperation,
        allowed: set[CompatibilityOperationState],
        target: CompatibilityOperationState,
        result: dict[str, Any] | None,
    ) -> CompatibilityPurchaseOperation:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM adcp_compat_operations WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            if row is None:
                raise _state_error("operation is missing from continuation store")
            current = _decode_operation(row)
            if (
                current.principal_id != operation.principal_id
                or current.idempotency_key != operation.idempotency_key
                or current.token_hash != operation.token_hash
                or current.payload_hash != operation.payload_hash
                or current.execution_input != operation.execution_input
            ):
                raise _state_error("operation binding changed in continuation store")
            if current.revision != operation.revision:
                raise _state_error("operation revision changed concurrently")
            if current.state not in allowed:
                raise _state_error(
                    f"cannot transition operation from {current.state.value} to {target.value}"
                )
            result_json = _dumps(result) if result is not None else None
            updated_at = _format_datetime(self._clock())
            updated = conn.execute(
                """
                UPDATE adcp_compat_operations
                SET state = ?, result_json = ?, revision = revision + 1, updated_at = ?
                WHERE operation_id = ? AND state = ? AND revision = ?
                """,
                (
                    target.value,
                    result_json,
                    updated_at,
                    current.operation_id,
                    current.state.value,
                    current.revision,
                ),
            )
            if updated.rowcount != 1:
                raise _state_error("operation state changed concurrently")
            conn.commit()
            return CompatibilityPurchaseOperation(
                operation_id=current.operation_id,
                principal_id=current.principal_id,
                idempotency_key=current.idempotency_key,
                token_hash=current.token_hash,
                payload_hash=current.payload_hash,
                state=target,
                revision=current.revision + 1,
                execution_input=copy.deepcopy(current.execution_input),
                result=copy.deepcopy(result),
            )

    async def purge_resolved_before(self, cutoff: datetime) -> int:
        """Delete only old terminal or never-claimed continuations.

        Claimed, in-flight, pending, and ambiguous operations are deliberately
        retained regardless of age because deleting them could permit an unsafe
        replay. Returns the number of continuation records removed.
        """

        return await asyncio.to_thread(self._purge_resolved_before, cutoff)

    def _purge_resolved_before(self, cutoff: datetime) -> int:
        cutoff_utc = _as_utc(cutoff)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT
                    continuation.token_hash,
                    continuation.expires_at,
                    continuation.updated_at AS continuation_updated_at,
                    operation.operation_id,
                    operation.state,
                    operation.updated_at AS operation_updated_at
                FROM adcp_compat_continuations AS continuation
                LEFT JOIN adcp_compat_operations AS operation
                  ON operation.token_hash = continuation.token_hash
                WHERE operation.state IN ('succeeded', 'failed')
                   OR operation.operation_id IS NULL
                """,
            ).fetchall()
        candidates = {
            row["token_hash"]: (
                row["expires_at"],
                row["continuation_updated_at"],
                row["operation_id"],
                row["state"],
                row["operation_updated_at"],
            )
            for row in rows
            if (
                row["state"]
                in {
                    CompatibilityOperationState.SUCCEEDED.value,
                    CompatibilityOperationState.FAILED.value,
                }
                and _parse_datetime(row["operation_updated_at"]) < cutoff_utc
            )
            or (
                row["state"] is None
                and _parse_datetime(row["expires_at"]) < cutoff_utc
                and _parse_datetime(row["continuation_updated_at"]) < cutoff_utc
            )
        }
        if not candidates:
            return 0

        # Candidate scanning and timestamp parsing happen without a write lock.
        # Each small write transaction then compares the raw values again, so a
        # newly claimed or otherwise updated row cannot be purged from a stale
        # scan.
        deleted = 0
        token_hashes = list(candidates)
        for start in range(0, len(token_hashes), 200):
            batch = token_hashes[start : start + 200]
            deleted += self._purge_candidate_batch(batch, candidates)
        return deleted

    def _purge_candidate_batch(
        self,
        token_hashes: list[str],
        candidates: Mapping[str, tuple[str, str, str | None, str | None, str | None]],
    ) -> int:
        placeholders = ",".join("?" for _ in token_hashes)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT
                    continuation.token_hash,
                    continuation.expires_at,
                    continuation.updated_at AS continuation_updated_at,
                    operation.operation_id,
                    operation.state,
                    operation.updated_at AS operation_updated_at
                FROM adcp_compat_continuations AS continuation
                LEFT JOIN adcp_compat_operations AS operation
                  ON operation.token_hash = continuation.token_hash
                WHERE continuation.token_hash IN ({placeholders})
                """,
                token_hashes,
            ).fetchall()
            confirmed = [
                row["token_hash"]
                for row in rows
                if candidates.get(row["token_hash"])
                == (
                    row["expires_at"],
                    row["continuation_updated_at"],
                    row["operation_id"],
                    row["state"],
                    row["operation_updated_at"],
                )
            ]
            if not confirmed:
                return 0
            confirmed_placeholders = ",".join("?" for _ in confirmed)
            conn.execute(
                f"DELETE FROM adcp_compat_operations "
                f"WHERE token_hash IN ({confirmed_placeholders})",
                confirmed,
            )
            removed = conn.execute(
                f"DELETE FROM adcp_compat_continuations "
                f"WHERE token_hash IN ({confirmed_placeholders})",
                confirmed,
            ).rowcount
            conn.commit()
            return removed


def _decode_continuation(row: sqlite3.Row) -> LegacyPurchaseContinuation:
    listed = _loads(row["listed_purchase_context_json"])
    projected = _loads(row["projected_products_json"])
    return LegacyPurchaseContinuation(
        token_hash=row["token_hash"],
        principal_id=row["principal_id"],
        account_identity=row["account_identity"],
        source_adcp_version=row["source_adcp_version"],
        expires_at=_parse_datetime(row["expires_at"]),
        observed_request=_require_object(_loads(row["observed_request_json"])),
        observed_response=_require_object(_loads(row["observed_response_json"])),
        observed_payload_hash=row["observed_payload_hash"],
        product_ids=tuple(_loads(row["product_ids_json"])),
        projected_products=(
            tuple(_require_object(product) for product in projected)
            if projected is not None
            else None
        ),
        losses=frozenset(_loads(row["losses_json"])),
        mutation_idempotency_guaranteed=bool(row["mutation_idempotency_guaranteed"]),
        target_binding=row["target_binding"],
        listed_purchase_context=_require_object(listed) if listed is not None else None,
    )


def _decode_operation(row: sqlite3.Row) -> CompatibilityPurchaseOperation:
    result = _loads(row["result_json"])
    return CompatibilityPurchaseOperation(
        operation_id=row["operation_id"],
        principal_id=row["principal_id"],
        idempotency_key=row["idempotency_key"],
        token_hash=row["token_hash"],
        payload_hash=row["payload_hash"],
        state=CompatibilityOperationState(row["state"]),
        revision=row["revision"],
        execution_input=_require_object(_loads(row["execution_input_json"])),
        result=_require_object(result) if result is not None else None,
    )


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _state_error("stored JSON payload is not an object")
    return value


def _format_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("continuation store requires timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def _error(
    code: CompatibilityContinuationErrorCode,
    message: str,
    recovery: str,
) -> CompatibilityContinuationError:
    return CompatibilityContinuationError(code, message, recovery_guidance=recovery)


def _not_found() -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.NOT_FOUND,
        "continuation was not found for the authenticated principal",
        "Verify the principal or restart product discovery.",
    )


def _state_error(message: str) -> CompatibilityContinuationError:
    return _error(
        CompatibilityContinuationErrorCode.STORE_CONFLICT,
        message,
        "Stop mutation and inspect the durable continuation ledger.",
    )


__all__ = ["SqliteCompatibilityContinuationStore"]
