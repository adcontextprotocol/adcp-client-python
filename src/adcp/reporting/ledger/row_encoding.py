"""Canonical JSONL row encoding for reporting revisions.

This is the Python half of the shared SDK persistence contract
``reporting-row-storage`` v1 (adcontextprotocol/adcp#7996, section 3.1). The
JavaScript SDK implements the same contract; both must produce byte-identical
chunks, manifests and digests for the same rows, which the shared golden
fixture ``tests/fixtures/reporting-row-storage-v1.json`` pins.

Rows are stored as ``JCS(row) + "\\n"`` in ordinal order, where ``JCS`` is
RFC 8785. JCS escapes newline characters inside strings, so the ``0x0A`` byte
only ever separates rows. Rows are grouped into *segments* (the unit of
verification and of paged reads) and *chunks* (the unit of storage). The
boundaries are fixed by the contract:

* a new chunk starts when the current chunk holds
  :data:`REPORTING_ROW_CHUNK_MAX_ROWS` rows, or when appending the next row
  would exceed :data:`REPORTING_ROW_CHUNK_MAX_BYTES` canonical bytes;
* segments restart at each chunk boundary and close every
  :data:`REPORTING_ROW_SEGMENT_MAX_ROWS` rows.

A row larger than the chunk byte limit forms its own segment and chunk.
Digests always cover the uncompressed canonical bytes.

Readers verify *bytes*. Nothing here re-serializes a stored row to check it, so
verification never depends on number parsing or timestamp formatting.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import rfc8785

__all__ = [
    "REPORTING_ROW_CHUNK_MAX_BYTES",
    "REPORTING_ROW_CHUNK_MAX_ROWS",
    "REPORTING_ROW_CHUNK_MAX_SEGMENTS",
    "REPORTING_ROW_ENCODING_V1",
    "REPORTING_ROW_SEGMENT_MAX_ROWS",
    "ReportingEncodedRowsV1",
    "ReportingRevisionEnvelopeDigestV1",
    "ReportingRevisionEnvelopeHasherV1",
    "ReportingRowChunkManifestV1",
    "ReportingRowChunkV1",
    "ReportingRowEncodingError",
    "ReportingRowEncodingErrorCode",
    "ReportingRowSegmentManifestV1",
    "assert_reporting_revision_envelope_v1",
    "canonical_reporting_row_v1",
    "create_reporting_revision_envelope_hasher_v1",
    "create_reporting_rows_only_hasher_v1",
    "decode_verified_reporting_row_segments_v1",
    "encode_reporting_rows_v1",
    "reporting_revision_envelope_digest_v1",
    "reporting_row_manifest_sha256_v1",
    "verify_reporting_row_chunk_v1",
    "verify_reporting_row_manifests_v1",
]

REPORTING_ROW_ENCODING_V1 = "adcp_canonical_jsonl_v1"
REPORTING_ROW_SEGMENT_MAX_ROWS = 500
REPORTING_ROW_CHUNK_MAX_SEGMENTS = 20
REPORTING_ROW_CHUNK_MAX_ROWS = REPORTING_ROW_SEGMENT_MAX_ROWS * REPORTING_ROW_CHUNK_MAX_SEGMENTS
REPORTING_ROW_CHUNK_MAX_BYTES = 8 * 1024 * 1024

#: ECMAScript ``Number.MAX_SAFE_INTEGER``; the write-side integer domain is
#: fixed per contract version, not per SDK.
_MAX_SAFE_INTEGER = 2**53 - 1

_PORTABLE_RANGE_DETAIL = (
    "row {ordinal} contains a number outside the portable range; encode it as a string"
)

_NEWLINE = b"\n"
_COMMA = b","

ReportingRowEncodingErrorCode = Literal[
    "INVALID_ROW",
    "MANIFEST_MISMATCH",
    "CHUNK_INTEGRITY_FAILED",
    "ENVELOPE_INTEGRITY_FAILED",
]


class ReportingRowEncodingError(ValueError):
    """A row set cannot be encoded, or stored bytes do not match their digests.

    Switch on :attr:`code`. ``detail`` never carries row content.
    """

    def __init__(self, code: ReportingRowEncodingErrorCode, detail: str) -> None:
        super().__init__(f"Reporting row encoding: {code}: {detail}")
        self.code: ReportingRowEncodingErrorCode = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReportingRowSegmentManifestV1:
    """One segment: up to 500 consecutive rows, the unit of verified reads."""

    first_ordinal: int
    row_count: int
    #: Offset of the segment within its chunk's uncompressed canonical bytes.
    byte_offset: int
    byte_count: int
    sha256: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "first_ordinal": self.first_ordinal,
            "row_count": self.row_count,
            "byte_offset": self.byte_offset,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> ReportingRowSegmentManifestV1:
        return cls(
            first_ordinal=int(value["first_ordinal"]),
            row_count=int(value["row_count"]),
            byte_offset=int(value["byte_offset"]),
            byte_count=int(value["byte_count"]),
            sha256=str(value["sha256"]),
        )


@dataclass(frozen=True, slots=True)
class ReportingRowChunkManifestV1:
    """One chunk: up to 20 segments, the unit of storage."""

    chunk_index: int
    first_ordinal: int
    row_count: int
    byte_count: int
    sha256: str
    segments: tuple[ReportingRowSegmentManifestV1, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "first_ordinal": self.first_ordinal,
            "row_count": self.row_count,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "segments": [segment.to_wire() for segment in self.segments],
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> ReportingRowChunkManifestV1:
        return cls(
            chunk_index=int(value["chunk_index"]),
            first_ordinal=int(value["first_ordinal"]),
            row_count=int(value["row_count"]),
            byte_count=int(value["byte_count"]),
            sha256=str(value["sha256"]),
            segments=tuple(
                ReportingRowSegmentManifestV1.from_wire(segment) for segment in value["segments"]
            ),
        )


@dataclass(frozen=True, slots=True)
class ReportingRowChunkV1:
    manifest: ReportingRowChunkManifestV1
    #: Uncompressed canonical JSONL bytes.
    data: bytes


@dataclass(frozen=True, slots=True)
class ReportingEncodedRowsV1:
    encoding: str
    row_count: int
    #: Total canonical JSONL bytes across all chunks.
    byte_count: int
    chunks: tuple[ReportingRowChunkV1, ...]
    manifests: tuple[ReportingRowChunkManifestV1, ...]
    #: ``sha256(JCS(manifests))``; bound immutably on the revision's row set.
    row_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ReportingRevisionEnvelopeDigestV1:
    sha256: str
    #: Byte length of the canonical preimage; equals the binding's byte count.
    byte_count: int


def _sha256_hex(value: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(value).hexdigest()


def _assert_portable_numbers(value: object, ordinal: int) -> None:
    """Refuse values whose canonical bytes are not portable across JCS implementations."""
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ReportingRowEncodingError(
                "INVALID_ROW",
                _PORTABLE_RANGE_DETAIL.format(ordinal=ordinal),
            )
        return
    if isinstance(value, float):
        if not math.isfinite(value) or (value.is_integer() and abs(value) > _MAX_SAFE_INTEGER):
            raise ReportingRowEncodingError(
                "INVALID_ROW",
                _PORTABLE_RANGE_DETAIL.format(ordinal=ordinal),
            )
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_portable_numbers(item, ordinal)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReportingRowEncodingError(
                    "INVALID_ROW", f"row {ordinal} has a non-string object key"
                )
            _assert_portable_numbers(item, ordinal)
        return
    raise ReportingRowEncodingError(
        "INVALID_ROW", f"row {ordinal} contains a non-JSON value of type {type(value).__name__}"
    )


def canonical_reporting_row_v1(row: object, ordinal: int = 0) -> bytes:
    """Canonicalize one row for storage, without the trailing newline.

    Refuses values whose canonical bytes are not portable across conforming JCS
    implementations: non-objects, non-finite numbers, integers outside
    +-(2^53 - 1) (they must be strings in the row schema), and lone surrogates.
    ``-0`` serializes as ``0`` per RFC 8785; that is not a refusal.
    """
    if not isinstance(row, dict):
        raise ReportingRowEncodingError("INVALID_ROW", f"row {ordinal} is not a JSON object")
    try:
        _assert_portable_numbers(row, ordinal)
        return rfc8785.dumps(row)
    except ReportingRowEncodingError:
        raise
    except (rfc8785.CanonicalizationError, ValueError, TypeError, RecursionError) as error:
        raise ReportingRowEncodingError(
            "INVALID_ROW", f"row {ordinal} is not canonical JSON: {type(error).__name__}"
        ) from None


def encode_reporting_rows_v1(rows: Iterable[object]) -> ReportingEncodedRowsV1:
    """Encode rows into contract-fixed canonical JSONL chunks and manifests."""
    chunks: list[ReportingRowChunkV1] = []
    chunk_lines: list[bytes] = []
    chunk_bytes = 0
    chunk_first_ordinal = 0
    total_rows = 0

    def close_chunk() -> None:
        nonlocal chunk_lines, chunk_bytes
        if not chunk_lines:
            return
        chunks.append(_build_chunk(len(chunks), chunk_first_ordinal, chunk_lines))
        chunk_lines = []
        chunk_bytes = 0

    for ordinal, row in enumerate(rows):
        line = canonical_reporting_row_v1(row, ordinal) + _NEWLINE
        if len(chunk_lines) == REPORTING_ROW_CHUNK_MAX_ROWS or (
            chunk_lines and chunk_bytes + len(line) > REPORTING_ROW_CHUNK_MAX_BYTES
        ):
            close_chunk()
        if not chunk_lines:
            chunk_first_ordinal = ordinal
        chunk_lines.append(line)
        chunk_bytes += len(line)
        total_rows = ordinal + 1
    close_chunk()

    manifests = tuple(chunk.manifest for chunk in chunks)
    return ReportingEncodedRowsV1(
        encoding=REPORTING_ROW_ENCODING_V1,
        row_count=total_rows,
        byte_count=sum(manifest.byte_count for manifest in manifests),
        chunks=tuple(chunks),
        manifests=manifests,
        row_manifest_sha256=reporting_row_manifest_sha256_v1(manifests),
    )


def reporting_row_manifest_sha256_v1(manifests: Sequence[ReportingRowChunkManifestV1]) -> str:
    """``sha256(JCS(manifests))`` over the ordered chunk manifests, locators excluded."""
    return _sha256_hex(rfc8785.dumps([manifest.to_wire() for manifest in manifests]))


def verify_reporting_row_manifests_v1(
    manifests: Sequence[ReportingRowChunkManifestV1], *, row_count: int, row_manifest_sha256: str
) -> None:
    """Verify a manifest set against the digest and row count committed in PostgreSQL.

    Ordinals must be contiguous from zero, chunk and segment boundaries must
    follow the contract limits, and segment byte ranges must tile their chunk
    exactly.
    """
    if reporting_row_manifest_sha256_v1(manifests) != row_manifest_sha256.lower():
        raise ReportingRowEncodingError(
            "MANIFEST_MISMATCH", "chunk manifest digest does not match the header"
        )
    next_ordinal = 0
    for index, manifest in enumerate(manifests):
        if manifest.chunk_index != index or manifest.first_ordinal != next_ordinal:
            raise ReportingRowEncodingError("MANIFEST_MISMATCH", f"chunk {index} is out of order")
        if (
            manifest.row_count < 1
            or manifest.row_count > REPORTING_ROW_CHUNK_MAX_ROWS
            or len(manifest.segments) < 1
            or len(manifest.segments) > REPORTING_ROW_CHUNK_MAX_SEGMENTS
        ):
            raise ReportingRowEncodingError(
                "MANIFEST_MISMATCH", f"chunk {index} exceeds contract limits"
            )
        segment_ordinal = manifest.first_ordinal
        segment_offset = 0
        for segment_index, segment in enumerate(manifest.segments):
            last = segment_index == len(manifest.segments) - 1
            if (
                segment.first_ordinal != segment_ordinal
                or segment.byte_offset != segment_offset
                or segment.row_count < 1
                or segment.byte_count < 1
                or segment.row_count > REPORTING_ROW_SEGMENT_MAX_ROWS
                or (not last and segment.row_count != REPORTING_ROW_SEGMENT_MAX_ROWS)
            ):
                raise ReportingRowEncodingError(
                    "MANIFEST_MISMATCH", f"chunk {index} segment {segment_index} is invalid"
                )
            segment_ordinal += segment.row_count
            segment_offset += segment.byte_count
        if (
            segment_ordinal != manifest.first_ordinal + manifest.row_count
            or segment_offset != manifest.byte_count
        ):
            raise ReportingRowEncodingError(
                "MANIFEST_MISMATCH", f"chunk {index} segments do not tile the chunk"
            )
        if manifest.row_count > 1 and manifest.byte_count > REPORTING_ROW_CHUNK_MAX_BYTES:
            raise ReportingRowEncodingError(
                "MANIFEST_MISMATCH", f"chunk {index} exceeds the byte limit"
            )
        next_ordinal += manifest.row_count
    if next_ordinal != row_count:
        raise ReportingRowEncodingError(
            "MANIFEST_MISMATCH", "chunk manifests do not cover row_count"
        )


def verify_reporting_row_chunk_v1(manifest: ReportingRowChunkManifestV1, data: bytes) -> None:
    """Verify a whole chunk's canonical bytes: digest, byte count, every segment."""
    buffer = memoryview(data)
    if len(buffer) != manifest.byte_count or _sha256_hex(buffer) != manifest.sha256:
        raise ReportingRowEncodingError(
            "CHUNK_INTEGRITY_FAILED", f"chunk {manifest.chunk_index} digest mismatch"
        )
    for index, segment in enumerate(manifest.segments):
        _verify_segment(
            manifest, index, buffer[segment.byte_offset : segment.byte_offset + segment.byte_count]
        )


