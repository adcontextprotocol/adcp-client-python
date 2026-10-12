"""Revision row storage: verified canonical chunks in PostgreSQL or object storage.

The Python half of the shared SDK persistence row store (adcontextprotocol/adcp#7996,
section 3). Revision headers stay in the ledger. Rows live as canonical JSONL
chunks (:mod:`adcp.reporting.ledger.row_encoding`) tracked in the
``adcp_reporting_row_*`` tables, which the JavaScript SDK also uses, so one
database can hold chunks written by either SDK. PostgreSQL is the trust anchor:
object stores are untrusted for integrity, and every read verifies the stored bytes
against digests committed in the database before a row is released.

Row location lives in the ``adcp_reporting_row_sets`` side table, keyed by revision
ID, until the shared Core layout. A revision without a row set still carries its
rows in the legacy ``reporting_revision_rows`` table.

Two binding kinds exist. ``postgres`` stores chunk bodies as ``BYTEA`` in the same
transaction as the revision header. ``object`` writes one create-only object per
chunk through a registered provider, pinned to the exact native version the write
produced, so reads and deletes can never touch a later object at the same key.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import inspect
import json
import re
import zlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import rfc8785

from adcp.reporting.ledger.row_encoding import (
    REPORTING_ROW_CHUNK_MAX_BYTES,
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
from adcp.reporting.ledger.row_storage_errors import (
    ReportingRowStoreError,
    ReportingRowStoreErrorCode,
)
from adcp.reporting.ledger.row_storage_object import (
    REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
    ReportingRowObjectContextV1,
    ReportingRowObjectDeleteInputV1,
    ReportingRowObjectGetInputV1,
    ReportingRowObjectProviderV1,
    ReportingRowObjectPutInputV1,
    assert_reporting_row_key_template_v1,
    assert_reporting_row_prefix_v1,
    render_reporting_row_object_key_v1,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.row_encoding import (
        ReportingRevisionEnvelopeDigestV1,
        ReportingRevisionEnvelopeHasherV1,
    )

__all__ = [
    "REPORTING_ROW_STORAGE_DDL_PATH",
    "REPORTING_ROW_STORAGE_TABLES",
    "ReportingObjectRowBindingV1",
    "ReportingPostgresRowBindingV1",
    "ReportingPreparedRowSetV1",
    "ReportingRowBindingSelectionInputV1",
    "ReportingRowChunkLocatorV1",
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

DigestProfile = Literal["revision_envelope_v1", "rows_v1"]
RowSetKind = Literal["revision", "adjustment"]
Compression = Literal["none", "gzip"]

_DEFAULT_BINDING_ID = "postgres"
_BINDING_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
#: Upper bound for adopting an existing object whose encoding may differ byte for byte.
_MAX_ADOPTED_OBJECT_BYTES = 2 * REPORTING_ROW_CHUNK_MAX_BYTES + 1024 * 1024
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ReportingPostgresRowBindingV1:
    """Rows stored as chunk bodies in the ledger's own PostgreSQL schema."""

    kind: Literal["postgres"] = "postgres"


@dataclass(frozen=True, slots=True)
class ReportingObjectRowBindingV1:
    """Rows stored as one object per chunk through a registered provider.

    The definition lives in host code, which makes it the allowlist: a row set
    whose recorded binding identity differs from the configured definition is
    never read from or written to.
    """

    #: Name of a provider registered in ``ReportingRowStorageOptionsV1.providers``.
    provider: str
    #: Non-secret provider coordinates, validated by the provider's closed schema.
    location: Mapping[str, str]
    #: Owned key prefix. Sweeps and deletes never touch keys outside it.
    prefix: str
    #: Object key template; see ``REPORTING_ROW_DEFAULT_KEY_TEMPLATE``.
    key_template: str | None = None
    #: Stored encoding. Digests always cover uncompressed bytes.
    compression: Compression = "gzip"
    #: Host-mapped client reference passed to the provider; never a secret.
    credential_ref: str | None = None
    kind: Literal["object"] = "object"


