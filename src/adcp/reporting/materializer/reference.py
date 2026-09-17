"""Deterministic memory destination for tests/development. NEVER production support."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from importlib.resources import files
from typing import Any, Literal, cast, final

from adcp.reporting.evidence import ReportingCanonicalDigest
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDestinationBinding,
    ReportingResourceRecord,
)
from adcp.reporting.ledger.models import ReportingDefinitionBinding
from adcp.reporting.materializer._json import strict_reporting_json
from adcp.reporting.materializer.contracts import (
    ReportingCanonicalization,
    ReportingDestinationLocator,
    ReportingDestinationPage,
    ReportingDestinationRequest,
    ReportingDestinationSession,
    ReportingIOContext,
    ReportingIOPhase,
    ReportingNativeObservation,
    ReportingPreparedRevision,
    ReportingVerificationKey,
    ReportingWriterCapability,
    ReportingWriterError,
    ReportingWriterFailure,
    binding_fingerprint,
    failure,
)
from adcp.reporting.materializer.verification import (
    ReportingRevisionVerifier,
    ReportingRevisionVerifierRegistry,
)


def reference_verifier(
    capability: ReportingWriterCapability | None = None,
) -> ReportingRevisionVerifier:
    """One installed example definition/canonicalization; no network resolution."""
    root = files("adcp.reporting.materializer").joinpath("assets")
    definition = root.joinpath("reference-definition.json").read_bytes()
    schema = root.joinpath("reference-row-schema.json").read_bytes()
    contract = root.joinpath("reference-canonicalization.json").read_bytes()
    capability = capability or ReportingWriterCapability(
        "file_transfer",
        "reference-memory",
        "jsonl",
        "canonical_digest",
        "producer",
        "immutable_location",
        "sha256",
        "conditional_create",
    )
    return ReportingRevisionVerifier(
        ReportingVerificationKey(
            "reference-report-v1",
            "paid_media_delivery",
            ReportingDefinitionBinding(
                "https://contracts.example.test/reference-definition.json",
                hashlib.sha256(definition).hexdigest(),
                "1.0.0",
                "https://contracts.example.test/reference-row-schema.json",
                hashlib.sha256(schema).hexdigest(),
                monetary_metric_units=(("spend", "USD"),),
                monetary_control_total_units=(("spend", "USD"),),
            ),
            ReportingCanonicalization(
                "reference-jcs-rows-v1",
                "https://contracts.example.test/reference-canonicalization.json",
                hashlib.sha256(contract).hexdigest(),
            ),
            capability,
        ),
        definition,
        schema,
        contract,
    )


def reference_digest(
    verifier: ReportingRevisionVerifier, rows: list[object]
) -> ReportingCanonicalDigest:
    encoded, _ = verifier.canonicalize(rows)
    contract = verifier.key.canonicalization
    return ReportingCanonicalDigest(
        hashlib.sha256(b"[" + b",".join(encoded) + b"]").hexdigest(),
        contract.canonicalization_id,
        contract.canonicalization_uri,
        contract.canonicalization_sha256,
    )


@dataclass(frozen=True)
class _Artifact:
    rows: tuple[bytes, ...]
    manifest: bytes | None
    objects: tuple[tuple[str, bytes], ...]
    locator: ReportingDestinationLocator


@final
class ReferenceReportingDestinationWriter:
    """Conditional, deterministic external identities in shared process memory.

    Losing this object loses its artifacts. No durability or Managed capability
    follows from this type or its descriptors. The production flag is a constant
    property, with no constructor/config override.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("the non-production reference writer cannot be promoted by subclassing")

    def __init__(self, capabilities: tuple[ReportingWriterCapability, ...]) -> None:
        if type(capabilities) is not tuple or any(
            type(c) is not ReportingWriterCapability for c in capabilities
        ):
            raise ValueError("reference capabilities require an immutable typed tuple")
        self._capabilities = capabilities
        self._artifacts: dict[str, _Artifact] = {}
        self.write_effects = 0
        self.open_count = 0
        self.close_count = 0

    @property
    def capabilities(self) -> tuple[ReportingWriterCapability, ...]:
        return self._capabilities

    @property
    def production_eligible(self) -> Literal[False]:
        return False

    def __repr__(self) -> str:
        return "<ReferenceReportingDestinationWriter non-production>"


