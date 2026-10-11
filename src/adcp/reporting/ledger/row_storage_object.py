"""Object-storage providers for revision row chunks.

Shared SDK persistence spec, adcontextprotocol/adcp#7996 sections 3.3 and 3.4. A
provider is a thin, credential-free port over one object store. It never decides
where bytes go: keys are rendered by the SDK from an allowlisted binding, and every
byte a provider returns is verified against digests committed in PostgreSQL before
a row is released.

The filesystem provider ships here for tests and single-host deployments. Cloud
providers live in their own optional modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

import rfc8785

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError

__all__ = [
    "REPORTING_ROW_DEFAULT_KEY_TEMPLATE",
    "REPORTING_ROW_KEY_PLACEHOLDERS",
    "ReportingRowObjectContextV1",
    "ReportingRowObjectDeleteInputV1",
    "ReportingRowObjectGetInputV1",
    "ReportingRowObjectProviderV1",
    "ReportingRowObjectPutInputV1",
    "ReportingRowObjectPutResultV1",
    "assert_reporting_row_key_template_v1",
    "assert_reporting_row_prefix_v1",
    "create_filesystem_reporting_row_object_provider_v1",
    "render_reporting_row_object_key_v1",
    "reporting_row_account_key_v1",
    "reporting_row_revision_key_v1",
    "run_reporting_row_object_provider_conformance_v1",
]


@dataclass(frozen=True, slots=True)
class ReportingRowObjectContextV1:
    """Per-call limits. ``timeout_seconds`` bounds one provider operation."""

    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ReportingRowObjectPutInputV1:
    #: Non-secret provider coordinates (bucket, container, filesystem root name).
    location: Mapping[str, str]
    key: str
    data: bytes
    content_type: Literal["application/x-ndjson", "application/gzip"]
    #: Non-secret object metadata (installation and intent identifiers).
    metadata: Mapping[str, str]
    #: Host-mapped client reference; never a secret.
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRowObjectPutResultV1:
    created: bool
    native_version: str


@dataclass(frozen=True, slots=True)
class ReportingRowObjectGetInputV1:
    location: Mapping[str, str]
    key: str
    #: Exact native version recorded at write time.
    native_version: str
    #: Hard cap on bytes read; providers abort beyond it.
    max_bytes: int
    #: ``(offset, length)`` within the stored object; ``None`` reads it whole.
    byte_range: tuple[int, int] | None = None
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRowObjectDeleteInputV1:
    location: Mapping[str, str]
    key: str
    native_version: str
    credential_ref: str | None = None


@runtime_checkable
class ReportingRowObjectProviderV1(Protocol):
    """The port a row object store implements.

    Native versions must identify exactly one write: an ETag is content-derived
    and does not qualify, because an identical upload re-created at the same key
    has the same ETag and a delayed delete would remove the replacement.
    """

    #: Registry name, for example ``filesystem``, ``gcs``, ``s3`` or ``azure_blob``.
    name: str
    #: Whether ``get`` honours ``byte_range`` without reading the whole object.
    ranged_reads: bool

    def validate_location(self, location: Mapping[str, str]) -> None:
        """Validate binding coordinates against the provider's closed schema.

        Raise ``INVALID_INPUT`` for unknown fields or values that could carry secrets.
        """

    async def probe(
        self,
        location: Mapping[str, str],
        prefix: str,
        context: ReportingRowObjectContextV1,
        credential_ref: str | None = None,
    ) -> None:
        """Fail closed (``UNSAFE_BINDING``) when the destination is unsafe."""

    async def put_if_absent(
        self, put: ReportingRowObjectPutInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectPutResultV1:
        """Create-only write. Must never replace an existing object.

        Returns ``created=True`` with the new native version, or ``created=False``
        with the existing object's version when the key is taken.
        """

    async def get(
        self, get: ReportingRowObjectGetInputV1, context: ReportingRowObjectContextV1
    ) -> bytes | None:
        """Bytes at the exact native version, or ``None`` when that version is absent."""

    async def delete(
        self, delete: ReportingRowObjectDeleteInputV1, context: ReportingRowObjectContextV1
    ) -> Literal["deleted", "absent"]:
        """Delete the exact native version. Idempotent: ``absent`` is success."""


#: Placeholders allowed in object key templates.
REPORTING_ROW_KEY_PLACEHOLDERS: tuple[str, ...] = (
    "prefix",
    "namespace_key",
    "account_key",
    "period_date",
    "finality",
    "revision_key",
    "content_sha256",
    "chunk_index",
)

REPORTING_ROW_DEFAULT_KEY_TEMPLATE = (
    "{prefix}/{namespace_key}/{account_key}/{period_date}/{finality}/{revision_key}"
    "/{content_sha256}/{chunk_index}"
)

_KEY = re.compile(r"^[A-Za-z0-9_.=-]+(?:/[A-Za-z0-9_.=-]+)*$")
_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def assert_reporting_row_key_template_v1(template: str) -> None:
    """Validate an object key template; raises ``INVALID_INPUT``."""
    if not template.startswith("{prefix}/{namespace_key}/"):
        raise ReportingRowStoreError(
            "INVALID_INPUT", "key templates must begin with {prefix}/{namespace_key}/"
        )
    for required in ("{content_sha256}", "{chunk_index}"):
        if required not in template:
            raise ReportingRowStoreError("INVALID_INPUT", f"key templates must include {required}")
    unknown = [
        name
        for name in _PLACEHOLDER.findall(template)
        if name not in REPORTING_ROW_KEY_PLACEHOLDERS
    ]
    if unknown:
        raise ReportingRowStoreError(
            "INVALID_INPUT", f"unknown key template placeholders: {', '.join(unknown)}"
        )
    literal = _PLACEHOLDER.sub("x", template)
    if not _KEY.match(literal) or any(segment in (".", "..") for segment in literal.split("/")):
        raise ReportingRowStoreError(
            "INVALID_INPUT", "key template contains unsafe characters or segments"
        )


def assert_reporting_row_prefix_v1(prefix: str) -> None:
    if (
        not _KEY.match(prefix)
        or len(prefix) > 512
        or any(segment in (".", "..") for segment in prefix.split("/"))
    ):
        raise ReportingRowStoreError("INVALID_INPUT", "row binding prefix is invalid")


def _sha256_hex(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def reporting_row_account_key_v1(namespace_key: str, account_id: str) -> str:
    """Pseudonymous account segment; never a raw account ID."""
    return _sha256_hex(rfc8785.dumps(["adcp.account_key.v1", namespace_key, account_id]))


def reporting_row_revision_key_v1(row_set_id: str) -> str:
    return _sha256_hex(rfc8785.dumps(["adcp.revision_key.v1", row_set_id]))


def _period_date(period_start: str | None) -> str:
    if not period_start:
        return "undated"
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
    except ValueError:
        return "undated"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y/%m/%d")


def render_reporting_row_object_key_v1(
    *,
    template: str,
    prefix: str,
    namespace_key: str,
    account_id: str,
    row_set_id: str,
    period_start: str | None,
    finality: Literal["snapshot", "official"] | None,
    content_sha256: str,
    chunk_index: int,
    compression: Literal["none", "gzip"],
) -> str:
    """Render one chunk's object key and confine it to the binding's owned prefix."""
    values = {
        "prefix": prefix,
        "namespace_key": namespace_key,
        "account_key": reporting_row_account_key_v1(namespace_key, account_id),
        "period_date": _period_date(period_start),
        "finality": finality or "adjustment",
        "revision_key": reporting_row_revision_key_v1(row_set_id),
        "content_sha256": content_sha256,
        "chunk_index": str(chunk_index).zfill(6),
    }
    key = _PLACEHOLDER.sub(lambda match: values[match.group(1)], template) + (
        ".jsonl.gz" if compression == "gzip" else ".jsonl"
    )
    if (
        not _KEY.match(key)
        or any(segment in (".", "..") for segment in key.split("/"))
        or not key.startswith(f"{prefix}/{namespace_key}/")
    ):
        raise ReportingRowStoreError(
            "INVALID_INPUT", "rendered object key escapes the binding prefix"
        )
    return key


# -- statx ---------------------------------------------------------------


def _statx_birth_time_ns(path: str) -> int | None:
    """Linux ``statx`` birth time, or ``None`` when it cannot be read.

    ``ctypes`` is imported here, not at module load: only the test and single-host
    filesystem provider needs it, and a Python build without ``_ctypes`` must still import
    the ledger.
    """
    import ctypes

    class Timespec(ctypes.Structure):
        _fields_ = [
            ("tv_sec", ctypes.c_int64),
            ("tv_nsec", ctypes.c_uint32),
            ("_pad", ctypes.c_int32),
        ]

    class Statx(ctypes.Structure):
        _fields_ = [
            ("stx_mask", ctypes.c_uint32),
            ("stx_blksize", ctypes.c_uint32),
            ("stx_attributes", ctypes.c_uint64),
            ("stx_nlink", ctypes.c_uint32),
            ("stx_uid", ctypes.c_uint32),
            ("stx_gid", ctypes.c_uint32),
            ("stx_mode", ctypes.c_uint16),
            ("_pad1", ctypes.c_uint16),
            ("stx_ino", ctypes.c_uint64),
            ("stx_size", ctypes.c_uint64),
            ("stx_blocks", ctypes.c_uint64),
            ("stx_attributes_mask", ctypes.c_uint64),
            ("stx_atime", Timespec),
            ("stx_btime", Timespec),
            ("stx_ctime", Timespec),
            ("stx_mtime", Timespec),
            ("_rest", ctypes.c_uint8 * 128),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    buffer = Statx()
    if (
        libc.statx(
            _AT_FDCWD, os.fsencode(path), 0, _STATX_BASIC_STATS_AND_BTIME, ctypes.byref(buffer)
        )
        != 0
    ):
        return None
    return int(buffer.stx_btime.tv_sec) * 1_000_000_000 + int(buffer.stx_btime.tv_nsec)


_AT_FDCWD = -100
_STATX_BASIC_STATS_AND_BTIME = 0x7FF | 0x800


def _birth_time_ns(path: str, fallback: os.stat_result) -> int:
    """Birth time in nanoseconds, matching Node's ``birthtimeNs`` on Linux.

    Filesystem versions are shared with the JavaScript SDK, which reads birth
    time through ``statx``. ``os.stat`` does not expose it on Linux, so read it
    the same way. A filesystem that records none reports zero, as Node does.
    """
    if sys.platform.startswith("linux"):
        try:
            birth_ns = _statx_birth_time_ns(path)
        except (ImportError, OSError, AttributeError):
            birth_ns = None
        if birth_ns is not None:
            return birth_ns
    birth = getattr(fallback, "st_birthtime_ns", None)
    if birth is None:
        seconds = getattr(fallback, "st_birthtime", None)
        birth = int(seconds * 1_000_000_000) if seconds is not None else 0
    return int(birth)


def _filesystem_version(path: str) -> str:
    stat = os.stat(path)
    return f"{stat.st_ino}:{_birth_time_ns(path, stat)}:{stat.st_mtime_ns}:{stat.st_size}"


# -- filesystem provider ---------------------------------------------------

_ROOT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(slots=True)
class _FilesystemProvider:
    roots: dict[str, str]
    name: str = "filesystem"
    ranged_reads: bool = True

    def validate_location(self, location: Mapping[str, str]) -> None:
        if set(location) != {"root"} or location["root"] not in self.roots:
            raise ReportingRowStoreError(
                "INVALID_INPUT", "filesystem locations name exactly one configured root"
            )

    def _resolve(self, location: Mapping[str, str], key: str) -> str:
        root = self.roots.get(location.get("root", ""))
        if root is None:
            raise ReportingRowStoreError("UNSAFE_BINDING", "filesystem root is not configured")
        target = os.path.normpath(os.path.join(root, key))
        if not target.startswith(root + os.sep):
            raise ReportingRowStoreError("INVALID_INPUT", "object key escapes the filesystem root")
        return target

    async def probe(
        self,
        location: Mapping[str, str],
        prefix: str,
        context: ReportingRowObjectContextV1,
        credential_ref: str | None = None,
    ) -> None:
        root = self.roots.get(location.get("root", ""))
        if root is None:
            raise ReportingRowStoreError("UNSAFE_BINDING", "filesystem root is not configured")
        await asyncio.to_thread(os.makedirs, root, 0o700, True)

    async def put_if_absent(
        self, put: ReportingRowObjectPutInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectPutResultV1:
        target = self._resolve(put.location, put.key)
        return await asyncio.to_thread(self._put, target, put.data)

    @staticmethod
    def _put(target: str, data: bytes) -> ReportingRowObjectPutResultV1:
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        temporary = f"{target}.{uuid.uuid4()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(data)
                    handle.flush()
                    # Write-unique version: stamp a random sub-millisecond mtime so
                    # an identical re-creation inside one coarse tick still differs.
                    stamp = (int(time.time() * 1000) * 1_000_000) + secrets.randbelow(1000) * 1000
                    os.utime(handle.fileno(), ns=(stamp, stamp))
                    os.fsync(handle.fileno())
            except OSError:
                raise ReportingRowStoreError(
                    "PROVIDER_UNAVAILABLE", "filesystem write failed"
                ) from None
            try:
                os.link(temporary, target)
            except FileExistsError:
                return ReportingRowObjectPutResultV1(False, _filesystem_version(target))
            except OSError:
                raise ReportingRowStoreError(
                    "PROVIDER_UNAVAILABLE", "filesystem write failed"
                ) from None
            return ReportingRowObjectPutResultV1(True, _filesystem_version(target))
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def get(
        self, get: ReportingRowObjectGetInputV1, context: ReportingRowObjectContextV1
    ) -> bytes | None:
        target = self._resolve(get.location, get.key)
        return await asyncio.to_thread(self._get, target, get)

    @staticmethod
    def _get(target: str, get: ReportingRowObjectGetInputV1) -> bytes | None:
        try:
            descriptor = os.open(target, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except OSError:
            raise ReportingRowStoreError("PROVIDER_UNAVAILABLE", "filesystem read failed") from None
        with os.fdopen(descriptor, "rb") as handle:
            stat = os.fstat(handle.fileno())
            version = (
                f"{stat.st_ino}:{_birth_time_ns(target, stat)}:{stat.st_mtime_ns}:{stat.st_size}"
            )
            if version != get.native_version:
                return None
            size = stat.st_size
            offset, length = get.byte_range if get.byte_range else (0, size)
            if offset < 0 or length < 0 or offset + length > size:
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "requested range exceeds the stored object"
                )
            if length > get.max_bytes:
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "stored object exceeds its recorded size"
                )
            handle.seek(offset)
            return handle.read(length)

    async def delete(
        self, delete: ReportingRowObjectDeleteInputV1, context: ReportingRowObjectContextV1
    ) -> Literal["deleted", "absent"]:
        target = self._resolve(delete.location, delete.key)
        return await asyncio.to_thread(self._delete, target, delete.native_version)

    @staticmethod
    def _delete(target: str, native_version: str) -> Literal["deleted", "absent"]:
        try:
            if _filesystem_version(target) != native_version:
                return "absent"
            os.unlink(target)
            return "deleted"
        except FileNotFoundError:
            return "absent"
        except OSError:
            raise ReportingRowStoreError(
                "PROVIDER_UNAVAILABLE", "filesystem delete failed"
            ) from None


def create_filesystem_reporting_row_object_provider_v1(
    *, roots: Mapping[str, str | Path]
) -> ReportingRowObjectProviderV1:
    """Local filesystem provider for tests and single-host deployments.

    ``roots`` maps a root name to an absolute directory. Bindings name a root
    (``location={"root": name}``); they never carry paths, so the database cannot
    point writes elsewhere. Create-only writes use a temporary file, ``fsync`` and
    ``link()`` to the final name, which fails instead of replacing an object.

    The native version is ``inode:birth_ns:mtime_ns:size``, the format the
    JavaScript provider uses, so either SDK reads the other's locators. It is
    write-unique only on a filesystem with sub-second timestamps (ext4, XFS,
    APFS, NTFS); ``write-unique-version`` in the conformance suite fails on one
    that is too coarse. Use it for tests and single-host development.
    """
    resolved: dict[str, str] = {}
    for name, directory in roots.items():
        path = os.fspath(directory)
        if not _ROOT_NAME.match(name) or not os.path.isabs(path):
            raise ReportingRowStoreError(
                "INVALID_INPUT", "filesystem roots need a simple name and an absolute path"
            )
        resolved[name] = os.path.normpath(path)
    return _FilesystemProvider(resolved)


async def run_reporting_row_object_provider_conformance_v1(
    provider: ReportingRowObjectProviderV1,
    *,
    location: Mapping[str, str],
    prefix: str,
    credential_ref: str | None = None,
) -> list[str]:
    """Behavioural conformance for an object provider.

    Writes and deletes only under ``prefix``; run it against a dedicated test
    bucket or prefix. Returns the checks that passed and raises
    ``AssertionError`` on the first violation.
    """
    assert_reporting_row_prefix_v1(prefix)
    provider.validate_location(location)
    context = ReportingRowObjectContextV1(timeout_seconds=60.0)
    passed: list[str] = []
    key = f"{prefix}/conformance/{uuid.uuid4()}.jsonl"
    first = b'{"a":1}\n{"b":2}\n'
    metadata = {"adcp-installation": "conformance", "adcp-intent": "conformance"}

    def put(data: bytes) -> ReportingRowObjectPutInputV1:
        return ReportingRowObjectPutInputV1(
            location, key, data, "application/x-ndjson", metadata, credential_ref
        )

    def get(
        version: str, *, byte_range: tuple[int, int] | None = None
    ) -> ReportingRowObjectGetInputV1:
        return ReportingRowObjectGetInputV1(
            location, key, version, 1024, byte_range, credential_ref
        )

    def delete(version: str) -> ReportingRowObjectDeleteInputV1:
        return ReportingRowObjectDeleteInputV1(location, key, version, credential_ref)

    await provider.probe(location, prefix, context, credential_ref)
    passed.append("probe")
    created = await provider.put_if_absent(put(first), context)
    assert created.created and created.native_version, "put_if_absent did not create a new object"
    passed.append("create")
    conflict = await provider.put_if_absent(put(b'{"c":3}\n'), context)
    assert (
        not conflict.created and conflict.native_version == created.native_version
    ), "put_if_absent replaced or re-versioned an existing object"
    stored = await provider.get(get(created.native_version), context)
    assert stored == first, "create-only write did not preserve the first bytes"
    passed.append("create-only")
    if provider.ranged_reads:
        ranged = await provider.get(get(created.native_version, byte_range=(8, 8)), context)
        assert ranged == b'{"b":2}\n', "ranged read returned wrong bytes"
        passed.append("ranged-read")
    missing = await provider.get(get("not-a-version"), context)
    assert missing is None, "get returned bytes for a version that does not exist"
    passed.append("version-pinned-read")
    outcome = await provider.delete(delete("not-a-version"), context)
    assert outcome == "absent", "delete removed an object at a different version"
    outcome = await provider.delete(delete(created.native_version), context)
    assert outcome == "deleted", "delete did not remove the exact version"
    outcome = await provider.delete(delete(created.native_version), context)
    assert outcome == "absent", "repeated delete was not idempotent"
    passed.append("exact-version-delete")
    # A delayed delete of an old version must never remove a later write at the same key,
    # even one with identical bytes: native versions are unique per write, never
    # content-derived.
    recreated = await provider.put_if_absent(put(first), context)
    assert recreated.created and recreated.native_version, "put_if_absent did not re-create"
    assert (
        recreated.native_version != created.native_version
    ), "re-creating identical bytes produced the same native version; versions must be write-unique"
    outcome = await provider.delete(delete(created.native_version), context)
    assert outcome == "absent", "delete of a stale version removed or reported a later write"
    survivor = await provider.get(get(recreated.native_version), context)
    assert survivor == first, "delete of a stale version removed a later write with identical bytes"
    outcome = await provider.delete(delete(recreated.native_version), context)
    assert outcome == "deleted", "delete did not remove the re-created version"
    passed.append("write-unique-version")
    return passed