ReportingRowBindingDefinitionV1 = ReportingPostgresRowBindingV1 | ReportingObjectRowBindingV1


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
    SDKs on one database must use the same value. ``select_row_binding`` picks the
    binding for each new row set; it defaults to the only binding, or ``postgres``.
    """

    bindings: Mapping[str, ReportingRowBindingDefinitionV1] | None = None
    #: Object-storage providers by name.
    providers: Mapping[str, ReportingRowObjectProviderV1] | None = None
    select_row_binding: (
        Callable[[ReportingRowBindingSelectionInputV1], str | Awaitable[str]] | None
    ) = None
    deployment_namespace: str = "default"
    #: Deadline for one row set's uploads. A writer must hold a worker lease that outlives it
    #: by five seconds; the default fits the producer's default 60 second lease.
    write_deadline_seconds: float = 30.0
    #: Deadline for one object read.
    read_deadline_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ReportingRowChunkLocatorV1:
    """Where one chunk was stored; recorded in the chunk manifest."""

    object_key: str
    native_version: str
    compression: Compression
    physical_sha256: str
    physical_byte_count: int
    #: Whether this upload created the object, as opposed to adopting identical bytes.
    created: bool = False


@dataclass(frozen=True, slots=True)
class _PlannedObject:
    key: str
    physical_bytes: bytes
    physical_sha256: str
    compression: Compression


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
    #: Object kind only: one planned object per chunk.
    objects: tuple[_PlannedObject, ...] = ()


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
    #: Object kind only, by chunk index.
    locators: tuple[ReportingRowChunkLocatorV1 | None, ...] = ()


def _sha256_hex(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _key_template(binding: ReportingObjectRowBindingV1) -> str:
    return binding.key_template or REPORTING_ROW_DEFAULT_KEY_TEMPLATE


def _binding_identity_config(binding: ReportingRowBindingDefinitionV1) -> dict[str, Any]:
    # camelCase on purpose: the JavaScript SDK hashes these exact names, and a binding
    # row is only shared between SDKs when both compute the same identity digest.
    if isinstance(binding, ReportingObjectRowBindingV1):
        return {
            "location": dict(binding.location),
            "prefix": binding.prefix,
            "keyTemplate": _key_template(binding),
        }
    return {}


def _binding_provider(binding: ReportingRowBindingDefinitionV1) -> str:
    return binding.provider if isinstance(binding, ReportingObjectRowBindingV1) else binding.kind


def _binding_identity_sha256(binding: ReportingRowBindingDefinitionV1) -> str:
    return _sha256_hex(
        rfc8785.dumps([binding.kind, _binding_provider(binding), _binding_identity_config(binding)])
    )


def _binding_operational_config(binding: ReportingRowBindingDefinitionV1) -> dict[str, Any]:
    if isinstance(binding, ReportingObjectRowBindingV1):
        return {
            "compression": binding.compression,
            **({"credentialRef": binding.credential_ref} if binding.credential_ref else {}),
        }
    return {}


def _encode_and_digest(
    rows: Sequence[Mapping[str, Any]],
    row_set_id: str,
    digest_profile: DigestProfile,
    control_totals: Sequence[Any] | None,
) -> tuple[ReportingEncodedRowsV1, ReportingRevisionEnvelopeDigestV1]:
    encoded = encode_reporting_rows_v1(rows)
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
    return encoded, hasher.digest()


def _physical_chunks(chunks: Sequence[bytes], compression: Compression) -> list[bytes]:
    if compression == "none":
        return list(chunks)
    return [gzip.compress(data, compresslevel=6, mtime=0) for data in chunks]


def _inflate(physical: bytes, compression: Compression, byte_count: int) -> bytes | None:
    """Decompress to exactly ``byte_count`` bytes, refusing trailing data and extra members."""
    if compression == "none":
        return physical if len(physical) == byte_count else None
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        logical = decompressor.decompress(physical, byte_count + 1)
        if (
            not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
            or len(logical) != byte_count
        ):
            return None
        return logical
    except zlib.error:
        return None


class ReportingRowStorageV1:
    """Reads and writes revision row sets over a caller-owned connection.

    Construct one per store. :meth:`ready` registers the configured bindings
    against the installed schema and probes every object provider; it must
    succeed before any write.
    """

    def __init__(self, options: ReportingRowStorageOptionsV1 | None = None) -> None:
        options = options or ReportingRowStorageOptionsV1()
        bindings = dict(options.bindings or {_DEFAULT_BINDING_ID: ReportingPostgresRowBindingV1()})
        if not bindings:
            raise ReportingRowStoreError("INVALID_INPUT", "at least one row binding is required")
        providers = dict(options.providers or {})
        for binding_id, binding in bindings.items():
            if not _BINDING_ID.match(binding_id):
                raise ReportingRowStoreError(
                    "INVALID_INPUT", f"invalid row binding ID {binding_id}"
                )
            if isinstance(binding, ReportingPostgresRowBindingV1):
                continue
            if not isinstance(binding, ReportingObjectRowBindingV1):
                raise ReportingRowStoreError(
                    "INVALID_INPUT", f"row binding {binding_id} has an unsupported kind"
                )
            provider = providers.get(binding.provider)
            if provider is None:
                raise ReportingRowStoreError(
                    "INVALID_INPUT", f"row binding {binding_id} names an unknown provider"
                )
            provider.validate_location(binding.location)
            assert_reporting_row_prefix_v1(binding.prefix)
            assert_reporting_row_key_template_v1(_key_template(binding))
            if binding.compression not in ("none", "gzip"):
                raise ReportingRowStoreError(
                    "INVALID_INPUT", f"row binding {binding_id} has an unsupported compression"
                )
            if binding.credential_ref is not None and not _CREDENTIAL_REF.match(
                binding.credential_ref
            ):
                raise ReportingRowStoreError(
                    "INVALID_INPUT", f"row binding {binding_id} has an invalid credential_ref"
                )
        namespace = options.deployment_namespace
        if not namespace or len(namespace) > 256:
            raise ReportingRowStoreError(
                "INVALID_INPUT", "deployment_namespace must be 1-256 characters"
            )
        for name, seconds in (
            ("write_deadline_seconds", options.write_deadline_seconds),
            ("read_deadline_seconds", options.read_deadline_seconds),
        ):
            if not 1 <= seconds <= 600:
                raise ReportingRowStoreError("INVALID_INPUT", f"{name} must be between 1 and 600")
        self._options = options
        self._bindings: Mapping[str, ReportingRowBindingDefinitionV1] = bindings
        self._providers: Mapping[str, ReportingRowObjectProviderV1] = providers
        self._deployment_namespace = namespace
        self._installation: tuple[str, str] | None = None
        self._ready_lock = asyncio.Lock()

    # -- setup ------------------------------------------------------------

    @property
    def write_deadline_milliseconds(self) -> int:
        """Lease budget a writer must still hold before it uploads."""
        return int(self._options.write_deadline_seconds * 1000)

    @property
    def installation_identity(self) -> tuple[str, str]:
        """``(installation_id, namespace_key)``; available after :meth:`ready`."""
        if self._installation is None:
            raise ReportingRowStoreError("STATE_UNAVAILABLE", "row storage is not ready")
        return self._installation

    @property
    def is_ready(self) -> bool:
        return self._installation is not None

    async def ready(self, connection: Any) -> None:
        """Probe the schema and providers, and register (or verify) the bindings. Idempotent."""
        if self.is_ready:
            return
        async with self._ready_lock:
            if not self.is_ready:
                await self._register(connection)

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
        # Probe every provider before the first insert: provider I/O must not run while
        # this transaction holds binding rows, and a fixed insert order keeps two starting
        # processes from deadlocking on each other's bindings.
        for binding in self._bindings.values():
            if isinstance(binding, ReportingObjectRowBindingV1):
                await self._probe(binding)
        for binding_id, binding in sorted(self._bindings.items()):
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
                    _binding_provider(binding),
                    json.dumps(_binding_identity_config(binding)),
                    identity_sha256,
                    json.dumps(_binding_operational_config(binding)),
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

    async def _probe(self, binding: ReportingObjectRowBindingV1) -> None:
        provider = self._providers[binding.provider]
        context = ReportingRowObjectContextV1(self._options.read_deadline_seconds)
        await self._provider_call(
            provider.probe(binding.location, binding.prefix, context, binding.credential_ref),
            self._options.read_deadline_seconds,
        )

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

    # -- providers --------------------------------------------------------

    @staticmethod
    async def _provider_call(operation: Awaitable[_T], timeout: float) -> _T:
        """Collapse provider failures to stable, secret-free codes."""
        try:
            return await asyncio.wait_for(operation, timeout)
        except ReportingRowStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise ReportingRowStoreError("DEADLINE_EXCEEDED") from None
        except Exception:
            raise ReportingRowStoreError("PROVIDER_UNAVAILABLE") from None

    def _object_binding(
        self, binding_id: str, identity_sha256: str | None = None
    ) -> tuple[ReportingObjectRowBindingV1, ReportingRowObjectProviderV1]:
        binding = self._bindings.get(binding_id)
        if (
            not isinstance(binding, ReportingObjectRowBindingV1)
            or (
                identity_sha256 is not None and _binding_identity_sha256(binding) != identity_sha256
            )
            or binding.provider not in self._providers
        ):
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE", f"row binding {binding_id} is not configured"
            )
        return binding, self._providers[binding.provider]

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
        period_start: str | None = None,
        binding_id: str | None = None,
    ) -> ReportingPreparedRowSetV1:
        """Encode rows, prove them against the committed digest, choose a binding, plan objects.

        ``digest_profile`` is ``revision_envelope_v1`` (the protocol revision
        binding, with ``control_totals``) or ``rows_v1`` (``sha256(JCS(rows))``).
        When ``expected_sha256`` is given the rows must reproduce it.
        ``period_start`` (RFC 3339) feeds ``{period_date}`` in object keys.
        """
        if self._installation is None:
            raise ReportingRowStoreError("STATE_UNAVAILABLE", "row storage is not ready")
        try:
            # Canonicalizing, hashing and compressing a large revision is CPU work; keep it
            # off the event loop so concurrent reads and heartbeats are not stalled.
            encoded, digest = await asyncio.to_thread(
                _encode_and_digest, rows, row_set_id, digest_profile, control_totals
            )
        except ReportingRowEncodingError as error:
            raise ReportingRowStoreError("INVALID_INPUT", error.detail) from None
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
        objects: tuple[_PlannedObject, ...] = ()
        if isinstance(binding, ReportingObjectRowBindingV1):
            namespace_key = self._installation[1]
            planned = []
            compressed = await asyncio.to_thread(
                _physical_chunks, [chunk.data for chunk in encoded.chunks], binding.compression
            )
            for chunk, physical in zip(encoded.chunks, compressed, strict=True):
                planned.append(
                    _PlannedObject(
                        key=render_reporting_row_object_key_v1(
                            template=_key_template(binding),
                            prefix=binding.prefix,
                            namespace_key=namespace_key,
                            account_id=account_id,
                            row_set_id=row_set_id,
                            period_start=period_start,
                            finality=finality,
                            content_sha256=digest.sha256,
                            chunk_index=chunk.manifest.chunk_index,
                            compression=binding.compression,
                        ),
                        physical_bytes=physical,
                        physical_sha256=_sha256_hex(physical),
                        compression=binding.compression,
                    )
                )
            objects = tuple(planned)
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
            objects=objects,
        )

    async def upload(
        self,
        prepared: ReportingPreparedRowSetV1,
        intent_id: str,
        on_created: Callable[[str, str], Awaitable[None]],
    ) -> list[ReportingRowChunkLocatorV1]:
        """Upload a prepared object-kind row set, create-only.

        An object already at a planned key is adopted only when its bytes decode to
        the exact chunk; otherwise ``CONTENT_CONFLICT``. ``on_created`` is awaited
        with ``(object_key, native_version)`` for every object this call created,
        before the next upload, so the caller can record it in its write intent.
        Adopted objects are never reported, so a sweep never deletes them. If ``on_created``
        raises, the object it was told about stays in the store: an upload never deletes, because
        its write intent may have been taken over by a writer that already adopted the object.
        """
        binding = prepared.binding
        if not isinstance(binding, ReportingObjectRowBindingV1):
            raise ReportingRowStoreError("INVALID_INPUT", "only object-kind row sets are uploaded")
        provider = self._providers[binding.provider]
        loop = asyncio.get_running_loop()
        # One budget for the whole row set, not one per chunk: the caller's lease check
        # covers exactly this long, so N chunks must not stretch to N times the deadline.
        deadline = loop.time() + self._options.write_deadline_seconds

        def remaining() -> float:
            left = deadline - loop.time()
            if left <= 0:
                raise ReportingRowStoreError("DEADLINE_EXCEEDED")
            return left

        installation_id = self.installation_identity[0]
        locators: list[ReportingRowChunkLocatorV1] = []
        for index, planned in enumerate(prepared.objects):
            manifest = prepared.encoded.manifests[index]
            budget = remaining()
            put = await self._provider_call(
                provider.put_if_absent(
                    ReportingRowObjectPutInputV1(
                        location=binding.location,
                        key=planned.key,
                        data=planned.physical_bytes,
                        content_type=(
                            "application/gzip"
                            if planned.compression == "gzip"
                            else "application/x-ndjson"
                        ),
                        metadata={"adcp-installation": installation_id, "adcp-intent": intent_id},
                        credential_ref=binding.credential_ref,
                    ),
                    ReportingRowObjectContextV1(budget),
                ),
                budget,
            )
            if put.created:
                # Never delete here. If recording fails because the intent was taken over, another
                # writer may already have adopted this version and committed it; a version-pinned
                # delete would remove a committed revision's bytes for good. Leave the object: at
                # worst it is storage a later sweep removes through the reference check (the same
                # choice the JavaScript SDK makes).
                await on_created(planned.key, put.native_version)
                locators.append(
                    ReportingRowChunkLocatorV1(
                        planned.key,
                        put.native_version,
                        planned.compression,
                        planned.physical_sha256,
                        len(planned.physical_bytes),
                        created=True,
                    )
                )
                continue
            budget = remaining()
            existing = await self._provider_call(
                provider.get(
                    ReportingRowObjectGetInputV1(
                        location=binding.location,
                        key=planned.key,
                        native_version=put.native_version,
                        max_bytes=_MAX_ADOPTED_OBJECT_BYTES,
                        credential_ref=binding.credential_ref,
                    ),
                    ReportingRowObjectContextV1(budget),
                ),
                budget,
            )
            logical = (
                _inflate(existing, planned.compression, manifest.byte_count)
                if existing is not None
                else None
            )
            if existing is None or logical is None or _sha256_hex(logical) != manifest.sha256:
                raise ReportingRowStoreError(
                    "CONTENT_CONFLICT", f"object for chunk {index} holds different bytes"
                )
            locators.append(
                ReportingRowChunkLocatorV1(
                    planned.key,
                    put.native_version,
                    planned.compression,
                    _sha256_hex(existing),
                    len(existing),
                    created=False,
                )
            )
        return locators

    async def write_in_transaction(
        self,
        connection: Any,
        prepared: ReportingPreparedRowSetV1,
        locators: Sequence[ReportingRowChunkLocatorV1] | None = None,
        shared_from_row_set_id: str | None = None,
    ) -> None:
        """Persist a prepared row set inside the caller's ledger transaction.

        Bodies for the ``postgres`` kind, or the uploaded locators for the object
        kind. Replays of an identical row set are no-ops; a conflicting row set
        under the same ID is refused.
        """
        encoded = prepared.encoded
        is_object = isinstance(prepared.binding, ReportingObjectRowBindingV1)
        if is_object and (locators is None or len(locators) != len(encoded.chunks)):
            raise ReportingRowStoreError(
                "INVALID_INPUT", "object-kind row sets need one locator per chunk"
            )
        cursor = await connection.execute(
            "INSERT INTO adcp_reporting_row_sets"
            " (row_set_id, row_set_kind, account_id, obligation_id, encoding, digest_profile,"
            "  content_sha256, canonical_byte_count, row_count, row_manifest_sha256,"
            "  chunk_count, row_binding_id, rows_shared_from_row_set_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
                shared_from_row_set_id,
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
        for index, chunk in enumerate(encoded.chunks):
            manifest = chunk.manifest
            locator = locators[index] if locators else None
            if locator is None:
                await connection.execute(
                    "INSERT INTO adcp_reporting_chunk_bodies"
                    " (account_id, obligation_id, sha256, body)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (prepared.account_id, prepared.obligation_id, manifest.sha256, chunk.data),
                )
            await connection.execute(
                "INSERT INTO adcp_reporting_row_chunks"
                " (row_set_id, chunk_index, account_id, obligation_id, first_ordinal, row_count,"
                "  byte_count, sha256, segments, object_key, native_version, compression,"
                "  physical_sha256, physical_byte_count)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
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
                    locator.object_key if locator else None,
                    locator.native_version if locator else None,
                    locator.compression if locator else "none",
                    locator.physical_sha256 if locator else None,
                    locator.physical_byte_count if locator else None,
                ),
            )

    async def find_shareable(
        self, connection: Any, prepared: ReportingPreparedRowSetV1
    ) -> tuple[str, list[ReportingRowChunkLocatorV1]] | None:
        """A live row set in the same obligation and binding with byte-identical rows.

        Identical rows are those with the same ``row_manifest_sha256``. Sharing
        never crosses an obligation, so it never crosses a consumer. A header-only
        revision references the existing chunks instead of storing them again.
        Takes ``FOR SHARE`` on the row set so retention cannot retire it before the
        caller's transaction commits.
        """
        if not prepared.encoded.chunks:
            return None
        found = await (
            await connection.execute(
                "SELECT row_set_id FROM adcp_reporting_row_sets"
                " WHERE obligation_id = %s AND account_id = %s AND row_binding_id = %s"
                " AND row_manifest_sha256 = %s AND rows_state = 'live' AND row_set_id <> %s"
                " ORDER BY recorded_at DESC, row_set_id LIMIT 1 FOR SHARE",
                (
                    prepared.obligation_id,
                    prepared.account_id,
                    prepared.binding_id,
                    prepared.encoded.row_manifest_sha256,
                    prepared.row_set_id,
                ),
            )
        ).fetchone()
        if found is None:
            return None
        chunks = await (
            await connection.execute(
                "SELECT object_key, native_version, compression, physical_sha256,"
                " physical_byte_count FROM adcp_reporting_row_chunks"
                " WHERE row_set_id = %s ORDER BY chunk_index",
                (found[0],),
            )
        ).fetchall()
        if len(chunks) != len(prepared.encoded.chunks):
            return None
        if not isinstance(prepared.binding, ReportingObjectRowBindingV1):
            return found[0], []
        if any(chunk[0] is None for chunk in chunks):
            return None
        return found[0], [
            ReportingRowChunkLocatorV1(
                chunk[0], chunk[1], chunk[2], chunk[3], int(chunk[4]), created=False
            )
            for chunk in chunks
        ]

    async def delete_objects(self, binding_id: str, objects: Sequence[tuple[str, str]]) -> None:
        """Delete exact ``(object_key, native_version)`` pairs through the binding's provider.

        ``absent`` counts as success. Only keys under the installation's owned
        prefix are accepted, so a corrupted locator cannot delete another
        installation's bytes.
        """
        if self._installation is None:
            raise ReportingRowStoreError("STATE_UNAVAILABLE", "row storage is not ready")
        binding, provider = self._object_binding(binding_id)
        owned_prefix = f"{binding.prefix}/{self._installation[1]}/"
        context = ReportingRowObjectContextV1(self._options.write_deadline_seconds)
        for object_key, native_version in objects:
            if not object_key.startswith(owned_prefix):
                raise ReportingRowStoreError(
                    "UNSAFE_BINDING", "refusing to delete an object outside the owned prefix"
                )
            await self._provider_call(
                provider.delete(
                    ReportingRowObjectDeleteInputV1(
                        binding.location, object_key, native_version, binding.credential_ref
                    ),
                    context,
                ),
                self._options.write_deadline_seconds,
            )

    # -- object lifetime --------------------------------------------------
    #
    # Chunks are adopted create-only, so a custom key template that omits an identity
    # placeholder lets two row sets name the same (binding, key, version). Every path that
    # deletes an object and every commit that adopts one serialize on a transaction-scoped
    # advisory lock per object: the deleter checks references and deletes under it, and the
    # committer re-verifies the adopted object under it, so whichever runs second sees the
    # other's effect. The lock text is shared with the JavaScript SDK and hashed in SQL so
    # both SDKs derive the same lock id in one database.

    @staticmethod
    def _object_lock_text(binding_id: str, object_key: str) -> str:
        return f"adcp-reporting-object:{binding_id}:{object_key}"

    async def lock_and_verify_adopted(
        self,
        connection: Any,
        binding_id: str,
        locators: Sequence[ReportingRowChunkLocatorV1],
    ) -> None:
        """Hold the object lock for every adopted object and prove each still exists.

        Call inside the ledger transaction, after the account lock and before the chunk rows
        are inserted. Objects this upload created are protected by its open write intent
        instead. (A writer's own just-created object is not locked: the window between its
        create and the intent record is the same one the JavaScript SDK leaves open.)
        """
        adopted = sorted((x for x in locators if not x.created), key=lambda x: x.object_key)
        if not adopted:
            return
        binding, provider = self._object_binding(binding_id)
        for locator in adopted:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (self._object_lock_text(binding_id, locator.object_key),),
            )
        for locator in adopted:
            budget = self._options.write_deadline_seconds
            existing = await self._provider_call(
                provider.get(
                    ReportingRowObjectGetInputV1(
                        location=binding.location,
                        key=locator.object_key,
                        native_version=locator.native_version,
                        max_bytes=_MAX_ADOPTED_OBJECT_BYTES,
                        credential_ref=binding.credential_ref,
                    ),
                    ReportingRowObjectContextV1(budget),
                ),
                budget,
            )
            if existing is None or _sha256_hex(existing) != locator.physical_sha256:
                raise ReportingRowStoreError(
                    "STATE_UNAVAILABLE",
                    "an adopted object was deleted before this commit; stage the rows again",
                )

    async def delete_unreferenced(
        self,
        pool: Any,
        binding_id: str,
        objects: Sequence[tuple[str, str]],
        *,
        obligation_id: str | None = None,
        row_set_id: str | None = None,
    ) -> int:
        """Delete each object nothing outside the operation's scope still needs.

        An object is kept while a chunk of a live or unavailable row set of the same binding
        references it (outside ``obligation_id`` / ``row_set_id``, the thing being deleted),
        or another open write intent created it. Row sets already pruning or pruned do not
        count: their own operation deletes the object. One transaction per object: the lock,
        the reference check and the provider delete happen together, so a concurrent commit
        adopting the object either already references it (it is kept) or finds it gone and
        fails closed. Returns the number of objects deleted.
        """
        deleted = 0
        for object_key, native_version in objects:
            async with pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (self._object_lock_text(binding_id, object_key),),
                )
                referenced = await (
                    await connection.execute(
                        "(SELECT 1 FROM adcp_reporting_row_chunks chunk"
                        "   JOIN adcp_reporting_row_sets row_set"
                        "     ON row_set.row_set_id = chunk.row_set_id"
                        "  WHERE row_set.row_binding_id = %(binding)s"
                        "    AND chunk.object_key = %(key)s AND chunk.native_version = %(version)s"
                        "    AND row_set.rows_state IN ('live', 'unavailable')"
                        "    AND (%(obligation)s::text IS NULL"
                        "         OR chunk.obligation_id <> %(obligation)s)"
                        "    AND (%(row_set)s::text IS NULL OR chunk.row_set_id <> %(row_set)s)"
                        "  LIMIT 1)"
                        " UNION ALL"
                        " (SELECT 1 FROM adcp_reporting_row_write_intents intent"
                        "  WHERE intent.row_binding_id = %(binding)s AND intent.state = 'open'"
                        "    AND intent.created_objects @> %(created)s::jsonb LIMIT 1)",
                        {
                            "binding": binding_id,
                            "key": object_key,
                            "version": native_version,
                            "obligation": obligation_id,
                            "row_set": row_set_id,
                            "created": json.dumps(
                                [{"objectKey": object_key, "nativeVersion": native_version}]
                            ),
                        },
                    )
                ).fetchone()
                if referenced is not None:
                    continue
                await self.delete_objects(binding_id, [(object_key, native_version)])
                deleted += 1
        return deleted

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
                " object_key, native_version, compression, physical_sha256, physical_byte_count"
                " FROM adcp_reporting_row_chunks WHERE row_set_id = %s ORDER BY chunk_index",
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
        locators: list[ReportingRowChunkLocatorV1 | None] = []
        for row in chunk_rows:
            if (row[6] is not None) != (binding_kind == "object"):
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", "chunk location does not match its binding kind"
                )
            locators.append(
                ReportingRowChunkLocatorV1(
                    str(row[6]), str(row[7]), row[8], str(row[9]), int(row[10])
                )
                if row[6] is not None
                else None
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
            locators=tuple(locators),
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
        if handle.kind == "postgres":
            row = await (
                await connection.execute(
                    "SELECT substring(body FROM %s::integer + 1 FOR %s::integer)"
                    " FROM adcp_reporting_chunk_bodies"
                    " WHERE account_id = %s AND obligation_id = %s AND sha256 = %s",
                    (
                        byte_offset,
                        byte_count,
                        handle.account_id,
                        handle.obligation_id,
                        manifest.sha256,
                    ),
                )
            ).fetchone()
            if row is None or row[0] is None:
                raise ReportingRowStoreError(
                    "ROWS_UNAVAILABLE", f"chunk {manifest.chunk_index} body is missing"
                )
            return bytes(row[0])
        binding, provider = self._object_binding(handle.binding_id, handle.binding_identity_sha256)
        locator = handle.locators[manifest.chunk_index]
        if locator is None:
            raise ReportingRowStoreError("ROWS_INTEGRITY_FAILED", "chunk location is missing")
        context = ReportingRowObjectContextV1(self._options.read_deadline_seconds)
        timeout = self._options.read_deadline_seconds
        if locator.compression == "none" and provider.ranged_reads:
            ranged = await self._provider_call(
                provider.get(
                    ReportingRowObjectGetInputV1(
                        location=binding.location,
                        key=locator.object_key,
                        native_version=locator.native_version,
                        max_bytes=byte_count,
                        byte_range=(byte_offset, byte_count),
                        credential_ref=binding.credential_ref,
                    ),
                    context,
                ),
                timeout,
            )
            if ranged is None:
                raise ReportingRowStoreError(
                    "ROWS_UNAVAILABLE", f"chunk {manifest.chunk_index} object is missing"
                )
            if len(ranged) != byte_count:
                raise ReportingRowStoreError(
                    "ROWS_INTEGRITY_FAILED", f"chunk {manifest.chunk_index} range is truncated"
                )
            return bytes(ranged)
        stored = await self._provider_call(
            provider.get(
                ReportingRowObjectGetInputV1(
                    location=binding.location,
                    key=locator.object_key,
                    native_version=locator.native_version,
                    max_bytes=locator.physical_byte_count,
                    credential_ref=binding.credential_ref,
                ),
                context,
            ),
            timeout,
        )
        if stored is None:
            raise ReportingRowStoreError(
                "ROWS_UNAVAILABLE", f"chunk {manifest.chunk_index} object is missing"
            )
        physical = bytes(stored)
        if (
            len(physical) != locator.physical_byte_count
            or _sha256_hex(physical) != locator.physical_sha256
        ):
            raise ReportingRowStoreError(
                "ROWS_INTEGRITY_FAILED", f"chunk {manifest.chunk_index} stored bytes differ"
            )
        logical = _inflate(physical, locator.compression, manifest.byte_count)
        if logical is None:
            raise ReportingRowStoreError(
                "ROWS_INTEGRITY_FAILED", f"chunk {manifest.chunk_index} does not decompress"
            )
        return logical[byte_offset : byte_offset + byte_count]


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
