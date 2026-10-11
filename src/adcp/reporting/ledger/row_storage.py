"""Revision row storage over PostgreSQL chunk bodies.

The Python half of the shared SDK persistence row store (adcontextprotocol/adcp#7996,
section 3). Revision headers stay in the ledger. Rows live as canonical JSONL
chunks (:mod:`adcp.reporting.ledger.row_encoding`) in the ``adcp_reporting_row_*``
tables, which the JavaScript SDK also uses, so one database can hold chunks
written by either SDK. PostgreSQL is the trust anchor: every read verifies the
stored bytes against digests committed in the database before a row is released.

Row location lives in the ``adcp_reporting_row_sets`` side table, keyed by revision
ID, until the shared Core layout. A revision without a row set still carries its
rows in the legacy ``reporting_revision_rows`` table.

This module covers the ``postgres`` binding kind. Object bindings (filesystem,
GCS, S3, Azure) arrive in later changes and share the same records.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import rfc8785

from adcp.reporting.ledger.row_encoding import (
    REPORTING_ROW_ENCODING_V1,
    ReportingEncodedRowsV1,
    ReportingRowChunkManifestV1,
    ReportingRowEncodingError,
    ReportingRowSegmentManifestV1,
    create_reporting_revision_envelope_hasher_v1,
    create_reporting_rows_only_hasher_v1,
    decode_verified_reporting_row_segments_v1,
    encode_reporting_rows_v1,
    verify_reporting_row_chunk_v1,
    verify_reporting_row_manifests_v1,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.row_encoding import (
        ReportingRevisionEnvelopeDigestV1,
        ReportingRevisionEnvelopeHasherV1,
    )

__all__ = [
    "REPORTING_ROW_STORAGE_DDL_PATH",
    "REPORTING_ROW_STORAGE_TABLES",
    "ReportingPostgresRowBindingV1",
    "ReportingPreparedRowSetV1",
    "ReportingRowBindingSelectionInputV1",
    "ReportingRowSetHandleV1",
    "ReportingRowStorageOptionsV1",
    "ReportingRowStorageV1",
    "ReportingRowStoreError",
    "ReportingRowStoreErrorCode",
]

REPORTING_ROW_STORAGE_DDL_PATH = Path(__file__).with_name("reporting_row_storage.sql")

#: Tables ``reporting_row_storage.sql`` creates, in dependency order.
REPORTING_ROW_STORAGE_TABLES: tuple[str, ...] = (
    "adcp_persistence_installation",
    "adcp_reporting_row_bindings",
    "adcp_reporting_row_sets",
    "adcp_reporting_row_chunks",
    "adcp_reporting_chunk_bodies",
    "adcp_reporting_row_write_intents",
)

ReportingRowStoreErrorCode = Literal[
    "INVALID_INPUT",
    "CONTENT_CONFLICT",
    "ROWS_INTEGRITY_FAILED",
    "ROWS_UNAVAILABLE",
    "ROWS_EXPIRED",
    "PROVIDER_UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "ABORTED",
    "UNSAFE_BINDING",
    "STATE_UNAVAILABLE",
]

DigestProfile = Literal["revision_envelope_v1", "rows_v1"]
RowSetKind = Literal["revision", "adjustment"]

_DEFAULT_BINDING_ID = "postgres"
_BINDING_ID_PATTERN = "^[A-Za-z0-9_.:-]{1,128}$"


class ReportingRowStoreError(RuntimeError):
    """A stable, secret-free row-storage failure. Switch on :attr:`code`.

    Operator codes are never buyer-facing: callers collapse them before a wire
    response so a buyer cannot probe whether an object exists.
    """

    def __init__(self, code: ReportingRowStoreErrorCode, detail: str | None = None) -> None:
        super().__init__(
            f"Reporting row store: {code}: {detail}" if detail else f"Reporting row store: {code}"
        )
        self.code: ReportingRowStoreErrorCode = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReportingPostgresRowBindingV1:
    """Rows stored as chunk bodies in the ledger's own PostgreSQL schema."""

    kind: Literal["postgres"] = "postgres"


