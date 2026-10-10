"""Canonical JSONL row encoding: golden-fixture parity and verifier behaviour.

``tests/fixtures/reporting-row-storage-v1.json`` is the shared cross-SDK golden
file (copied byte for byte from the JavaScript SDK). Every chunk, manifest and
digest below must reproduce it exactly; a one-byte difference means the two
SDKs disagree about stored evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from adcp.reporting.ledger.row_encoding import (
    REPORTING_ROW_CHUNK_MAX_BYTES,
    REPORTING_ROW_CHUNK_MAX_ROWS,
    REPORTING_ROW_SEGMENT_MAX_ROWS,
    ReportingRowChunkManifestV1,
    ReportingRowEncodingError,
    assert_reporting_revision_envelope_v1,
    canonical_reporting_row_v1,
    create_reporting_revision_envelope_hasher_v1,
    create_reporting_rows_only_hasher_v1,
    decode_verified_reporting_row_segments_v1,
    encode_reporting_rows_v1,
    reporting_revision_envelope_digest_v1,
    reporting_row_manifest_sha256_v1,
    verify_reporting_row_chunk_v1,
    verify_reporting_row_manifests_v1,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "reporting-row-storage-v1.json").read_text("utf-8")
)

TOTALS: list[dict[str, str]] = [
    {"name": "impressions", "value": "1200", "value_type": "integer"},
    {"name": "spend", "value": "42.17", "value_type": "decimal", "unit": "USD"},
]


def reference_binding(
    revision_id: str, rows: list[Any], control_totals: list[Any] | None = None
) -> tuple[str, int]:
    payload = rfc8785.dumps(
        {
            "reporting_revision_id": revision_id,
            "row_count": len(rows),
            "control_totals": TOTALS if control_totals is None else control_totals,
            "reporting_rows": rows,
        }
    )
    return hashlib.sha256(payload).hexdigest(), len(payload)


def rows_of(count: int, fill: str = "") -> list[dict[str, Any]]:
    return [{"ordinal": index, "label": f"row-{index}{fill}"} for index in range(count)]


def assert_code(code: str) -> Any:
    return pytest.raises(ReportingRowEncodingError, match=code)


def test_streamed_envelope_digest_equals_the_protocol_revision_binding() -> None:
    cases: list[list[dict[str, Any]]] = [
        [],
        [{}],
        [{"media_buy_id": "mb_1", "impressions": 10, "spend": "1.50"}],
        [
            {"name": "café \U0001f600 日本語", "sep": " ", "control": '\x07\n"\\'},
            {
                "nested": {"z": [1, {"b": None, "a": True}], "a": []},
                "n": -0.0,
                "big": 1e15,
                "tiny": 1e-7,
            },
            {"float": 0.30000000000000004, "neg": -12.5, "int": 9007199254740991},
        ],
        rows_of(1234),
    ]
    for rows in cases:
        encoded = encode_reporting_rows_v1(rows)
        digest = reporting_revision_envelope_digest_v1(
            reporting_revision_id="rrev_case",
            row_count=len(rows),
            control_totals=TOTALS,
            chunks=[chunk.data for chunk in encoded.chunks],
        )
        assert (digest.sha256, digest.byte_count) == reference_binding("rrev_case", rows)
        assert encoded.row_count == len(rows)
        assert_reporting_revision_envelope_v1(
            reporting_revision_id="rrev_case",
            row_count=len(rows),
            control_totals=TOTALS,
            chunks=[chunk.data for chunk in encoded.chunks],
            sha256=digest.sha256,
            byte_count=digest.byte_count,
        )


def test_an_empty_revision_has_no_chunks_and_still_binds_empty_rows() -> None:
    encoded = encode_reporting_rows_v1([])
    assert encoded.chunks == ()
    assert encoded.byte_count == 0
    assert encoded.row_manifest_sha256 == hashlib.sha256(b"[]").hexdigest()
    verify_reporting_row_manifests_v1(
        [], row_count=0, row_manifest_sha256=encoded.row_manifest_sha256
    )
    digest = reporting_revision_envelope_digest_v1(
        reporting_revision_id="rrev_empty", row_count=0, control_totals=[], chunks=[]
    )
    assert (digest.sha256, digest.byte_count) == reference_binding("rrev_empty", [], [])


def test_segments_close_every_500_rows_and_chunks_every_10000_rows() -> None:
    encoded = encode_reporting_rows_v1(rows_of(REPORTING_ROW_CHUNK_MAX_ROWS + 1))
    assert len(encoded.chunks) == 2
    first, second = encoded.manifests
    assert first.row_count == REPORTING_ROW_CHUNK_MAX_ROWS
    assert len(first.segments) == REPORTING_ROW_CHUNK_MAX_ROWS // REPORTING_ROW_SEGMENT_MAX_ROWS
    assert all(s.row_count == REPORTING_ROW_SEGMENT_MAX_ROWS for s in first.segments)
    assert (second.first_ordinal, second.row_count, len(second.segments)) == (
        REPORTING_ROW_CHUNK_MAX_ROWS,
        1,
        1,
    )
    assert second.segments[0].byte_offset == 0
    verify_reporting_row_manifests_v1(
        encoded.manifests,
        row_count=REPORTING_ROW_CHUNK_MAX_ROWS + 1,
        row_manifest_sha256=encoded.row_manifest_sha256,
    )
    partial = encode_reporting_rows_v1(rows_of(1234))
    assert [s.row_count for s in partial.manifests[0].segments] == [500, 500, 234]


def test_chunks_close_before_exceeding_8_mib_and_an_oversized_row_stands_alone() -> None:
    megabyte = "x" * (1024 * 1024)
    encoded = encode_reporting_rows_v1([{"index": i, "payload": megabyte} for i in range(9)])
    assert [m.row_count for m in encoded.manifests] == [7, 2]
    assert all(m.byte_count <= REPORTING_ROW_CHUNK_MAX_BYTES for m in encoded.manifests)

    oversized = [{"a": 1}, {"payload": "y" * REPORTING_ROW_CHUNK_MAX_BYTES}, {"b": 2}]
    split = encode_reporting_rows_v1(oversized)
    assert [m.row_count for m in split.manifests] == [1, 1, 1]
    verify_reporting_row_manifests_v1(
        split.manifests, row_count=3, row_manifest_sha256=split.row_manifest_sha256
    )
    digest = reporting_revision_envelope_digest_v1(
        reporting_revision_id="rrev_big",
        row_count=3,
        control_totals=TOTALS,
        chunks=[c.data for c in split.chunks],
    )
    assert (digest.sha256, digest.byte_count) == reference_binding("rrev_big", oversized)


@pytest.mark.parametrize(
    "row",
    [
        ["not", "an", "object"],
        None,
        {"id": 2**53},
        {"nested": [{"id": -(2**60)}]},
        {"value": float("inf")},
        {"value": float("nan")},
        {"value": 1e20},
        {"value": 2.0**53},
        {"text": "lone \ud800 surrogate"},
        {"text": "lone \udc00 surrogate"},
        {"at": object()},
        {"decimal": __import__("decimal").Decimal("1.5")},
        {1: "non-string key"},
    ],
)
def test_refuses_rows_whose_canonical_bytes_are_not_portable(row: Any) -> None:
    with assert_code("INVALID_ROW"):
        encode_reporting_rows_v1([row])


def test_refuses_circular_and_over_deep_rows_with_the_typed_error() -> None:
    circular: dict[str, Any] = {}
    circular["self"] = circular
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(sys.getrecursionlimit() + 10):
        cursor["next"] = {}
        cursor = cursor["next"]
    for row in (circular, deep):
        with assert_code("INVALID_ROW"):
            encode_reporting_rows_v1([row])


def test_accepts_the_edges_of_the_portable_domain() -> None:
    encode_reporting_rows_v1([{"ok": "paired \U0001f600", "id": 2**53 - 1, "f": 2.0**53 - 1}])
    assert (
        canonical_reporting_row_v1({"n": -0.0, "i": -(2**53 - 1)})
        == b'{"i":-9007199254740991,"n":0}'
    )


def test_manifest_verification_rejects_dropped_reordered_and_altered_chunks() -> None:
    encoded = encode_reporting_rows_v1(rows_of(REPORTING_ROW_CHUNK_MAX_ROWS * 2 + 10))
    expected = {"row_count": encoded.row_count, "row_manifest_sha256": encoded.row_manifest_sha256}
    verify_reporting_row_manifests_v1(encoded.manifests, **expected)

    with assert_code("MANIFEST_MISMATCH"):
        verify_reporting_row_manifests_v1(encoded.manifests[:2], **expected)
    with assert_code("MANIFEST_MISMATCH"):
        verify_reporting_row_manifests_v1(
            (encoded.manifests[1], encoded.manifests[0], encoded.manifests[2]), **expected
        )
    altered = list(encoded.manifests)
    altered[1] = ReportingRowChunkManifestV1(
        **{**_fields(altered[1]), "sha256": "f" * 64}  # type: ignore[arg-type]
    )
    with assert_code("MANIFEST_MISMATCH"):
        verify_reporting_row_manifests_v1(altered, **expected)

    # A self-consistent manifest that breaks the contract tiling is refused even
    # when the caller supplies its matching digest.
    first = encoded.manifests[0]
    truncated = [ReportingRowChunkManifestV1(**{**_fields(first), "segments": first.segments[:-1]})]  # type: ignore[arg-type]
    with assert_code("MANIFEST_MISMATCH"):
        verify_reporting_row_manifests_v1(
            truncated,
            row_count=REPORTING_ROW_CHUNK_MAX_ROWS,
            row_manifest_sha256=reporting_row_manifest_sha256_v1(truncated),
        )


def _fields(manifest: ReportingRowChunkManifestV1) -> dict[str, Any]:
    return {
        "chunk_index": manifest.chunk_index,
        "first_ordinal": manifest.first_ordinal,
        "row_count": manifest.row_count,
        "byte_count": manifest.byte_count,
        "sha256": manifest.sha256,
        "segments": manifest.segments,
    }


def test_chunk_verification_detects_tampered_bytes_and_segment_line_counts() -> None:
    chunk = encode_reporting_rows_v1(rows_of(1200)).chunks[0]
    verify_reporting_row_chunk_v1(chunk.manifest, chunk.data)

    flipped = bytearray(chunk.data)
    flipped[10] ^= 0x01
    with assert_code("CHUNK_INTEGRITY_FAILED"):
        verify_reporting_row_chunk_v1(chunk.manifest, bytes(flipped))
    with assert_code("CHUNK_INTEGRITY_FAILED"):
        verify_reporting_row_chunk_v1(chunk.manifest, chunk.data[1:])


def test_ranged_segment_reads_release_only_verified_rows() -> None:
    rows = rows_of(1200)
    chunk = encode_reporting_rows_v1(rows).chunks[0]
    _, second, third = chunk.manifest.segments
    ranged = chunk.data[second.byte_offset : third.byte_offset + third.byte_count]
    assert decode_verified_reporting_row_segments_v1(chunk.manifest, (1, 2), ranged) == rows[500:]

    tampered = bytearray(ranged)
    tampered[-5] ^= 0x01
    with assert_code("CHUNK_INTEGRITY_FAILED"):
        decode_verified_reporting_row_segments_v1(chunk.manifest, (1, 2), bytes(tampered))
    with assert_code("MANIFEST_MISMATCH"):
        decode_verified_reporting_row_segments_v1(chunk.manifest, (2, 1), ranged)
    with assert_code("MANIFEST_MISMATCH"):
        decode_verified_reporting_row_segments_v1(chunk.manifest, (0, 3), ranged)
    with assert_code("CHUNK_INTEGRITY_FAILED"):
        decode_verified_reporting_row_segments_v1(chunk.manifest, (0, 0), ranged)


def test_envelope_hashing_fails_closed_on_row_count_drift_and_partial_rows() -> None:
    rows = rows_of(3)
    encoded = encode_reporting_rows_v1(rows)
    short = create_reporting_revision_envelope_hasher_v1(
        reporting_revision_id="rrev_x", row_count=4, control_totals=TOTALS
    )
    short.update(encoded.chunks[0].data)
    with assert_code("ENVELOPE_INTEGRITY_FAILED"):
        short.digest()

    partial = create_reporting_revision_envelope_hasher_v1(
        reporting_revision_id="rrev_x", row_count=3, control_totals=TOTALS
    )
    with assert_code("CHUNK_INTEGRITY_FAILED"):
        partial.update(encoded.chunks[0].data[:5])

    sha256, byte_count = reference_binding("rrev_x", rows)
    with assert_code("ENVELOPE_INTEGRITY_FAILED"):
        assert_reporting_revision_envelope_v1(
            reporting_revision_id="rrev_other",
            row_count=3,
            control_totals=TOTALS,
            chunks=[c.data for c in encoded.chunks],
            sha256=sha256,
            byte_count=byte_count,
        )

    finished = create_reporting_rows_only_hasher_v1(0)
    finished.digest()
    with pytest.raises(RuntimeError):
        finished.update(b"")


def test_rows_v1_profile_equals_sha256_of_jcs_rows() -> None:
    for rows in ([], rows_of(1), rows_of(1001)):
        encoded = encode_reporting_rows_v1(rows)
        hasher = create_reporting_rows_only_hasher_v1(len(rows))
        for chunk in encoded.chunks:
            hasher.update(chunk.data)
        payload = rfc8785.dumps(rows)
        digest = hasher.digest()
        assert (digest.sha256, digest.byte_count) == (
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )


@pytest.mark.parametrize("vector", FIXTURE["vectors"], ids=lambda vector: vector["id"])
def test_matches_the_shared_golden_fixture_byte_for_byte(vector: dict[str, Any]) -> None:
    expected = vector["expected"]
    encoded = encode_reporting_rows_v1(vector["rows"])
    assert [base64.b64encode(chunk.data).decode() for chunk in encoded.chunks] == expected[
        "chunks_base64"
    ]
    assert [manifest.to_wire() for manifest in encoded.manifests] == expected["manifests"]
    assert encoded.row_manifest_sha256 == expected["row_manifest_sha256"]
    envelope = vector["envelope"]
    assert reference_binding(
        envelope["reporting_revision_id"], vector["rows"], envelope["control_totals"]
    ) == (expected["revision_content_sha256"], expected["canonical_byte_count"])
    digest = reporting_revision_envelope_digest_v1(
        reporting_revision_id=envelope["reporting_revision_id"],
        row_count=envelope["row_count"],
        control_totals=envelope["control_totals"],
        chunks=[chunk.data for chunk in encoded.chunks],
    )
    assert (digest.sha256, digest.byte_count) == (
        expected["revision_content_sha256"],
        expected["canonical_byte_count"],
    )
    # Verify the golden chunks themselves, then decode them back to the rows.
    for chunk, manifest in zip(encoded.chunks, expected["manifests"], strict=True):
        parsed = ReportingRowChunkManifestV1.from_wire(manifest)
        verify_reporting_row_chunk_v1(parsed, chunk.data)
        assert (
            decode_verified_reporting_row_segments_v1(
                parsed, (0, len(parsed.segments) - 1), chunk.data
            )
            == vector["rows"][parsed.first_ordinal : parsed.first_ordinal + parsed.row_count]
        )
