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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import rfc8785

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError

__all__ = [
    "ReportingRowObjectListEntryV1",
    "ReportingRowObjectListInputV1",
    "ReportingRowObjectListPageV1",
    "REPORTING_ROW_BINDING_ID_MAX_LENGTH",
    "REPORTING_ROW_CREDENTIAL_REF_MAX_LENGTH",
    "REPORTING_ROW_DEFAULT_KEY_TEMPLATE",
    "REPORTING_ROW_KEY_PLACEHOLDERS",
    "REPORTING_ROW_NATIVE_VERSION_REF_MAX_LENGTH",
    "REPORTING_ROW_OBJECT_REF_MAX_LENGTH",
    "ReportingRowObjectContextV1",
    "ReportingRowObjectDeleteInputV1",
    "ReportingRowObjectGetInputV1",
    "ReportingRowObjectProviderV1",
    "ReportingRowObjectPutInputV1",
    "ReportingRowObjectPutResultV1",
    "ReportingRowObjectStatInputV1",
    "ReportingRowObjectStatResultV1",
    "assert_reporting_row_key_template_v1",
    "assert_reporting_row_prefix_v1",
    "create_filesystem_reporting_row_object_provider_v1",
    "render_reporting_row_object_key_v1",
    "reporting_period_date_v1",
    "reporting_row_account_key_v1",
    "reporting_row_revision_key_v1",
    "run_reporting_row_object_provider_conformance_v1",
]


@dataclass(frozen=True, slots=True)
class ReportingRowObjectContextV1:
    """Per-call limits. ``timeout_seconds`` bounds one provider operation."""

    timeout_seconds: float = 30.0


#: Bounds AdCP puts on ``object_ref`` and ``native_version_ref``
#: (``reporting-file-object-ref.json``, ``reporting-native-version-ref.json``): 1 to 1024
#: characters. The row-storage ``CHECK`` constraints enforce the same.
REPORTING_ROW_OBJECT_REF_MAX_LENGTH = 1024
REPORTING_ROW_NATIVE_VERSION_REF_MAX_LENGTH = 1024
#: Bound on a row binding ID, 1 to 255 characters of ``A-Za-z0-9_.:-``: the AdCP identifier
#: bound that ``reporting_revision_id``, ``resource_ref`` and ``destination_ref`` use
#: (``^[A-Za-z0-9_.:-]{1,255}$``). The ``adcp_reporting_row_bindings`` ``CHECK`` enforces the same.
REPORTING_ROW_BINDING_ID_MAX_LENGTH = 255
#: Bound on a binding's ``credential_ref`` (and on the keys of a provider's ``clients`` map): the
#: same 1 to 255 characters of ``A-Za-z0-9_.:-`` as a row binding ID.
REPORTING_ROW_CREDENTIAL_REF_MAX_LENGTH = 255


@dataclass(frozen=True, slots=True)
class ReportingRowObjectPutInputV1:
    #: Non-secret provider coordinates (bucket, container, filesystem root name).
    location: Mapping[str, str]
    #: AdCP ``object_ref``: the decoded, destination-relative object key.
    object_ref: str
    data: bytes
    content_type: Literal["application/x-ndjson", "application/gzip"]
    #: Non-secret object metadata (installation and intent identifiers).
    metadata: Mapping[str, str]
    #: Host-mapped client reference; never a secret.
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRowObjectPutResultV1:
    created: bool
    native_version_ref: str


@dataclass(frozen=True, slots=True)
class ReportingRowObjectGetInputV1:
    location: Mapping[str, str]
    object_ref: str
    #: AdCP ``native_version_ref``: the exact native version recorded at write time.
    native_version_ref: str
    #: Hard cap on bytes read; providers abort beyond it.
    max_bytes: int
    #: ``(offset, length)`` within the stored object; ``None`` reads it whole.
    byte_range: tuple[int, int] | None = None
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRowObjectDeleteInputV1:
    location: Mapping[str, str]
    object_ref: str
    native_version_ref: str
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRowObjectStatInputV1:
    location: Mapping[str, str]
    object_ref: str
    native_version_ref: str
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRowObjectStatResultV1:
    #: The version the provider resolved; it must equal the one asked for.
    native_version_ref: str
    #: AdCP ``size_bytes``: the stored object's size.
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ReportingRowObjectListInputV1:
    location: Mapping[str, str]
    #: Key prefix to list under. The SDK passes the binding's owned prefix,
    #: ``{prefix}/{namespace_key}/``.
    prefix: str
    #: Upper bound on objects in the page. A provider may return fewer and still return a token.
    page_size: int
    #: Opaque continuation from the previous page; ``None`` for the first page.
    page_token: str | None = None
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRowObjectListEntryV1:
    """One stored object version found by a listing."""

    #: AdCP ``object_ref``: the key, as it would be passed to ``get`` and ``delete``.
    object_ref: str
    #: AdCP ``native_version_ref``: the exact version, as ``put_if_absent`` would have recorded it.
    native_version_ref: str
    size_bytes: int
    #: When this version was written, as an RFC 3339 UTC instant (``...Z``). Never earlier than
    #: the write.
    created_at: str


