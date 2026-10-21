"""Borrowed-pool PostgreSQL storage for inline objects and committed replay seals.

Create either store with an open ``psycopg_pool.AsyncConnectionPool`` and call
``create_schema()`` before use; both stores share the same additive migration.
``check_ready()`` audits required catalog objects without changing the schema.
Operations also audit it within their short transaction. Pools, credentials,
timeouts, database durability, backups and retention remain adopter-owned.
Install ``adcp[pg]`` to construct a store. Importing this module needs no driver.
Catalog fingerprints are qualified on PostgreSQL 16. Other server majors are
unqualified; catalog formatting differences may require separate qualification.

An authenticated caller supplies ``account_id``. A reference is not a credential;
``source_scope`` does not add authorization. Staging deduplicates exact bytes
within an account, independently of execution key or ordinal. Objects are capped
at 16 MiB (a constructor may lower that cap), manifests at the source contract's
1 MiB cap. No garbage collector or destructive retention operation is supplied.
Payload hashing yields between chunks of at most 1 MiB; it starts no threads.

Staging and seal insertion are separate commits. A committed seal enables exact
replay after process restart, but a crash before the seal commits can refetch,
even if an object already committed. These stores do not reserve requests before
dispatch, recover a lost provider answer, bind unseen request/routing facts, own
source leases, or compose a production service. The producer's conformance check
still validates a replay against its complete frozen request. Seal validation
does not establish that referenced objects exist or share this storage backend.
A seal write has no lease token and does not fence a stale source owner.

Private connection participants accept detached backend preparations and return
tentative references or a neutral ``SealedSlice``. Their caller owns the same-task
READ COMMITTED transaction, table locks and schema audit, account-lock ordering,
cancellation/error boundary, commit/rollback and connection return. They acquire
no connection, start no task/transaction, and provide no grant or commit proof.

Cancellation settles the borrowed connection before propagating a fresh, redacted
``CancelledError``. Cancellation or a resource error may follow a committed write;
resume with the same account/key or content identity to discover its outcome.
An active AnyIO cancellation scope retains only a fixed framework marker so its
timeout/containment semantics survive; caller text and exception chains do not.
The read cancellation event is checked before and after the transaction; cancel
the task to interrupt a pending database operation. An object above a store's
lower configured read cap fails closed with ``INTEGRITY_FAILED``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, Generic, Literal, ParamSpec, TypeVar

from anyio import CancelScope, current_effective_deadline

from adcp.reporting.inline_source import SealedSlice
from adcp.reporting.source import (
    SOURCE_BATCH_MANIFEST_MAX_BYTES_V1,
    SourceBatchManifestReferenceV1,
    parse_verified_source_batch_manifest_v1,
)

if TYPE_CHECKING:
    pass

__all__ = [
    "INLINE_STAGING_MAX_BYTES",
    "InlineStorageError",
]

INLINE_STAGING_MAX_BYTES = 16_777_216
_KEY = re.compile(r"[A-Za-z0-9_.:-]{8,255}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_Code = Literal[
    "INVALID_INPUT", "NOT_FOUND", "INTEGRITY_FAILED", "SCHEMA_UNREADY", "RESOURCE_UNAVAILABLE"
]
_MESSAGES: dict[_Code, str] = {
    "INVALID_INPUT": "inline storage input is invalid",
    "NOT_FOUND": "inline object is unavailable within the supplied account",
    "INTEGRITY_FAILED": "inline storage integrity verification failed",
    "SCHEMA_UNREADY": "inline storage schema is not ready",
    "RESOURCE_UNAVAILABLE": (
        "inline storage operation did not confirm an outcome; resume the same identity"
    ),
}


class InlineStorageError(RuntimeError):
    """Closed diagnostic; resource failures can have an unknown commit outcome."""

    def __init__(self, code: _Code) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])


P = ParamSpec("P")
T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class _Completed(Generic[T]):
    value: T


@dataclass(frozen=True, repr=False)
class _Failed:
    # None represents cancellation; outcomes never contain exception objects.
    code: _Code | None


def _cancellation_marker(error: asyncio.CancelledError) -> str | None:
    # A caller-controlled prefix alone is not evidence of a cancelled scope.
    if current_effective_deadline() != float("-inf"):
        return None
    seen: set[int] = set()
    current: BaseException | None = error
    while isinstance(current, asyncio.CancelledError) and len(seen) < 16:
        if id(current) in seen:
            break
        seen.add(id(current))
        if current.args and type(current.args[0]) is str:
            for prefix in ("Cancelled by cancel scope ", "Cancelled via cancel scope "):
                if current.args[0].startswith(prefix):
                    return prefix + "[redacted]"
        current = current.__context__
    return None


async def _payload_digest(payload: bytes) -> str:
    digest = hashlib.sha256()
    view = memoryview(payload)
    chunk_bytes = 1_048_576
    for offset in range(0, len(view), chunk_bytes):
        digest.update(view[offset : offset + chunk_bytes])
        if offset + chunk_bytes < len(view):
            await asyncio.sleep(0)
    return digest.hexdigest()


def _redact(method: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        async def run() -> _Completed[T] | _Failed:
            # A raw exception crossing a Task boundary can remain active in
            # Python 3.10's pure-Python Task wakeup frame. Return closed data so
            # even that runner cannot attach a driver error to our public error.
            try:
                return _Completed(await method(*args, **kwargs))
            except InlineStorageError as error:
                return _Failed(error.code)
            except asyncio.CancelledError:
                return _Failed(None)
            except Exception:
                return _Failed("RESOURCE_UNAVAILABLE")

        operation = asyncio.create_task(run())
        marker = None
        try:
            outcome = await asyncio.shield(operation)
        except asyncio.CancelledError as error:
            # Deliver cancellation once to the owned operation. AnyIO's repeated
            # level cancellation must not interrupt the driver's query settlement
            # and rollback. Repeated explicit Task.cancel() affects only this
            # waiter; keep joining until the borrowed resource has been returned.
            operation.cancel()
            with CancelScope(shield=True):
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                if not operation.cancelled():
                    operation.result()
                # Also leave an ambient Task wakeup exception when cancellation
                # races an already-completed operation (no join await needed).
                while True:
                    try:
                        await asyncio.sleep(0)
                        break
                    except asyncio.CancelledError:
                        continue
            marker = _cancellation_marker(error)
            outcome = _Failed(None)
        # Raise outside the handler: raw database/input exception contexts must
        # not survive even for callers that inspect __context__ directly.
        if isinstance(outcome, _Completed):
            return outcome.value
        if outcome.code is None:
            if marker is not None:
                raise asyncio.CancelledError(marker)
            raise asyncio.CancelledError
        raise InlineStorageError(outcome.code)

    return guarded


def _account(value: str) -> None:
    if type(value) is not str or not 1 <= len(value) <= 255 or "\x00" in value:
        raise InlineStorageError("INVALID_INPUT")
    # PostgreSQL text requires valid Unicode scalar values.
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InlineStorageError("INVALID_INPUT") from None


def _identity(account_id: str, key: str) -> None:
    _account(account_id)
    if type(key) is not str or _KEY.fullmatch(key) is None:
        raise InlineStorageError("INVALID_INPUT")


def _reference(account_id: str, digest: str) -> str:
    scope = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    return f"pg-inline-v1.{scope}.{digest}"


def _prepared_identity(account_id: str, key: str) -> None:
    invalid = False
    try:
        _identity(account_id, key)
    except InlineStorageError:
        invalid = True
    if invalid:
        raise InlineStorageError("INVALID_INPUT")


def _object_inputs(
    account_id: str, key: str, ordinal: int, payload: bytes, max_payload_bytes: int
) -> None:
    _prepared_identity(account_id, key)
    if type(ordinal) is not int or not 0 <= ordinal < 100_000:
        raise InlineStorageError("INVALID_INPUT")
    if type(payload) is not bytes or len(payload) > max_payload_bytes:
        raise InlineStorageError("INVALID_INPUT")


@dataclass(frozen=True, slots=True, repr=False)
class PreparedBackendObjectV1:
    """Private immutable inputs; the claimed digest still needs stored-byte verification."""

    account_id: str
    source_execution_key: str
    ordinal: int
    payload: bytes
    payload_sha256: str

    def __post_init__(self) -> None:
        self._validate(INLINE_STAGING_MAX_BYTES)

    def _validate(self, max_payload_bytes: int) -> None:
        _object_inputs(
            self.account_id,
            self.source_execution_key,
            self.ordinal,
            self.payload,
            max_payload_bytes,
        )
        if type(self.payload_sha256) is not str or _DIGEST.fullmatch(self.payload_sha256) is None:
            raise InlineStorageError("INVALID_INPUT")

    def __repr__(self) -> str:
        return "PreparedBackendObjectV1(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class PreparedBackendSealV1:
    """Private detached scalars/bytes, not admission provenance or a committed seal."""

    account_id: str
    source_execution_key: str
    staged_commit_ref: str
    manifest_sha256: str
    byte_count: int
    manifest_bytes: bytes

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _prepared_identity(self.account_id, self.source_execution_key)
        if (
            type(self.staged_commit_ref) is not str
            or not 1 <= len(self.staged_commit_ref) <= 255
            or type(self.manifest_sha256) is not str
            or _DIGEST.fullmatch(self.manifest_sha256) is None
            or type(self.byte_count) is not int
            or not 1 <= self.byte_count <= SOURCE_BATCH_MANIFEST_MAX_BYTES_V1
            or type(self.manifest_bytes) is not bytes
            or len(self.manifest_bytes) != self.byte_count
        ):
            raise InlineStorageError("INVALID_INPUT")

    def __repr__(self) -> str:
        return "PreparedBackendSealV1(<redacted>)"


@dataclass(frozen=True, repr=False)
class _StoredSeal(SealedSlice):
    def __repr__(self) -> str:
        return "SealedSlice(<redacted>)"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SealedSlice):
            return NotImplemented
        return self.reference == other.reference and self.manifest_bytes == other.manifest_bytes


def _seal(account_id: str, key: str, sealed: SealedSlice, code: _Code) -> SealedSlice:
    try:
        if not isinstance(sealed, SealedSlice) or type(sealed.manifest_bytes) is not bytes:
            raise ValueError
        # Revalidate even model_construct/model_copy inputs; never normalize the
        # retained bytes or trust the caller's mutable instance.
        reference = SourceBatchManifestReferenceV1.model_validate(sealed.reference.model_dump())
        raw = sealed.manifest_bytes
        manifest = parse_verified_source_batch_manifest_v1(reference, raw)
        if (
            manifest.identity.account_id != account_id
            or manifest.identity.source_execution_key != key
        ):
            raise ValueError
        return _StoredSeal(reference=reference, manifest_bytes=raw)
    except Exception:
        failure = InlineStorageError(code)
    raise failure