def decode_verified_reporting_row_segments_v1(
    manifest: ReportingRowChunkManifestV1,
    segment_range: tuple[int, int],
    data: bytes,
) -> list[dict[str, Any]]:
    """Verify and decode the inclusive segment range ``[from, to]``.

    ``data`` must hold exactly those segments' canonical bytes, starting at the
    first segment's ``byte_offset``; that is the shape a ranged object read
    returns. Rows are released only after every segment verifies.
    """
    first, last = segment_range
    if first < 0 or last < first or last >= len(manifest.segments):
        raise ReportingRowEncodingError("MANIFEST_MISMATCH", "segment range is outside the chunk")
    buffer = memoryview(data)
    base = manifest.segments[first].byte_offset
    end = manifest.segments[last].byte_offset + manifest.segments[last].byte_count
    if len(buffer) != end - base:
        raise ReportingRowEncodingError(
            "CHUNK_INTEGRITY_FAILED", "segment range byte length mismatch"
        )
    for index in range(first, last + 1):
        segment = manifest.segments[index]
        start = segment.byte_offset - base
        _verify_segment(manifest, index, buffer[start : start + segment.byte_count])
    rows: list[dict[str, Any]] = []
    for line in _split_lines(bytes(buffer)):
        try:
            parsed = json.loads(line)
        except ValueError:
            raise ReportingRowEncodingError(
                "CHUNK_INTEGRITY_FAILED", "a verified row is not valid JSON"
            ) from None
        if not isinstance(parsed, dict):
            raise ReportingRowEncodingError(
                "CHUNK_INTEGRITY_FAILED", "a verified row is not a JSON object"
            )
        rows.append(parsed)
    return rows