@dataclass(frozen=True, slots=True)
class ReportingRowObjectListPageV1:
    objects: tuple[ReportingRowObjectListEntryV1, ...]
    #: Absent on the last page.
    next_page_token: str | None = None
    #: Entries the provider left out because they cannot be addressed or aged (no usable version,
    #: size or write time). They are never collected; one bad entry must not stop the listing.
    skipped: int = 0


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

    # Optional, so it is not part of the runtime-checkable port:
    #
    #   async def stat(self, stat: ReportingRowObjectStatInputV1, context)
    #       -> ReportingRowObjectStatResultV1 | None
    #
    # Metadata of the exact native version without reading its bytes: the version it resolved
    # (which must equal the requested one) and its stored size, or ``None`` when that version
    # is absent. The commit transaction uses it to prove an adopted object unchanged (native
    # versions are write-unique, so the same version at the recorded size is the same bytes)
    # without downloading it under the account lock. A provider without ``stat`` falls back to
    # a full read there. Never return a size for a different version.
    #
    #   async def list(self, request: ReportingRowObjectListInputV1, context)
    #       -> ReportingRowObjectListPageV1
    #
    # Every stored version whose key starts with ``prefix``, in a stable order, one page at a
    # time. The orphan collector (``collect_orphaned_row_objects``) and its ownership fence use
    # it; a binding whose provider has no ``list`` is reported ``unsupported`` there. Return each
    # version once as ``(object_ref, native_version_ref)`` exactly as ``put_if_absent`` recorded
    # it (on a versioned store, noncurrent versions too, never delete markers), the stored size
    # and the time the version was written. Return only keys under ``prefix``. A page may be
    # short; ``next_page_token`` is absent on the last page. A token is only valid for the same
    # prefix and location. Leave out an entry that cannot be addressed or aged and count it in
    # ``skipped`` rather than failing the page. The listing must be strongly consistent for keys
    # ``put_if_absent`` has already returned (read-after-write): the collector's fence reads the
    # newest claim by listing.


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
_INSTANT = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?Z"
)


def is_row_object_key_shape(
    key: object, max_length: int = REPORTING_ROW_OBJECT_REF_MAX_LENGTH
) -> bool:
    """Whether a key has the shape ``render_reporting_row_object_key_v1`` gives every row object.

    The same character set, no empty, ``.`` or ``..`` segment, and within AdCP's ``object_ref``
    bound. ``fullmatch``: ``$`` would also accept a trailing newline.
    """
    return (
        isinstance(key, str)
        and len(key) <= max_length
        and _KEY.fullmatch(key) is not None
        and not any(segment in (".", "..") for segment in key.split("/"))
    )


def parse_row_instant(value: object) -> datetime | None:
    """A strict RFC 3339 UTC instant (``2026-10-01T00:00:00.123Z``) as an aware datetime.

    Anything else, including an offset other than ``Z``, a date-only value or an impossible
    date, is ``None``: an object whose write time cannot be read is treated as young, never
    deleted on a guess.
    """
    if not isinstance(value, str):
        return None
    match = _INSTANT.fullmatch(value)
    if match is None:
        return None
    fraction = (match.group(2) or "").ljust(6, "0")[:6]
    try:
        return datetime.fromisoformat(f"{match.group(1)}.{fraction}+00:00")
    except ValueError:
        return None


def is_row_instant(value: object) -> bool:
    return parse_row_instant(value) is not None


def format_row_instant(moment: datetime) -> str:
    """An aware datetime as the millisecond RFC 3339 UTC instant providers report."""
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


_REVISION_KEY_REQUIRED = (
    "key templates must include {revision_key} so every row set has unique object keys. "
    "A binding identity includes its template, so add it under a NEW binding ID and keep the "
    "old definition configured with read_only=True while rows reference it (rows already "
    "written stay readable at their recorded object_ref)"
)