ReportingRowBindingDefinitionV1 = ReportingPostgresRowBindingV1


@dataclass(frozen=True, slots=True)
class ReportingRowBindingSelectionInputV1:
    account_id: str
    reporting_obligation_id: str
    row_set_kind: RowSetKind
    finality: Literal["snapshot", "official"] | None
    row_count: int
    canonical_byte_count: int


@dataclass(frozen=True, slots=True)
class ReportingRowStorageOptionsV1:
    """Host configuration for row storage.

    ``bindings`` maps a logical binding ID to its immutable definition. Changing
    a definition requires a new ID; keep retired definitions configured while
    revisions still reference them. ``deployment_namespace`` is combined with the
    schema's installation identity into each binding's ``namespace_key``, so two
    SDKs on one database must use the same value.
    """

    bindings: Mapping[str, ReportingRowBindingDefinitionV1] | None = None
    select_row_binding: (
        Callable[[ReportingRowBindingSelectionInputV1], str | Awaitable[str]] | None
    ) = None
    deployment_namespace: str = "default"


@dataclass(frozen=True, slots=True)
class ReportingPreparedRowSetV1:
    """Encoded rows, proven to reproduce the committed digest, with their binding chosen."""

    row_set_id: str
    row_set_kind: RowSetKind
    account_id: str
    obligation_id: str
    encoded: ReportingEncodedRowsV1
    digest_profile: DigestProfile
    content_sha256: str
    canonical_byte_count: int
    binding_id: str
    binding: ReportingRowBindingDefinitionV1


@dataclass(frozen=True, slots=True)
class ReportingRowSetHandleV1:
    """A row set whose chunk manifest has been verified against its committed digest."""

    row_set_id: str
    row_set_kind: RowSetKind
    account_id: str
    obligation_id: str
    digest_profile: DigestProfile
    content_sha256: str
    canonical_byte_count: int
    row_count: int
    row_manifest_sha256: str
    binding_id: str
    binding_identity_sha256: str
    kind: Literal["postgres", "object"]
    row_location_version: int
    manifests: tuple[ReportingRowChunkManifestV1, ...]


