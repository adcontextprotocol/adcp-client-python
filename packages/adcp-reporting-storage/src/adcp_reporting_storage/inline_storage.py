"""PostgreSQL inline source storage: immutable staged objects and seals.

The staging and seal stores keep the producer's sealed source slices in the
same database as the ledger, under the ledger's account lock when the
PostgreSQL ledger store publishes a seal. The contract, the prepared-object
shapes and the error policy are :mod:`adcp.reporting.inline_storage`; this
module is the durable backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import TYPE_CHECKING, Any, ClassVar

from adcp.reporting.inline_source import SealedSlice
from adcp.reporting.inline_storage import (
    _DIGEST,
    INLINE_STAGING_MAX_BYTES,
    InlineStorageError,
    PreparedBackendObjectV1,
    PreparedBackendSealV1,
    _account,
    _identity,
    _object_inputs,
    _payload_digest,
    _prepared_identity,
    _redact,
    _reference,
    _seal,
)
from adcp.reporting.source import SOURCE_BATCH_MANIFEST_MAX_BYTES_V1, SourceBatchManifestReferenceV1
from adcp_reporting_storage._inline_storage_schema import REQUIRED_OBJECTS
from adcp_reporting_storage.outbox._schema import schema_objects

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

__all__ = ["PgReportingSealStore", "PgReportingStagingStore"]


class _PgStorage:
    is_durable: ClassVar[bool] = True

    def __init__(self, *, pool: AsyncConnectionPool) -> None:
        try:
            from psycopg_pool import AsyncConnectionPool as Pool
        except ImportError:
            pass
        else:
            if not isinstance(pool, Pool):
                raise InlineStorageError("INVALID_INPUT")
            self._pool = pool
            return
        raise ImportError("PostgreSQL inline storage requires adcp-reporting-storage[pg]")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<borrowed pool>)"

    async def _ready_on(self, connection: Any) -> None:
        installed = await schema_objects(connection)
        if not REQUIRED_OBJECTS or any(
            installed.get(key, {}).get(field) != value[field]
            for key, value in REQUIRED_OBJECTS.items()
            for field in ("fingerprint", "enabled")
        ):
            raise InlineStorageError("SCHEMA_UNREADY")

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Any]:
        from psycopg.errors import UndefinedTable

        async with self._pool.connection() as connection, connection.transaction():
            # DO NOTHING's conflict winner must be visible to the next SELECT,
            # regardless of a pool's default isolation level.
            await connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            try:
                await connection.execute(
                    "LOCK TABLE reporting_inline_objects, reporting_inline_seals "
                    "IN ACCESS SHARE MODE"
                )
            except UndefinedTable:
                raise InlineStorageError("SCHEMA_UNREADY") from None
            await self._ready_on(connection)
            yield connection

    @_redact
    async def create_schema(self) -> None:
        """Install the standalone additive migration and verify it atomically."""
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            await connection.execute(
                files("adcp_reporting_storage.ledger")
                .joinpath("reporting_inline_storage.sql")
                .read_text()
            )
            await self._ready_on(connection)

    @_redact
    async def check_ready(self) -> None:
        """Verify required schema objects, including constraints and write guards."""
        async with self._pool.connection() as connection, connection.transaction():
            await self._ready_on(connection)


class PgReportingStagingStore(_PgStorage):
    """Immutable account-qualified content storage using a borrowed async pool."""

    def __init__(
        self, *, pool: AsyncConnectionPool, max_payload_bytes: int = INLINE_STAGING_MAX_BYTES
    ) -> None:
        if (
            type(max_payload_bytes) is not int
            or not 1 <= max_payload_bytes <= INLINE_STAGING_MAX_BYTES
        ):
            raise InlineStorageError("INVALID_INPUT")
        super().__init__(pool=pool)
        self._max_payload_bytes = max_payload_bytes

    async def _read_on(self, connection: Any, account_id: str, digest: str) -> bytes:
        row = await (
            await connection.execute(
                "SELECT CASE WHEN octet_length(payload) <= %s THEN payload END"
                " FROM reporting_inline_objects WHERE account_id = %s AND payload_sha256 = %s",
                (self._max_payload_bytes, account_id, digest),
            )
        ).fetchone()
        if row is None:
            raise InlineStorageError("NOT_FOUND")
        payload = row[0]
        if type(payload) is not bytes or await _payload_digest(payload) != digest:
            raise InlineStorageError("INTEGRITY_FAILED")
        return payload

    async def _prepare_object(
        self, *, account_id: str, source_execution_key: str, ordinal: int, payload: bytes
    ) -> PreparedBackendObjectV1:
        _object_inputs(account_id, source_execution_key, ordinal, payload, self._max_payload_bytes)
        digest = await _payload_digest(payload)
        return PreparedBackendObjectV1(account_id, source_execution_key, ordinal, payload, digest)

    async def _stage_on(
        self, connection: Any, prepared: PreparedBackendObjectV1
    ) -> tuple[str, str]:
        """Stage on the caller's audited transaction; the returned identity is tentative.

        Caller owns account-lock ordering and the full transaction/error boundary.
        No checkout, task, transaction, commit, drain or fence is owned here.
        """
        if type(prepared) is not PreparedBackendObjectV1:
            raise InlineStorageError("INVALID_INPUT")
        prepared._validate(self._max_payload_bytes)
        await connection.execute(
            "INSERT INTO reporting_inline_objects (account_id, payload_sha256, payload)"
            " VALUES (%s, %s, %s) ON CONFLICT (account_id, payload_sha256) DO NOTHING",
            (prepared.account_id, prepared.payload_sha256, prepared.payload),
        )
        if (
            await self._read_on(connection, prepared.account_id, prepared.payload_sha256)
            != prepared.payload
        ):
            raise InlineStorageError("INTEGRITY_FAILED")
        return _reference(prepared.account_id, prepared.payload_sha256), prepared.payload_sha256

    @_redact
    async def stage(
        self, *, account_id: str, source_execution_key: str, ordinal: int, payload: bytes
    ) -> tuple[str, str]:
        prepared = await self._prepare_object(
            account_id=account_id,
            source_execution_key=source_execution_key,
            ordinal=ordinal,
            payload=payload,
        )
        async with self._transaction() as connection:
            return await self._stage_on(connection, prepared)

    @_redact
    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Mapping[str, Any],
        cancel: asyncio.Event,
    ) -> bytes:
        _account(account_id)
        if type(object_generation) is not str or _DIGEST.fullmatch(object_generation) is None:
            raise InlineStorageError("INVALID_INPUT")
        if type(object_ref) is not str or object_ref != _reference(account_id, object_generation):
            raise InlineStorageError("NOT_FOUND")
        if cancel.is_set():
            raise asyncio.CancelledError
        async with self._transaction() as connection:
            payload = await self._read_on(connection, account_id, object_generation)
        if cancel.is_set():
            raise asyncio.CancelledError
        return payload


class PgReportingSealStore(_PgStorage):
    """First committed seal wins for each trusted account and execution key."""

    async def _get_on(self, connection: Any, account_id: str, key: str) -> SealedSlice | None:
        row = await (
            await connection.execute(
                "SELECT staged_commit_ref, manifest_sha256, byte_count,"
                " CASE WHEN octet_length(manifest) <= %s THEN manifest END"
                " FROM reporting_inline_seals WHERE account_id = %s AND source_execution_key = %s",
                (SOURCE_BATCH_MANIFEST_MAX_BYTES_V1, account_id, key),
            )
        ).fetchone()
        if row is None:
            return None
        reference: SourceBatchManifestReferenceV1 | None
        try:
            reference = SourceBatchManifestReferenceV1(
                staged_commit_ref=row[0], manifest_sha256=row[1], byte_count=row[2]
            )
        except Exception:
            reference = None
        if reference is None:
            raise InlineStorageError("INTEGRITY_FAILED")
        return _seal(
            account_id,
            key,
            SealedSlice(reference=reference, manifest_bytes=row[3]),
            "INTEGRITY_FAILED",
        )

    @_redact
    async def get(self, *, account_id: str, source_execution_key: str) -> SealedSlice | None:
        _identity(account_id, source_execution_key)
        async with self._transaction() as connection:
            return await self._get_on(connection, account_id, source_execution_key)

    def _prepare_seal(
        self, *, account_id: str, source_execution_key: str, sealed: SealedSlice
    ) -> PreparedBackendSealV1:
        _prepared_identity(account_id, source_execution_key)
        candidate = _seal(account_id, source_execution_key, sealed, "INVALID_INPUT")
        reference = candidate.reference
        return PreparedBackendSealV1(
            account_id,
            source_execution_key,
            reference.staged_commit_ref,
            reference.manifest_sha256,
            reference.byte_count,
            candidate.manifest_bytes,
        )

    async def _put_on(self, connection: Any, prepared: PreparedBackendSealV1) -> SealedSlice:
        """Return the verified stored winner on the caller's audited transaction.

        The neutral value proves no admission, referenced-object existence or
        commit. Caller owns account locks, cancellation, settlement and any fence.
        """
        if type(prepared) is not PreparedBackendSealV1:
            raise InlineStorageError("INVALID_INPUT")
        prepared._validate()
        # Build a fresh reference, then revalidate it inside the existing closed
        # candidate validator. No mutable caller model is retained or trusted.
        reference = SourceBatchManifestReferenceV1.model_construct(
            staged_commit_ref=prepared.staged_commit_ref,
            manifest_sha256=prepared.manifest_sha256,
            byte_count=prepared.byte_count,
        )
        candidate = _seal(
            prepared.account_id,
            prepared.source_execution_key,
            SealedSlice(reference=reference, manifest_bytes=prepared.manifest_bytes),
            "INVALID_INPUT",
        )
        await connection.execute(
            "INSERT INTO reporting_inline_seals (account_id, source_execution_key,"
            " staged_commit_ref, manifest_sha256, byte_count, manifest)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (account_id, source_execution_key) DO NOTHING",
            (
                prepared.account_id,
                prepared.source_execution_key,
                candidate.reference.staged_commit_ref,
                candidate.reference.manifest_sha256,
                candidate.reference.byte_count,
                candidate.manifest_bytes,
            ),
        )
        winner = await self._get_on(connection, prepared.account_id, prepared.source_execution_key)
        if winner is None:
            raise InlineStorageError("INTEGRITY_FAILED")
        return winner

    @_redact
    async def put(
        self, *, account_id: str, source_execution_key: str, sealed: SealedSlice
    ) -> SealedSlice:
        prepared = self._prepare_seal(
            account_id=account_id, source_execution_key=source_execution_key, sealed=sealed
        )
        async with self._transaction() as connection:
            return await self._put_on(connection, prepared)