def assert_reporting_row_key_template_v1(
    template: str, *, require_revision_key: bool = True
) -> None:
    """Validate an object key template; raises ``INVALID_INPUT``.

    Every template that writes must include ``{revision_key}``, so no two row sets can ever name
    the same object key; ``require_revision_key=False`` is for a read-only binding that only
    reads rows written under an older template.
    """
    if not template.startswith("{prefix}/{namespace_key}/"):
        raise ReportingRowStoreError(
            "INVALID_INPUT", "key templates must begin with {prefix}/{namespace_key}/"
        )
    required = ["{content_sha256}", "{chunk_index}"]
    if require_revision_key:
        required.append("{revision_key}")
    for placeholder in required:
        if placeholder not in template:
            raise ReportingRowStoreError(
                "INVALID_INPUT",
                (
                    _REVISION_KEY_REQUIRED
                    if placeholder == "{revision_key}"
                    else f"key templates must include {placeholder}"
                ),
            )
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


_PERIOD_INSTANT = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[zZ]|([+-])(\d{2}):(\d{2}))$"
)


def reporting_period_date_v1(period_start: str, source_timezone: str) -> str:
    """The calendar date, as AdCP's ``YYYY-MM-DD``, of a period's start in its ``source_timezone``.

    A period starting ``2026-10-02T07:00:00Z`` in ``America/Los_Angeles`` begins on 2026-10-02
    local time; ``2026-10-01T16:00:00Z`` is already 2026-10-02 in ``Asia/Tokyo``. The date is
    the civil date of the start instant: a period that starts on a daylight-saving boundary
    keeps the local date it begins on.

    Fails closed with ``ValueError``: an unparseable or calendar-invalid instant (February 30,
    ``T24:00``), a missing, unknown or non-IANA timezone, or a year outside 0001-9999. The host's
    own default zone is never consulted and UTC is never assumed.
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    if not isinstance(period_start, str):
        raise ValueError("period start must be an RFC 3339 instant")
    if (
        not isinstance(source_timezone, str)
        or not 1 <= len(source_timezone) <= 255
        or source_timezone[0] in "+-"
    ):
        raise ValueError("a period date requires the period source_timezone")
    match = _PERIOD_INSTANT.match(period_start)
    if match is None:
        raise ValueError("period start must be an RFC 3339 instant")
    year, month, day, hour, minute, second = (int(match.group(index)) for index in range(1, 7))
    leap_second = second == 60
    try:
        # ``datetime`` rejects what ``Date.parse`` would roll forward: February 30, T24:00, hour 25.
        base = datetime(year, month, day, hour, minute, 59 if leap_second else second)
        if match.group(7):
            hours, minutes = int(match.group(8)), int(match.group(9))
            if hours > 23 or minutes > 59:
                raise ValueError("invalid offset")
            offset = timedelta(hours=hours, minutes=minutes)
            zone_offset = timezone(offset if match.group(7) == "+" else -offset)
        else:
            zone_offset = timezone.utc
        instant = base.replace(tzinfo=zone_offset)
        if leap_second:
            instant += timedelta(seconds=1)
    except (ValueError, OverflowError):
        raise ValueError("period start must be an RFC 3339 instant") from None
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", source_timezone) or ".." in source_timezone:
        raise ValueError("source_timezone must be an IANA timezone")
    try:
        local = instant.astimezone(ZoneInfo(source_timezone))
    except (ValueError, KeyError, OSError):
        raise ValueError("source_timezone must be an IANA timezone") from None
    except OverflowError:
        raise ValueError("the period date is outside the supported year range") from None
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"


def _rendered_period_date(period_start: str | None, source_timezone: str | None) -> str:
    if not period_start:
        return "undated"
    if not source_timezone:
        raise ReportingRowStoreError(
            "INVALID_INPUT", "a period start needs its source_timezone to render {period_date}"
        )
    try:
        return reporting_period_date_v1(period_start, source_timezone)
    except ValueError:
        raise ReportingRowStoreError(
            "INVALID_INPUT",
            "period start or source_timezone is invalid; cannot render {period_date}",
        ) from None


def render_reporting_row_object_key_v1(
    *,
    template: str,
    prefix: str,
    namespace_key: str,
    account_id: str,
    row_set_id: str,
    period_start: str | None,
    source_timezone: str | None = None,
    finality: Literal["snapshot", "official"] | None,
    content_sha256: str,
    chunk_index: int,
    compression: Literal["none", "gzip"],
) -> str:
    """Render one chunk's object key and confine it to the binding's owned prefix.

    ``{period_date}`` is AdCP's ``YYYY-MM-DD``: the period start's calendar date in the period's
    ``source_timezone``, one path segment (``undated`` without a period start). It is rendered
    only when the template uses it, so a template without it needs no timezone.
    """
    values = {
        "prefix": prefix,
        "namespace_key": namespace_key,
        "account_key": reporting_row_account_key_v1(namespace_key, account_id),
        "period_date": (
            _rendered_period_date(period_start, source_timezone)
            if "{period_date}" in template
            else ""
        ),
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
    if len(key) > REPORTING_ROW_OBJECT_REF_MAX_LENGTH:
        raise ReportingRowStoreError(
            "INVALID_INPUT",
            f"rendered object key exceeds {REPORTING_ROW_OBJECT_REF_MAX_LENGTH} characters"
            " (AdCP object_ref bound)",
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
        target = self._resolve(put.location, put.object_ref)
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
        target = self._resolve(get.location, get.object_ref)
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
            if version != get.native_version_ref:
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

    async def stat(
        self, stat: ReportingRowObjectStatInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectStatResultV1 | None:
        target = self._resolve(stat.location, stat.object_ref)
        return await asyncio.to_thread(self._stat, target, stat.native_version_ref)

    @staticmethod
    def _stat(target: str, native_version_ref: str) -> ReportingRowObjectStatResultV1 | None:
        try:
            version = _filesystem_version(target)
        except FileNotFoundError:
            return None
        except OSError:
            raise ReportingRowStoreError("PROVIDER_UNAVAILABLE", "filesystem stat failed") from None
        if version != native_version_ref:
            return None
        return ReportingRowObjectStatResultV1(version, int(version.rsplit(":", 1)[1]))

    async def list(
        self, request: ReportingRowObjectListInputV1, context: ReportingRowObjectContextV1
    ) -> ReportingRowObjectListPageV1:
        """List objects under ``prefix`` in key order, one page at a time.

        Reads and sorts each directory it walks in full, so keep directories small: it is for
        tests and single hosts, not millions of objects per prefix.
        """
        if type(request.page_size) is not int or request.page_size < 1:
            raise ReportingRowStoreError("INVALID_INPUT", "page_size must be a positive integer")
        root = self.roots.get(request.location.get("root", ""))
        if root is None:
            raise ReportingRowStoreError("UNSAFE_BINDING", "filesystem root is not configured")
        if not isinstance(request.prefix, str) or ".." in request.prefix.split("/"):
            raise ReportingRowStoreError("INVALID_INPUT", "list prefix is invalid")
        return await asyncio.to_thread(self._list, root, request)

    @staticmethod
    def _list(root: str, request: ReportingRowObjectListInputV1) -> ReportingRowObjectListPageV1:
        after = None if request.page_token is None else tuple(request.page_token.split("/"))
        objects: list[ReportingRowObjectListEntryV1] = []
        more = False

        def walk(directory: tuple[str, ...]) -> bool:
            nonlocal more
            try:
                with os.scandir(os.path.join(root, *directory)) as scanner:
                    # A symlink is neither listed nor followed: a link inside the root must not
                    # make a key resolve outside it.
                    entries = sorted(
                        (entry for entry in scanner if not entry.is_symlink()),
                        key=lambda entry: entry.name,
                    )
            except (FileNotFoundError, NotADirectoryError):
                return True
            except OSError:
                raise ReportingRowStoreError(
                    "PROVIDER_UNAVAILABLE", "filesystem list failed"
                ) from None
            for entry in entries:
                name = entry.name
                segments = (*directory, name)
                path = entry.path
                is_directory = entry.is_dir(follow_symlinks=False)
                if after is not None:
                    head = after[: len(segments)]
                    if segments < head:
                        continue
                    if segments == head and not is_directory and len(segments) == len(after):
                        continue
                if is_directory:
                    # Keep only subtrees that can hold a key under the prefix.
                    rendered = "/".join(segments) + "/"
                    if not rendered.startswith(request.prefix) and not request.prefix.startswith(
                        rendered
                    ):
                        continue
                    if not walk(segments):
                        return False
                    continue
                if name.endswith(".tmp") or not entry.is_file(follow_symlinks=False):
                    continue
                key = "/".join(segments)
                if not key.startswith(request.prefix):
                    continue
                if len(objects) >= request.page_size:
                    more = True
                    return False
                try:
                    stat = os.stat(path)
                    version = _filesystem_version(path)
                    birth = _birth_time_ns(path, stat)
                except FileNotFoundError:
                    continue
                except OSError:
                    raise ReportingRowStoreError(
                        "PROVIDER_UNAVAILABLE", "filesystem list failed"
                    ) from None
                written_ns = max(birth, stat.st_mtime_ns)
                objects.append(
                    ReportingRowObjectListEntryV1(
                        key,
                        version,
                        stat.st_size,
                        format_row_instant(
                            datetime.fromtimestamp(written_ns / 1_000_000_000, tz=timezone.utc)
                        ),
                    )
                )
            return True

        walk(())
        token = objects[-1].object_ref if more and objects else None
        return ReportingRowObjectListPageV1(tuple(objects), token)

    async def delete(
        self, delete: ReportingRowObjectDeleteInputV1, context: ReportingRowObjectContextV1
    ) -> Literal["deleted", "absent"]:
        target = self._resolve(delete.location, delete.object_ref)
        return await asyncio.to_thread(self._delete, target, delete.native_version_ref)

    @staticmethod
    def _delete(target: str, native_version_ref: str) -> Literal["deleted", "absent"]:
        try:
            if _filesystem_version(target) != native_version_ref:
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

    def stat_input(version: str) -> ReportingRowObjectStatInputV1:
        return ReportingRowObjectStatInputV1(location, key, version, credential_ref)

    stat = getattr(provider, "stat", None)

    await provider.probe(location, prefix, context, credential_ref)
    passed.append("probe")
    created = await provider.put_if_absent(put(first), context)
    assert (
        created.created and created.native_version_ref
    ), "put_if_absent did not create a new object"
    passed.append("create")
    conflict = await provider.put_if_absent(put(b'{"c":3}\n'), context)
    assert (
        not conflict.created and conflict.native_version_ref == created.native_version_ref
    ), "put_if_absent replaced or re-versioned an existing object"
    stored = await provider.get(get(created.native_version_ref), context)
    assert stored == first, "create-only write did not preserve the first bytes"
    passed.append("create-only")
    if provider.ranged_reads:
        ranged = await provider.get(get(created.native_version_ref, byte_range=(8, 8)), context)
        assert ranged == b'{"b":2}\n', "ranged read returned wrong bytes"
        passed.append("ranged-read")
    missing = await provider.get(get("not-a-version"), context)
    assert missing is None, "get returned bytes for a version that does not exist"
    passed.append("version-pinned-read")
    if stat is not None:
        found = await stat(stat_input(created.native_version_ref), context)
        assert found is not None and found.size_bytes == len(
            first
        ), "stat did not report the stored object size"
        assert (
            found.native_version_ref == created.native_version_ref
        ), "stat did not echo the version it checked"
        missing_stat = await stat(stat_input("not-a-version"), context)
        assert missing_stat is None, "stat returned metadata for a version that does not exist"
        passed.append("version-pinned-stat")
    outcome = await provider.delete(delete("not-a-version"), context)
    assert outcome == "absent", "delete removed an object at a different version"
    outcome = await provider.delete(delete(created.native_version_ref), context)
    assert outcome == "deleted", "delete did not remove the exact version"
    outcome = await provider.delete(delete(created.native_version_ref), context)
    assert outcome == "absent", "repeated delete was not idempotent"
    passed.append("exact-version-delete")
    if stat is not None:
        deleted_stat = await stat(stat_input(created.native_version_ref), context)
        assert deleted_stat is None, "stat returned metadata for a deleted version"
    # A delayed delete of an old version must never remove a later write at the same key,
    # even one with identical bytes: native versions are unique per write, never
    # content-derived.
    recreated = await provider.put_if_absent(put(first), context)
    assert recreated.created and recreated.native_version_ref, "put_if_absent did not re-create"
    assert (
        recreated.native_version_ref != created.native_version_ref
    ), "re-creating identical bytes produced the same native version; versions must be write-unique"
    outcome = await provider.delete(delete(created.native_version_ref), context)
    assert outcome == "absent", "delete of a stale version removed or reported a later write"
    if stat is not None:
        # A stale version must never be proven present by a later write at the same key.
        stale_stat = await stat(stat_input(created.native_version_ref), context)
        assert stale_stat is None, "stat of a stale version reported a later write at the same key"
        current = await stat(stat_input(recreated.native_version_ref), context)
        assert (
            current is not None
            and current.size_bytes == len(first)
            and current.native_version_ref == recreated.native_version_ref
        ), "stat did not report the re-created object"
    survivor = await provider.get(get(recreated.native_version_ref), context)
    assert survivor == first, "delete of a stale version removed a later write with identical bytes"
    outcome = await provider.delete(delete(recreated.native_version_ref), context)
    assert outcome == "deleted", "delete did not remove the re-created version"
    passed.append("write-unique-version")
    lister = getattr(provider, "list", None)
    if lister is not None:
        await _check_listing(lister, location, prefix, credential_ref, provider, context)
        passed.append("list")
    return passed


async def _check_listing(
    lister: Any,
    location: Mapping[str, str],
    prefix: str,
    credential_ref: str | None,
    provider: ReportingRowObjectProviderV1,
    context: ReportingRowObjectContextV1,
) -> None:
    """The listing contract the orphan collector depends on.

    Every object under the prefix exactly once across pages, each with the version, size and
    write time ``put_if_absent`` reports, nothing outside the prefix, and deleted objects gone.
    """
    folder = f"{prefix}/conformance/{uuid.uuid4()}/"
    bodies = {
        f"{folder}a.jsonl": b'{"a":1}\n',
        f"{folder}b.jsonl.gz": b'{"b":2}\n{"b":3}\n',
        f"{folder}nested/c.jsonl": b'{"c":4}\n',
    }
    metadata = {"adcp-installation": "conformance", "adcp-intent": "conformance"}
    created: dict[str, str] = {}
    started = datetime.now(timezone.utc)

    async def list_all(under: str, page_size: int) -> list[ReportingRowObjectListEntryV1]:
        found: list[ReportingRowObjectListEntryV1] = []
        token: str | None = None
        for _ in range(100):
            page = await lister(
                ReportingRowObjectListInputV1(location, under, page_size, token, credential_ref),
                context,
            )
            assert (
                isinstance(page.objects, tuple) and len(page.objects) <= page_size
            ), "list returned more objects than page_size"
            found.extend(page.objects)
            previous, token = token, page.next_page_token
            if token is None:
                return found
            assert (
                isinstance(token, str) and token and token != previous
            ), "list returned an invalid or repeated token"
        raise AssertionError("list did not finish within 100 pages")

    try:
        for key, data in bodies.items():
            put = await provider.put_if_absent(
                ReportingRowObjectPutInputV1(
                    location,
                    key,
                    data,
                    "application/gzip" if key.endswith(".gz") else "application/x-ndjson",
                    metadata,
                    credential_ref,
                ),
                context,
            )
            assert put.created, "list: put_if_absent did not create a fresh object"
            created[key] = put.native_version_ref
        for page_size in (1, 2, 100):
            found = await list_all(folder, page_size)
            keys = [entry.object_ref for entry in found]
            assert len(set(keys)) == len(
                keys
            ), f"list returned an object twice (page_size {page_size})"
            assert set(keys) == set(
                bodies
            ), f"list did not return exactly the objects under the prefix (page_size {page_size})"
            for entry in found:
                assert (
                    entry.native_version_ref == created[entry.object_ref]
                ), "list reported a native version that put_if_absent did not"
                assert entry.size_bytes == len(
                    bodies[entry.object_ref]
                ), "list reported a wrong size"
                written = parse_row_instant(entry.created_at)
                # Object-store clocks differ from this host's; a wide margin still catches a
                # missing or zero time.
                assert (
                    written is not None and abs((written - started).total_seconds()) <= 600
                ), "list did not report when the version was written"
        assert len(await list_all(f"{folder}nested/", 10)) == 1, "list ignored the prefix"
        assert (
            len(await list_all(f"{folder}zz", 10)) == 0
        ), "list returned objects for an unused prefix"
        first = next(iter(created))
        outcome = await provider.delete(
            ReportingRowObjectDeleteInputV1(location, first, created[first], credential_ref),
            context,
        )
        assert outcome == "deleted", "list: delete did not remove the object"
        del created[first]
        remaining = sorted(entry.object_ref for entry in await list_all(folder, 2))
        assert remaining == sorted(created), "list returned a deleted object, or lost a live one"
    finally:
        for key, version in created.items():
            try:
                await provider.delete(
                    ReportingRowObjectDeleteInputV1(location, key, version, credential_ref), context
                )
            except Exception:  # noqa: S110  # nosec B110
                pass