class ReportingRevisionEnvelopeHasherV1(Protocol):
    """Streaming hasher over verified chunk bytes; rows are never parsed."""

    def update(self, chunk_bytes: bytes) -> None: ...

    def digest(self) -> ReportingRevisionEnvelopeDigestV1: ...


class _JoinedRowsHasher:
    """``prefix + row,row,... + suffix`` without ever parsing a row."""

    def __init__(self, prefix: bytes, suffix: bytes, expected_rows: int) -> None:
        self._hash = hashlib.sha256(prefix)
        self._byte_count = len(prefix)
        self._suffix = suffix
        self._expected_rows = expected_rows
        self._rows = 0
        self._finished = False

    def update(self, chunk_bytes: bytes) -> None:
        if self._finished:
            raise RuntimeError("Reporting row hasher already finished")
        if not chunk_bytes:
            return
        if not chunk_bytes.endswith(_NEWLINE):
            raise ReportingRowEncodingError(
                "CHUNK_INTEGRITY_FAILED", "chunk does not end at a row boundary"
            )
        lines = chunk_bytes.count(_NEWLINE)
        # Every 0x0A is a row separator, so replacing separators with commas and
        # dropping the final one yields the rows joined by ",".
        joined = chunk_bytes[:-1].replace(_NEWLINE, _COMMA)
        if self._rows > 0:
            self._hash.update(_COMMA)
            self._byte_count += 1
        self._hash.update(joined)
        self._byte_count += len(joined)
        self._rows += lines

    def digest(self) -> ReportingRevisionEnvelopeDigestV1:
        if self._finished:
            raise RuntimeError("Reporting row hasher already finished")
        self._finished = True
        if self._rows != self._expected_rows:
            raise ReportingRowEncodingError(
                "ENVELOPE_INTEGRITY_FAILED", "row count does not match the header"
            )
        self._hash.update(self._suffix)
        return ReportingRevisionEnvelopeDigestV1(
            sha256=self._hash.hexdigest(), byte_count=self._byte_count + len(self._suffix)
        )


