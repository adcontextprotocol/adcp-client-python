"""Shared plumbing for the cross-SDK row storage interop test and its fixtures.

The JavaScript SDK (``@adcp/sdk``) and this SDK share the ``adcp_reporting_row_*``
tables and the canonical chunk bytes. Interop is proven two ways:

* **live**, with both SDKs against one PostgreSQL schema and one filesystem bucket
  root (needs Node and a built JS SDK; see ``docs/reporting-row-storage.md``);
* **by fixture**: the rows each SDK wrote, as the other SDK's tests replay them.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from adcp.reporting.ledger.row_storage import (
    ReportingObjectRowBindingV1,
    ReportingPostgresRowBindingV1,
    ReportingRowStorageOptionsV1,
    ReportingRowStorageV1,
)
from adcp.reporting.ledger.row_storage_object import (
    ReportingRowObjectContextV1,
    ReportingRowObjectPutInputV1,
    create_filesystem_reporting_row_object_provider_v1,
)

NAMESPACE = "interop"
#: A zone whose local date differs from UTC, so the object key proves the timezone rule.
PERIOD_START = "2026-10-01T00:00:00Z"
SOURCE_TIMEZONE = "America/Los_Angeles"
PERIOD_DATE = "2026-09-30"
ACCOUNT = "acct_interop"
OBLIGATION = "robl_interop"
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
GOLDEN = FIXTURES / "reporting-row-storage-v1.json"
INTEROP_DIR = FIXTURES / "reporting-row-storage-interop-v1"
_ROW_SET_COLUMNS = (
    "row_set_id",
    "row_set_kind",
    "account_id",
    "obligation_id",
    "format",
    "algorithm",
    "digest_profile",
    "content_sha256",
    "content_size_bytes",
    "row_count",
    "row_manifest_sha256",
    "chunk_count",
    "row_binding_id",
    "rows_shared_from_row_set_id",
)
SET_KINDS = (
    ("revision", "inline"),
    ("revision", "reports"),
    ("adjustment", "inline"),
    ("adjustment", "reports"),
)


def storage_options(root: Path) -> ReportingRowStorageOptionsV1:
    """The binding configuration both SDKs use, byte for byte."""
    return ReportingRowStorageOptionsV1(
        deployment_namespace=NAMESPACE,
        bindings={
            "inline": ReportingPostgresRowBindingV1(),
            "reports": ReportingObjectRowBindingV1(
                provider="filesystem",
                location={"root": "reports"},
                prefix="adcp-rows",
                compression="gzip",
            ),
        },
        providers={
            "filesystem": create_filesystem_reporting_row_object_provider_v1(
                roots={"reports": root}
            )
        },
    )


def golden_vectors() -> list[dict[str, Any]]:
    vectors = json.loads(GOLDEN.read_text("utf-8"))["vectors"]
    return [vector for vector in vectors if vector["rows"]]


async def python_write(storage: ReportingRowStorageV1, pool: Any) -> list[dict[str, Any]]:
    """Write every golden vector as revision and adjustment row sets, in both binding kinds."""
    from adcp.reporting.ledger.row_encoding import (
        create_reporting_revision_envelope_hasher_v1,
        create_reporting_rows_only_hasher_v1,
        encode_reporting_rows_v1,
    )

    written: list[dict[str, Any]] = []
    for vector in golden_vectors():
        encoded = encode_reporting_rows_v1(vector["rows"])
        for kind, binding_id in SET_KINDS:
            row_set_id = f"rrev_py_{vector['id']}_{kind}_{binding_id}"
            totals = vector["envelope"]["control_totals"] if kind == "revision" else None
            if kind == "revision":
                hasher = create_reporting_revision_envelope_hasher_v1(
                    reporting_revision_id=row_set_id,
                    row_count=len(vector["rows"]),
                    control_totals=totals,
                )
            else:
                hasher = create_reporting_rows_only_hasher_v1(len(vector["rows"]))
            for chunk in encoded.chunks:
                hasher.update(chunk.data)
            digest = hasher.digest()
            prepared = await storage.prepare(
                row_set_id=row_set_id,
                row_set_kind=kind,  # type: ignore[arg-type]
                account_id=ACCOUNT,
                obligation_id=OBLIGATION,
                rows=vector["rows"],
                digest_profile="revision_envelope_v1" if kind == "revision" else "rows_v1",
                control_totals=totals,
                expected_sha256=digest.sha256,
                finality="snapshot" if kind == "revision" else None,
                period_start=PERIOD_START,
                source_timezone=SOURCE_TIMEZONE,
                binding_id=binding_id,
            )
            locators = None

            async def created(_key: str, _version: str) -> None:
                return None

            if binding_id == "reports":
                locators = await storage.upload(prepared, "py-interop-intent", created)
            async with pool.connection() as connection, connection.transaction():
                await storage.write_in_transaction(connection, prepared, locators)
            written.append(
                {
                    "row_set_id": row_set_id,
                    "row_set_kind": kind,
                    "binding_id": binding_id,
                    "account_id": ACCOUNT,
                    "obligation_id": OBLIGATION,
                    "digest_profile": prepared.digest_profile,
                    "content_sha256": digest.sha256,
                    "content_size_bytes": digest.byte_count,
                    "control_totals": totals,
                    "rows": vector["rows"],
                }
            )
    return written


async def python_verify(
    storage: ReportingRowStorageV1, pool: Any, expected: list[dict[str, Any]]
) -> int:
    """Verify row sets written by the other SDK through this SDK's readers."""
    verified = 0
    for item in expected:
        async with pool.connection() as connection:
            handle = await storage.find(connection, item["row_set_id"], item["account_id"])
            assert handle is not None, item["row_set_id"]
            assert handle.content_sha256 == item["content_sha256"], item["row_set_id"]
            assert handle.digest_profile == item["digest_profile"], item["row_set_id"]
            assert handle.content_size_bytes == item["content_size_bytes"], item["row_set_id"]
            rows = await storage.read_all(connection, handle, control_totals=item["control_totals"])
            assert rows == item["rows"], item["row_set_id"]
            page = await storage.read_page(connection, handle, 0, min(500, len(item["rows"])))
            assert page == item["rows"][:500], item["row_set_id"]
        verified += 1
    return verified