@dataclass(frozen=True)
class ReferenceReportingResolver:
    writer: ReferenceReportingDestinationWriter
    registry: ReportingRevisionVerifierRegistry
    bindings: tuple[ReportingDestinationBinding, ...] = field(repr=False)
    _revoked: set[ReportingDeliveryPrincipal] = field(
        default_factory=set, init=False, repr=False, compare=False
    )
    _rotations: list[int] = field(
        default_factory=lambda: [0], init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.bindings) is not tuple or any(
            type(b) is not ReportingDestinationBinding for b in self.bindings
        ):
            raise ValueError("reference resolver requires frozen bindings")

    def revoke(self, principal: ReportingDeliveryPrincipal) -> None:
        self._revoked.add(principal)

    def rotate(self) -> None:
        self._rotations[0] += 1

    def resolve(
        self,
        request: ReportingDestinationRequest,
        *,
        phase: ReportingIOPhase,
        context: ReportingIOContext,
    ) -> ReportingDestinationSession:
        return _Session(self, request, phase, context)


class _Session(ReportingDestinationSession):
    def __init__(
        self,
        resolver: ReferenceReportingResolver,
        request: ReportingDestinationRequest,
        phase: ReportingIOPhase,
        context: ReportingIOContext,
    ) -> None:
        super().__init__(request, phase, context)
        self._resolver = resolver
        self._credential: object | None = None

    async def _open(self) -> None:
        resolver = self._resolver
        resolver.writer.open_count += 1
        resolver.registry.require(self.request.verification_key)
        if self.request.verification_key.capability not in resolver.writer.capabilities:
            raise failure("UNSUPPORTED_VERIFICATION")
        if self.request.principal in resolver._revoked or not any(
            b.principal == self.request.principal
            and b.generation_key == self.request.generation
            and b.destination_ref == self.request.destination_ref
            and b.trusted_binding_ref == self.request.trusted_binding_ref
            and binding_fingerprint(b) == self.request.binding_fingerprint
            for b in resolver.bindings
        ):
            raise failure("AUTHORIZATION_DENIED")
        # A test credential exists only in the owned session and is discarded on
        # every close. Real resolvers retrieve fresh credentials at this point.
        self._credential = (object(), resolver._rotations[0])

    async def _close(self) -> None:
        self._credential = None
        self._resolver.writer.close_count += 1

    def _check(self, phase: ReportingIOPhase) -> None:
        if self._credential is None or self._closed or self.phase != phase:
            raise failure("AUTHORIZATION_DENIED")

    async def write(self, content: ReportingPreparedRevision) -> ReportingDestinationLocator:
        self._check("write")
        request = self.request
        if content.request != request:
            raise failure("BINDING_MISMATCH")
        writer = self._resolver.writer
        existing = writer._artifacts.get(request.external_id)
        if existing is not None:
            if existing.rows != content.rows:
                raise ReportingWriterError(
                    ReportingWriterFailure("WRITE_FAILED", "never", "applied")
                )
            return existing.locator
        cap = request.verification_key.capability
        prefix = request.external_id
        objects: tuple[tuple[str, bytes], ...] = ()
        manifest: bytes | None = None
        if cap.method == "file_transfer":
            objects = tuple(
                (
                    f"{prefix}/part-{index // 250:06d}.jsonl",
                    b"".join(r + b"\n" for r in content.rows[index : index + 250]),
                )
                for index in range(0, max(1, len(content.rows)), 250)
            )
            period = content.obligation.period
            manifest = strict_reporting_json(
                {
                    "manifest_version": "1.0",
                    "complete": True,
                    "reporting_revision_id": request.reporting_revision_id,
                    "reporting_obligation_id": request.reporting_obligation_id,
                    "reporting_materialization_id": request.reporting_materialization_id,
                    "period": {
                        "start": period.start.isoformat(),
                        "end": period.end.isoformat(),
                        "source_timezone": period.source_timezone,
                    },
                    "format": cap.format,
                    "compression": "none",
                    "files": [
                        {
                            "object_ref": ref,
                            "size_bytes": len(raw),
                            "row_count": raw.count(b"\n"),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        }
                        for ref, raw in objects
                    ],
                    "total_size_bytes": sum(len(raw) for _, raw in objects),
                    "row_count": len(content.rows),
                    "control_totals": [
                        t.to_wire() for t in content.revision.managed_control_totals or ()
                    ],
                    "created_at": content.revision.created_at.isoformat(),
                }
            )
        resource = ReportingResourceRecord(
            resource_ref=prefix,
            kind=cast(
                Literal["manifest", "dataset", "warehouse_relation"],
                {
                    "file_transfer": "manifest",
                    "dataset_share": "dataset",
                    "warehouse_materialization": "warehouse_relation",
                }[cap.method],
            ),
            location=f"{prefix}/manifest.json" if manifest is not None else f"{prefix}/table",
            immutability=cap.immutability,
            expires_at=max(
                content.delivery.resource_retained_until,
                self.context.deadline_at + timedelta(days=content.binding.resource_retention_days),
            ),
            native_version_ref=(
                f"reference-{prefix}" if cap.immutability == "native_version" else None
            ),
            manifest_sha256=hashlib.sha256(manifest).hexdigest() if manifest is not None else None,
            object_refs=tuple(ref for ref, _ in objects),
            reader_compatibility=content.binding.reader_compatibility,
        )
        locator = ReportingDestinationLocator(
            request.external_id, request.binding_fingerprint, resource
        )
        writer._artifacts[request.external_id] = _Artifact(content.rows, manifest, objects, locator)
        writer.write_effects += 1
        return locator

    def _artifact(self, locator: ReportingDestinationLocator) -> _Artifact:
        self._check("readback")
        if (
            locator.external_id != self.request.external_id
            or locator.binding_fingerprint != self.request.binding_fingerprint
        ):
            raise failure("BINDING_MISMATCH")
        artifact = self._resolver.writer._artifacts.get(locator.external_id)
        if artifact is None:
            raise failure("RESOURCE_UNAVAILABLE")
        if artifact.locator.resource.location != locator.resource.location:
            raise failure("BINDING_MISMATCH")
        return artifact

    async def read_rows(
        self, locator: ReportingDestinationLocator, *, cursor: str | None, limit: int
    ) -> ReportingDestinationPage:
        artifact = self._artifact(locator)
        offset = 0
        prefix = f"{self.request.external_id}:"
        if cursor is not None:
            if not cursor.startswith(prefix) or not cursor[len(prefix) :].isdigit():
                raise failure("BINDING_MISMATCH")
            offset = int(cursor[len(prefix) :])
        window = artifact.rows[offset : offset + limit]
        following = offset + len(window)
        more = following < len(artifact.rows)
        cap = self.request.verification_key.capability
        return ReportingDestinationPage(
            self.request.reporting_revision_id,
            window,
            len(artifact.rows),
            more,
            f"{prefix}{following}" if more else None,
            cap.format,
            cap.verification_path,
            artifact.locator.resource.native_version_ref,
        )

    async def read_manifest(self, locator: ReportingDestinationLocator) -> bytes:
        result = self._artifact(locator).manifest
        if result is None:
            raise failure("RESOURCE_UNAVAILABLE")
        return result

    async def list_objects(self, locator: ReportingDestinationLocator) -> tuple[str, ...]:
        return tuple(ref for ref, _ in self._artifact(locator).objects)

    async def read_object(
        self, locator: ReportingDestinationLocator, *, object_ref: str
    ) -> AsyncIterator[bytes]:
        for ref, raw in self._artifact(locator).objects:
            if ref == object_ref:
                for start in range(0, len(raw), 4096):
                    yield raw[start : start + 4096]
                return
        raise failure("RESOURCE_UNAVAILABLE")

    async def observe_native_version(
        self, locator: ReportingDestinationLocator
    ) -> ReportingNativeObservation:
        resource = self._artifact(locator).locator.resource
        path = self.request.verification_key.capability.verification_path
        if resource.native_version_ref is None or path == "producer":
            raise failure("UNSUPPORTED_VERIFICATION")
        return ReportingNativeObservation(resource.location, resource.native_version_ref, path)