def create_reporting_revision_envelope_hasher_v1(
    *, reporting_revision_id: str, row_count: int, control_totals: Sequence[Any]
) -> ReportingRevisionEnvelopeHasherV1:
    """Streaming hasher for the protocol revision binding.

    ``sha256(JCS({reporting_revision_id, row_count, control_totals, reporting_rows}))``.
    JCS orders those keys as ``control_totals``, ``reporting_revision_id``,
    ``reporting_rows``, ``row_count``, so the preimage is the fixed prefix, the
    rows joined by commas, and the fixed suffix. Feed verified chunk bytes in
    order.
    """
    prefix = (
        b'{"control_totals":'
        + rfc8785.dumps(list(control_totals))
        + b',"reporting_revision_id":'
        + rfc8785.dumps(reporting_revision_id)
        + b',"reporting_rows":['
    )
    suffix = b'],"row_count":' + rfc8785.dumps(row_count) + b"}"
    return _JoinedRowsHasher(prefix, suffix, row_count)


def create_reporting_rows_only_hasher_v1(row_count: int) -> ReportingRevisionEnvelopeHasherV1:
    """Streaming hasher for digest profile ``rows_v1``: ``sha256(JCS(rows))``."""
    return _JoinedRowsHasher(b"[", b"]", row_count)