async def dump_state(pool: Any, root: Path, row_set_ids: list[str]) -> dict[str, Any]:
    """What a writer left in PostgreSQL and in the bucket, for fixture replay."""
    async with pool.connection() as connection:

        async def rows(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
            cursor = await connection.execute(query, params)
            names = [column.name for column in cursor.description or ()]
            return [dict(zip(names, row, strict=True)) for row in await cursor.fetchall()]

        sets = await rows(
            "SELECT row_set_id, row_set_kind, account_id, obligation_id, format, algorithm,"
            " digest_profile,"
            " content_sha256, content_size_bytes, row_count, row_manifest_sha256, chunk_count,"
            " row_binding_id, rows_shared_from_row_set_id FROM adcp_reporting_row_sets"
            " WHERE row_set_id = ANY(%s) ORDER BY row_set_id",
            (row_set_ids,),
        )
        chunks = await rows(
            "SELECT row_set_id, chunk_index, account_id, obligation_id, first_ordinal, row_count,"
            " canonical_size_bytes, sha256, segments, object_ref, compression, physical_sha256,"
            " size_bytes FROM adcp_reporting_row_chunks"
            " WHERE row_set_id = ANY(%s) ORDER BY row_set_id, chunk_index",
            (row_set_ids,),
        )
        bodies = await rows(
            "SELECT DISTINCT b.account_id, b.obligation_id, b.sha256, encode(b.body, 'hex') AS body_hex"
            " FROM adcp_reporting_chunk_bodies b JOIN adcp_reporting_row_chunks c"
            "   ON c.account_id = b.account_id AND c.obligation_id = b.obligation_id"
            "  AND c.sha256 = b.sha256 WHERE c.row_set_id = ANY(%s) ORDER BY b.sha256",
            (row_set_ids,),
        )
    objects = {
        chunk["object_ref"]: base64.b64encode((root / chunk["object_ref"]).read_bytes()).decode()
        for chunk in chunks
        if chunk["object_ref"]
    }
    return {"row_sets": sets, "row_chunks": chunks, "chunk_bodies": bodies, "objects": objects}


async def replay_state(pool: Any, root: Path, state: dict[str, Any]) -> dict[str, str]:
    """Load another SDK's recorded row sets into this database and bucket root.

    Object native versions are write-unique per filesystem, so each recorded object
    is re-created in the new bucket, at the same key, and its fresh version recorded.
    Everything else (manifests, digests, bodies, compressed bytes) is the other SDK's, untouched.
    Returns the object versions it recorded, by key.
    """
    provider = create_filesystem_reporting_row_object_provider_v1(roots={"reports": root})
    context = ReportingRowObjectContextV1()
    await provider.probe({"root": "reports"}, "adcp-rows", context)
    versions: dict[str, str] = {}
    for key, encoded in state["objects"].items():
        # Keys are kept verbatim: reads resolve by the recorded key, never by recomputing it.
        data = base64.b64decode(encoded)
        created = await provider.put_if_absent(
            ReportingRowObjectPutInputV1(
                {"root": "reports"}, key, data, "application/gzip", {}, None
            ),
            context,
        )
        versions[key] = created.native_version_ref
    async with pool.connection() as connection, connection.transaction():
        for row in state["row_sets"]:
            await connection.execute(
                "INSERT INTO adcp_reporting_row_sets (row_set_id, row_set_kind, account_id,"
                " obligation_id, format, algorithm, digest_profile, content_sha256,"
                " content_size_bytes, row_count, row_manifest_sha256, chunk_count, row_binding_id,"
                " rows_shared_from_row_set_id)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(row[name] for name in _ROW_SET_COLUMNS),
            )
        for body in state["chunk_bodies"]:
            await connection.execute(
                "INSERT INTO adcp_reporting_chunk_bodies (account_id, obligation_id, sha256, body)"
                " VALUES (%s, %s, %s, decode(%s, 'hex')) ON CONFLICT DO NOTHING",
                (body["account_id"], body["obligation_id"], body["sha256"], body["body_hex"]),
            )
        for chunk in state["row_chunks"]:
            await connection.execute(
                "INSERT INTO adcp_reporting_row_chunks (row_set_id, chunk_index, account_id,"
                " obligation_id, first_ordinal, row_count, canonical_size_bytes, sha256, segments,"
                " object_ref, native_version_ref, compression, physical_sha256, size_bytes)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)",
                (
                    chunk["row_set_id"],
                    chunk["chunk_index"],
                    chunk["account_id"],
                    chunk["obligation_id"],
                    chunk["first_ordinal"],
                    chunk["row_count"],
                    chunk["canonical_size_bytes"],
                    chunk["sha256"],
                    json.dumps(chunk["segments"]),
                    chunk["object_ref"],
                    versions.get(chunk["object_ref"]) if chunk["object_ref"] else None,
                    chunk["compression"],
                    chunk["physical_sha256"],
                    chunk["size_bytes"],
                ),
            )
    return versions
