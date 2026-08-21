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
    losses_json TEXT NOT NULL,
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
        state IN ('claimed', 'in_flight', 'succeeded', 'ambiguous')
    ),
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
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        await asyncio.to_thread(self._put_continuation, continuation)

    def _put_continuation(self, value: LegacyPurchaseContinuation) -> None:
        now = _format_datetime(self._clock())
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO adcp_compat_continuations (
                        token_hash, principal_id, account_identity,
                        source_adcp_version, expires_at, observed_request_json,
                        observed_response_json, observed_payload_hash,
                        product_ids_json, losses_json, target_binding,
                        listed_purchase_context_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        _dumps(sorted(value.losses)),
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
        now: datetime,
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._claim,
            token_hash,
            principal_id,
            idempotency_key,
            payload_hash,
            now,
        )

    def _claim(
        self,
        token_hash: str,
        principal_id: str,
        idempotency_key: str,
        payload_hash: str,
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
                    payload_hash, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    principal_id,
                    idempotency_key,
                    token_hash,
                    payload_hash,
                    CompatibilityOperationState.CLAIMED.value,
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
            )

    async def mark_in_flight(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            operation,
            {CompatibilityOperationState.CLAIMED},
            CompatibilityOperationState.IN_FLIGHT,
            None,
        )

    async def mark_ambiguous(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            operation,
            {CompatibilityOperationState.CLAIMED, CompatibilityOperationState.IN_FLIGHT},
            CompatibilityOperationState.AMBIGUOUS,
            None,
        )

    async def complete(
        self,
        operation: CompatibilityPurchaseOperation,
        result: Mapping[str, Any],
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            operation,
            {CompatibilityOperationState.IN_FLIGHT, CompatibilityOperationState.AMBIGUOUS},
            CompatibilityOperationState.SUCCEEDED,
            dict(result),
        )

    async def resume_after_not_applied(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatibilityPurchaseOperation:
        return await asyncio.to_thread(
            self._transition,
            operation,
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
            ):
                raise _state_error("operation binding changed in continuation store")
            if current.state not in allowed:
                raise _state_error(
                    f"cannot transition operation from {current.state.value} to {target.value}"
                )
            result_json = _dumps(result) if result is not None else None
            updated_at = _format_datetime(self._clock())
            updated = conn.execute(
                """
                UPDATE adcp_compat_operations
                SET state = ?, result_json = ?, updated_at = ?
                WHERE operation_id = ? AND state = ?
                """,
                (
                    target.value,
                    result_json,
                    updated_at,
                    current.operation_id,
                    current.state.value,
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
                result=result,
            )

    async def purge_resolved_before(self, cutoff: datetime) -> int:
        """Delete only old succeeded or never-claimed continuations.

        Claimed, in-flight, and ambiguous operations are deliberately retained
        regardless of age because deleting them could permit an unsafe replay.
        Returns the number of continuation records removed.
        """

        return await asyncio.to_thread(self._purge_resolved_before, cutoff)

    def _purge_resolved_before(self, cutoff: datetime) -> int:
        cutoff_utc = _as_utc(cutoff)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT
                    continuation.token_hash,
                    continuation.expires_at,
                    continuation.updated_at AS continuation_updated_at,
                    operation.state,
                    operation.updated_at AS operation_updated_at
                FROM adcp_compat_continuations AS continuation
                LEFT JOIN adcp_compat_operations AS operation
                  ON operation.token_hash = continuation.token_hash
                WHERE operation.state = 'succeeded'
                   OR operation.operation_id IS NULL
                """,
            ).fetchall()
            token_hashes = [
                row["token_hash"]
                for row in rows
                if (
                    row["state"] == CompatibilityOperationState.SUCCEEDED.value
                    and _parse_datetime(row["operation_updated_at"]) < cutoff_utc
                )
                or (
                    row["state"] is None
                    and _parse_datetime(row["expires_at"]) < cutoff_utc
                    and _parse_datetime(row["continuation_updated_at"]) < cutoff_utc
                )
            ]
            if not token_hashes:
                return 0
            placeholders = ",".join("?" for _ in token_hashes)
            conn.execute(
                f"DELETE FROM adcp_compat_operations WHERE token_hash IN ({placeholders})",
                token_hashes,
            )
            deleted = conn.execute(
                f"DELETE FROM adcp_compat_continuations WHERE token_hash IN ({placeholders})",
                token_hashes,
            ).rowcount
            conn.commit()
            return deleted


def _decode_continuation(row: sqlite3.Row) -> LegacyPurchaseContinuation:
    listed = _loads(row["listed_purchase_context_json"])
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
        losses=frozenset(_loads(row["losses_json"])),
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