def reporting_revision_envelope_digest_v1(
    *,
    reporting_revision_id: str,
    row_count: int,
    control_totals: Sequence[Any],
    chunks: Iterable[bytes],
) -> ReportingRevisionEnvelopeDigestV1:
    """Envelope digest over already-encoded chunk bytes."""
    hasher = create_reporting_revision_envelope_hasher_v1(
        reporting_revision_id=reporting_revision_id,
        row_count=row_count,
        control_totals=control_totals,
    )
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.digest()


def assert_reporting_revision_envelope_v1(
    *,
    reporting_revision_id: str,
    row_count: int,
    control_totals: Sequence[Any],
    chunks: Iterable[bytes],
    sha256: str,
    byte_count: int | None = None,
) -> None:
    """Raise unless the canonical chunks reproduce the committed revision binding."""
    actual = reporting_revision_envelope_digest_v1(
        reporting_revision_id=reporting_revision_id,
        row_count=row_count,
        control_totals=control_totals,
        chunks=chunks,
    )
    if actual.sha256 != sha256.lower() or (
        byte_count is not None and actual.byte_count != byte_count
    ):
        raise ReportingRowEncodingError(
            "ENVELOPE_INTEGRITY_FAILED", "revision content binding mismatch"
        )


def _build_chunk(
    chunk_index: int, first_ordinal: int, lines: Sequence[bytes]
) -> ReportingRowChunkV1:
    segments: list[ReportingRowSegmentManifestV1] = []
    offset = 0
    for start in range(0, len(lines), REPORTING_ROW_SEGMENT_MAX_ROWS):
        segment_lines = lines[start : start + REPORTING_ROW_SEGMENT_MAX_ROWS]
        segment_bytes = b"".join(segment_lines)
        segments.append(
            ReportingRowSegmentManifestV1(
                first_ordinal=first_ordinal + start,
                row_count=len(segment_lines),
                byte_offset=offset,
                byte_count=len(segment_bytes),
                sha256=_sha256_hex(segment_bytes),
            )
        )
        offset += len(segment_bytes)
    data = b"".join(lines)
    return ReportingRowChunkV1(
        manifest=ReportingRowChunkManifestV1(
            chunk_index=chunk_index,
            first_ordinal=first_ordinal,
            row_count=len(lines),
            byte_count=len(data),
            sha256=_sha256_hex(data),
            segments=tuple(segments),
        ),
        data=data,
    )


def _verify_segment(
    manifest: ReportingRowChunkManifestV1, index: int, data: bytes | memoryview
) -> None:
    segment = manifest.segments[index]
    if len(data) != segment.byte_count or _sha256_hex(data) != segment.sha256:
        raise ReportingRowEncodingError(
            "CHUNK_INTEGRITY_FAILED",
            f"chunk {manifest.chunk_index} segment {index} digest mismatch",
        )
    raw = bytes(data)
    if raw.count(_NEWLINE) != segment.row_count or not raw.endswith(_NEWLINE):
        raise ReportingRowEncodingError(
            "CHUNK_INTEGRITY_FAILED",
            f"chunk {manifest.chunk_index} segment {index} line count mismatch",
        )


def _split_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    if not data.endswith(_NEWLINE):
        raise ReportingRowEncodingError(
            "CHUNK_INTEGRITY_FAILED", "trailing bytes after the last row"
        )
    return data[:-1].split(_NEWLINE)