def _sha256_hex(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _binding_identity_config(binding: ReportingRowBindingDefinitionV1) -> dict[str, Any]:
    return {}


def _binding_identity_sha256(binding: ReportingRowBindingDefinitionV1) -> str:
    return _sha256_hex(
        rfc8785.dumps([binding.kind, binding.kind, _binding_identity_config(binding)])
    )


class ReportingRowStorageV1:
    """Reads and writes revision row sets over a caller-owned connection.

    Construct one per store. :meth:`ready` registers the configured bindings
    against the installed schema and must succeed before any read or write.
    """

    def __init__(self, options: ReportingRowStorageOptionsV1 | None = None) -> None:
        options = options or ReportingRowStorageOptionsV1()
        bindings = dict(options.bindings or {_DEFAULT_BINDING_ID: ReportingPostgresRowBindingV1()})
        if not bindings:
            raise ReportingRowStoreError("INVALID_INPUT", "at least one row binding is required")
        for binding_id, binding in bindings.items():
            if not re.fullmatch(_BINDING_ID_PATTERN, binding_id):
                raise ReportingRowStoreError(
                    "INVALID_INPUT", f"invalid row binding ID {binding_id}"
                )
            if not isinstance(binding, ReportingPostgresRowBindingV1):
                raise ReportingRowStoreError(
                    "INVALID_INPUT", f"row binding {binding_id} has an unsupported kind"
                )
        namespace = options.deployment_namespace
        if not namespace or len(namespace) > 256:
            raise ReportingRowStoreError(
                "INVALID_INPUT", "deployment_namespace must be 1-256 characters"
            )
        self._options = options
        self._bindings: Mapping[str, ReportingRowBindingDefinitionV1] = bindings
        self._deployment_namespace = namespace
        self._installation: tuple[str, str] | None = None
        self._ready_lock = asyncio.Lock()

    # -- setup ------------------------------------------------------------

    @property
    def installation_identity(self) -> tuple[str, str]:
        """``(installation_id, namespace_key)``; available after :meth:`ready`."""
        if self._installation is None:
            raise ReportingRowStoreError("STATE_UNAVAILABLE", "row storage is not ready")
        return self._installation

    async def ready(self, connection: Any) -> None:
        """Probe the schema and register (or verify) every configured binding. Idempotent."""
        if self.is_ready:
            return
        async with self._ready_lock:
            if not self.is_ready:
                await self._register(connection)

    @property
    def is_ready(self) -> bool:
        return self._installation is not None

    async def _register(self, connection: Any) -> None:
        present = await (
            await connection.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema() AND table_name = ANY(%s::text[])",
                (list(REPORTING_ROW_STORAGE_TABLES),),
            )
        ).fetchall()
        names = {row[0] for row in present}
        missing = [name for name in REPORTING_ROW_STORAGE_TABLES if name not in names]
        if missing:
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE",
                "row storage requires the reporting_row_storage.sql migration; missing tables: "
                + ", ".join(missing),
            )
        await self._verify_catalog(connection)
        await self._refuse_foreign_core_keys(connection)
        row = await (
            await connection.execute(
                "SELECT installation_id::text FROM adcp_persistence_installation WHERE singleton"
            )
        ).fetchone()
        if row is None:
            raise ReportingRowStoreError("STATE_UNAVAILABLE", "installation identity is missing")
        installation_id = str(row[0])
        namespace_key = _sha256_hex(
            rfc8785.dumps(["adcp.rows.v1", self._deployment_namespace, installation_id])
        )
        for binding_id, binding in self._bindings.items():
            identity_sha256 = _binding_identity_sha256(binding)
            await connection.execute(
                "INSERT INTO adcp_reporting_row_bindings"
                " (row_binding_id, kind, provider, identity_config, identity_sha256,"
                "  operational_config, namespace_key)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)"
                " ON CONFLICT (row_binding_id) DO NOTHING",
                (
                    binding_id,
                    binding.kind,
                    "postgres",
                    json.dumps(_binding_identity_config(binding)),
                    identity_sha256,
                    json.dumps({}),
                    namespace_key,
                ),
            )
            stored = await (
                await connection.execute(
                    "SELECT identity_sha256, namespace_key FROM adcp_reporting_row_bindings"
                    " WHERE row_binding_id = %s",
                    (binding_id,),
                )
            ).fetchone()
            if stored is None or stored[0] != identity_sha256:
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING",
                    f"row binding {binding_id} identity differs from its definition",
                )
            if stored[1] != namespace_key:
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", f"row binding {binding_id} belongs to another installation"
                )
        self._installation = (installation_id, namespace_key)

    @staticmethod
    async def _verify_catalog(connection: Any) -> None:
        """Fail closed on DDL drift in any object this store depends on.

        Each required column, constraint, index, trigger and guard function is
        fingerprinted against the manifest generated from the packaged
        migration. Unrelated adopter objects are ignored, and diagnostics name
        only a bundled object.
        """
        from importlib.resources import files

        from adcp.reporting.outbox._schema import _catalog_objects

        required: dict[str, dict[str, Any]] = json.loads(
            files("adcp.reporting.ledger").joinpath("required_row_storage_schema.json").read_text()
        )
        installed = await _catalog_objects(
            connection,
            table_names=REPORTING_ROW_STORAGE_TABLES,
            function_identity=("adcp_reporting_row_storage_immutable", ""),
            name_prefix="adcp_",
        )
        for key, expected in required.items():
            actual = installed.get(key)
            if actual is None:
                classification = "missing"
            elif not actual["enabled"]:
                classification = "disabled"
            elif actual["fingerprint"] != expected["fingerprint"]:
                classification = "changed"
            else:
                continue
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE", f"row storage schema is {classification}: {key}"
            )

    @staticmethod
    async def _refuse_foreign_core_keys(connection: Any) -> None:
        """Refuse a row-set table that another SDK created with a core-table foreign key.

        The JavaScript migration references ``adcp_reporting_obligations``, which
        Python obligations never populate (they live in ``reporting_obligations``
        until the shared Core layout). Writing here would fail at the first row
        set, so refuse up front with the remedy.
        """
        row = await (
            await connection.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid ="
                " to_regclass(quote_ident(current_schema()) || '.adcp_reporting_row_sets')"
                " AND contype = 'f' AND (SELECT relname FROM pg_class WHERE oid = confrelid)"
                " <> ALL(%s::text[]) LIMIT 1",
                (list(REPORTING_ROW_STORAGE_TABLES),),
            )
        ).fetchone()
        if row is not None:
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE",
                "adcp_reporting_row_sets has a foreign key to another SDK's core table "
                f"({row[0]}); apply reporting_row_storage.sql before that SDK's migration, "
                "or drop the constraint",
            )

    # -- write ------------------------------------------------------------

    async def prepare(
        self,
        *,
        row_set_id: str,
        row_set_kind: RowSetKind,
        account_id: str,
        obligation_id: str,
        rows: Sequence[Mapping[str, Any]],
        digest_profile: DigestProfile,
        control_totals: Sequence[Any] | None = None,
        expected_sha256: str | None = None,
        finality: Literal["snapshot", "official"] | None = None,
        binding_id: str | None = None,
    ) -> ReportingPreparedRowSetV1:
        """Encode rows, prove them against the committed digest, and choose a binding.

        ``digest_profile`` is ``revision_envelope_v1`` (the protocol revision
        binding, with ``control_totals``) or ``rows_v1`` (``sha256(JCS(rows))``).
        When ``expected_sha256`` is given the rows must reproduce it.
        """
        if self._installation is None:
            raise ReportingRowStoreError("STATE_UNAVAILABLE", "row storage is not ready")
        try:
            encoded = encode_reporting_rows_v1(rows)
        except ReportingRowEncodingError as error:
            raise ReportingRowStoreError("INVALID_INPUT", error.detail) from None
        hasher: ReportingRevisionEnvelopeHasherV1
        if digest_profile == "revision_envelope_v1":
            hasher = create_reporting_revision_envelope_hasher_v1(
                reporting_revision_id=row_set_id,
                row_count=len(rows),
                control_totals=list(control_totals or ()),
            )
        else:
            hasher = create_reporting_rows_only_hasher_v1(len(rows))
        for chunk in encoded.chunks:
            hasher.update(chunk.data)
        digest = hasher.digest()
        if expected_sha256 is not None and digest.sha256 != expected_sha256.lower():
            raise ReportingRowStoreError(
                "INVALID_INPUT", "rows do not reproduce their committed binding"
            )
        chosen = binding_id
        if chosen is None:
            if self._options.select_row_binding is not None:
                selected = self._options.select_row_binding(
                    ReportingRowBindingSelectionInputV1(
                        account_id=account_id,
                        reporting_obligation_id=obligation_id,
                        row_set_kind=row_set_kind,
                        finality=finality,
                        row_count=len(rows),
                        canonical_byte_count=digest.byte_count,
                    )
                )
                chosen = await selected if inspect.isawaitable(selected) else selected
            elif len(self._bindings) == 1:
                chosen = next(iter(self._bindings))
            else:
                chosen = _DEFAULT_BINDING_ID
        binding = self._bindings.get(chosen)
        if binding is None:
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE", f"row binding {chosen} is not configured"
            )
        return ReportingPreparedRowSetV1(
            row_set_id=row_set_id,
            row_set_kind=row_set_kind,
            account_id=account_id,
            obligation_id=obligation_id,
            encoded=encoded,
            digest_profile=digest_profile,
            content_sha256=digest.sha256,
            canonical_byte_count=digest.byte_count,
            binding_id=chosen,
            binding=binding,
        )

    async def write_in_transaction(
        self, connection: Any, prepared: ReportingPreparedRowSetV1
    ) -> None:
        """Persist a prepared row set inside the caller's ledger transaction.

        Replays of an identical row set are no-ops; a conflicting row set under
        the same ID is refused.
        """
        encoded = prepared.encoded
        cursor = await connection.execute(
            "INSERT INTO adcp_reporting_row_sets"
            " (row_set_id, row_set_kind, account_id, obligation_id, encoding, digest_profile,"
            "  content_sha256, canonical_byte_count, row_count, row_manifest_sha256,"
            "  chunk_count, row_binding_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (row_set_id) DO NOTHING",
            (
                prepared.row_set_id,
                prepared.row_set_kind,
                prepared.account_id,
                prepared.obligation_id,
                REPORTING_ROW_ENCODING_V1,
                prepared.digest_profile,
                prepared.content_sha256,
                prepared.canonical_byte_count,
                encoded.row_count,
                encoded.row_manifest_sha256,
                len(encoded.chunks),
                prepared.binding_id,
            ),
        )
        if cursor.rowcount != 1:
            existing = await (
                await connection.execute(
                    "SELECT content_sha256, row_manifest_sha256 FROM adcp_reporting_row_sets"
                    " WHERE row_set_id = %s",
                    (prepared.row_set_id,),
                )
            ).fetchone()
            if (
                existing is None
                or existing[0] != prepared.content_sha256
                or existing[1] != encoded.row_manifest_sha256
            ):
                raise ReportingRowStoreError(
                    "CONTENT_CONFLICT", "row set identity names different content"
                )
            return
        for chunk in encoded.chunks:
            manifest = chunk.manifest
            await connection.execute(
                "INSERT INTO adcp_reporting_chunk_bodies (account_id, obligation_id, sha256, body)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (prepared.account_id, prepared.obligation_id, manifest.sha256, chunk.data),
            )
            await connection.execute(
                "INSERT INTO adcp_reporting_row_chunks"
                " (row_set_id, chunk_index, account_id, obligation_id, first_ordinal, row_count,"
                "  byte_count, sha256, segments, object_key, native_version, compression,"
                "  physical_sha256, physical_byte_count)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,"
                "         NULL, NULL, 'none', NULL, NULL)",
                (
                    prepared.row_set_id,
                    manifest.chunk_index,
                    prepared.account_id,
                    prepared.obligation_id,
                    manifest.first_ordinal,
                    manifest.row_count,
                    manifest.byte_count,
                    manifest.sha256,
                    json.dumps([segment.to_wire() for segment in manifest.segments]),
                ),
            )

    # -- read -------------------------------------------------------------

    async def find(
        self, connection: Any, row_set_id: str, account_id: str
    ) -> ReportingRowSetHandleV1 | None:
        """Load a row set and verify its chunk manifest against the committed digest.

        Returns ``None`` when no row set exists (legacy rows) or it belongs to
        another account. Raises ``ROWS_EXPIRED`` or ``ROWS_UNAVAILABLE`` when the
        rows were pruned or lost, and ``ROWS_INTEGRITY_FAILED`` when the manifest
        does not match its committed digest.
        """
        record = await (
            await connection.execute(
                "SELECT s.row_set_id, s.row_set_kind, s.account_id, s.obligation_id,"
                " s.digest_profile, s.content_sha256, s.canonical_byte_count, s.row_count,"
                " s.row_manifest_sha256, s.chunk_count, s.row_binding_id, s.row_location_version,"
                " s.rows_state, b.kind, b.identity_sha256"
                " FROM adcp_reporting_row_sets s"
                " JOIN adcp_reporting_row_bindings b ON b.row_binding_id = s.row_binding_id"
                " WHERE s.row_set_id = %s AND s.account_id = %s",
                (row_set_id, account_id),
            )
        ).fetchone()
        if record is None:
            return None
        (
            found_id,
            kind,
            found_account,
            obligation_id,
            profile,
            content_sha256,
            byte_count,
            row_count,
            manifest_sha256,
            chunk_count,
            binding_id,
            location_version,
            rows_state,
            binding_kind,
            identity_sha256,
        ) = record
        if rows_state in ("pruning", "pruned"):
            raise ReportingRowStoreError("ROWS_EXPIRED")
        if rows_state == "unavailable":
            raise ReportingRowStoreError("ROWS_UNAVAILABLE")
        chunk_rows = await (
            await connection.execute(
                "SELECT chunk_index, first_ordinal, row_count, byte_count, sha256, segments,"
                " object_key FROM adcp_reporting_row_chunks WHERE row_set_id = %s"
                " ORDER BY chunk_index",
                (row_set_id,),
            )
        ).fetchall()
        manifests = tuple(
            ReportingRowChunkManifestV1(
                chunk_index=int(row[0]),
                first_ordinal=int(row[1]),
                row_count=int(row[2]),
                byte_count=int(row[3]),
                sha256=str(row[4]),
                segments=tuple(ReportingRowSegmentManifestV1.from_wire(s) for s in row[5]),
            )
            for row in chunk_rows
        )
        try:
            verify_reporting_row_manifests_v1(
                manifests, row_count=int(row_count), row_manifest_sha256=manifest_sha256
            )
        except ReportingRowEncodingError as error:
            raise ReportingRowStoreError("ROWS_INTEGRITY_FAILED", error.detail) from None
        if len(manifests) != chunk_count:
            raise ReportingRowStoreError(
                "ROWS_INTEGRITY_FAILED", "chunk count does not match the row set"
            )
        for row in chunk_rows:
            if (row[6] is not None) != (binding_kind == "object"):
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "chunk location does not match its binding kind"
                )
        return ReportingRowSetHandleV1(
            row_set_id=found_id,
            row_set_kind=kind,
            account_id=found_account,
            obligation_id=obligation_id,
            digest_profile=profile,
            content_sha256=content_sha256,
            canonical_byte_count=int(byte_count),
            row_count=int(row_count),
            row_manifest_sha256=manifest_sha256,
            binding_id=binding_id,
            binding_identity_sha256=identity_sha256,
            kind=binding_kind,
            row_location_version=int(location_version),
            manifests=manifests,
        )

    async def read_page(
        self, connection: Any, handle: ReportingRowSetHandleV1, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        """Verified rows ``[offset, offset + limit)`` in ordinal order."""
        if offset < 0 or limit < 1:
            raise ReportingRowStoreError("INVALID_INPUT", "row page bounds are invalid")
        end = min(handle.row_count, offset + limit)
        rows: list[dict[str, Any]] = []
        for manifest in handle.manifests:
            if offset >= end:
                break
            chunk_end = manifest.first_ordinal + manifest.row_count
            if chunk_end <= offset or manifest.first_ordinal >= end:
                continue
            start_ordinal = max(offset, manifest.first_ordinal)
            first = next(
                index
                for index, segment in enumerate(manifest.segments)
                if segment.first_ordinal + segment.row_count > start_ordinal
            )
            last = first
            while (
                last + 1 < len(manifest.segments)
                and manifest.segments[last + 1].first_ordinal < end
            ):
                last += 1
            first_segment = manifest.segments[first]
            last_segment = manifest.segments[last]
            data = await self._read_chunk_range(
                connection,
                handle,
                manifest,
                first_segment.byte_offset,
                last_segment.byte_offset + last_segment.byte_count - first_segment.byte_offset,
            )
            decoded = _decode_segments(manifest, (first, last), data)
            start_index = max(offset, first_segment.first_ordinal) - first_segment.first_ordinal
            end_index = min(end, last_segment.first_ordinal + last_segment.row_count) - (
                first_segment.first_ordinal
            )
            rows.extend(decoded[start_index:end_index])
        return rows

    async def read_all(
        self,
        connection: Any,
        handle: ReportingRowSetHandleV1,
        *,
        control_totals: Sequence[Any] | None = None,
        max_bytes: int | None = None,
        also_verify: tuple[Sequence[Any], str] | None = None,
    ) -> list[dict[str, Any]]:
        """Every row, verified chunk by chunk and against the committed binding.

        ``control_totals`` are required for ``revision_envelope_v1`` row sets.
        ``also_verify=(totals, sha256)`` additionally proves the stored bytes
        reproduce a header binding that uses a different totals encoding, which
        is how a retained pair-hashed revision is verified: its row set is
        ``rows_v1``, and its header binding hashes ``{name, value}`` pairs.
        """
        if max_bytes is not None and handle.canonical_byte_count > max_bytes:
            raise ReportingRowStoreError(
                "INVALID_INPUT", "row set exceeds the hydration byte limit"
            )
        hasher: ReportingRevisionEnvelopeHasherV1
        if handle.digest_profile == "revision_envelope_v1":
            hasher = create_reporting_revision_envelope_hasher_v1(
                reporting_revision_id=handle.row_set_id,
                row_count=handle.row_count,
                control_totals=list(control_totals or ()),
            )
        else:
            hasher = create_reporting_rows_only_hasher_v1(handle.row_count)
        extra: ReportingRevisionEnvelopeHasherV1 | None = None
        if also_verify is not None:
            extra = create_reporting_revision_envelope_hasher_v1(
                reporting_revision_id=handle.row_set_id,
                row_count=handle.row_count,
                control_totals=list(also_verify[0]),
            )
        rows: list[dict[str, Any]] = []
        for manifest in handle.manifests:
            data = await self._read_chunk_range(
                connection, handle, manifest, 0, manifest.byte_count
            )
            _verify_chunk(manifest, data)
            hasher.update(data)
            if extra is not None:
                extra.update(data)
            rows.extend(_decode_segments(manifest, (0, len(manifest.segments) - 1), data))
        digest = _finish(hasher)
        if (
            digest.sha256 != handle.content_sha256
            or digest.byte_count != handle.canonical_byte_count
        ):
            raise ReportingRowStoreError(
                "ROWS_INTEGRITY_FAILED", "rows do not reproduce the committed binding"
            )
        if extra is not None and also_verify is not None:
            if _finish(extra).sha256 != also_verify[1].lower():
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "rows do not reproduce the revision binding"
                )
        return rows

    async def _read_chunk_range(
        self,
        connection: Any,
        handle: ReportingRowSetHandleV1,
        manifest: ReportingRowChunkManifestV1,
        byte_offset: int,
        byte_count: int,
    ) -> bytes:
        if handle.kind != "postgres":
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE", f"row binding {handle.binding_id} is not configured"
            )
        row = await (
            await connection.execute(
                "SELECT substring(body FROM %s::integer + 1 FOR %s::integer)"
                " FROM adcp_reporting_chunk_bodies"
                " WHERE account_id = %s AND obligation_id = %s AND sha256 = %s",
                (byte_offset, byte_count, handle.account_id, handle.obligation_id, manifest.sha256),
            )
        ).fetchone()
        if row is None or row[0] is None:
            raise ReportingRowStoreError(
                "ROWS_UNAVAILABLE", f"chunk {manifest.chunk_index} body is missing"
            )
        return bytes(row[0])


def _verify_chunk(manifest: ReportingRowChunkManifestV1, data: bytes) -> None:
    try:
        verify_reporting_row_chunk_v1(manifest, data)
    except ReportingRowEncodingError as error:
        raise ReportingRowStoreError("ROWS_INTEGRITY_FAILED", error.detail) from None


def _decode_segments(
    manifest: ReportingRowChunkManifestV1, segment_range: tuple[int, int], data: bytes
) -> list[dict[str, Any]]:
    try:
        return decode_verified_reporting_row_segments_v1(manifest, segment_range, data)
    except ReportingRowEncodingError as error:
        raise ReportingRowStoreError("ROWS_INTEGRITY_FAILED", error.detail) from None


def _finish(hasher: ReportingRevisionEnvelopeHasherV1) -> ReportingRevisionEnvelopeDigestV1:
    try:
        return hasher.digest()
    except ReportingRowEncodingError as error:
        raise ReportingRowStoreError("ROWS_INTEGRITY_FAILED", error.detail) from None
